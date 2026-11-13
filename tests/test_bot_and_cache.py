from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import bot
from src.cache import get_cache_path, get_cached_day_digest, save_day_digest_cache
from src.pipeline import DigestResult
from src.storage import atomic_write_text, load_state, save_state
from src.subscribers import (
    add_subscriber,
    build_help_message,
    build_welcome_message,
    load_subscribers,
    remove_subscriber,
)


class CacheTests(unittest.TestCase):
    def test_cache_hit_and_no_tmp_left(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            res = DigestResult(text="<b>Digest</b>", posts=[], is_empty=False)
            save_day_digest_cache("ai", res, cache_dir=cdir)

            hit = get_cached_day_digest("ai", max_age_hours=5.0, cache_dir=cdir)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit.text, "<b>Digest</b>")
            self.assertFalse(hit.is_empty)
            self.assertEqual(hit.posts_count, 0)
            self.assertEqual(list(cdir.glob("*.tmp")), [])

    def test_cache_expired(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            res = DigestResult(text="old", posts=[], is_empty=False)
            save_day_digest_cache("ai", res, cache_dir=cdir, generated_at=time.time() - 5.1 * 3600)

            self.assertIsNone(get_cached_day_digest("ai", max_age_hours=5.0, cache_dir=cdir))

    def test_empty_result_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            empty_res = DigestResult(text="empty", posts=[], is_empty=True)
            save_day_digest_cache("spb", empty_res, cache_dir=cdir)

            hit = get_cached_day_digest("spb", max_age_hours=5.0, cache_dir=cdir)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertTrue(hit.is_empty)
            self.assertEqual(hit.text, "empty")

    def test_corrupted_or_invalid_cache_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            cache_path = get_cache_path("ai", cache_dir=cdir)
            cache_path.parent.mkdir(parents=True, exist_ok=True)

            cache_path.write_text("{bad json", encoding="utf-8")
            self.assertIsNone(get_cached_day_digest("ai", max_age_hours=5.0, cache_dir=cdir))

            cache_path.write_text("[]", encoding="utf-8")
            self.assertIsNone(get_cached_day_digest("ai", max_age_hours=5.0, cache_dir=cdir))


class StorageAndSubscribersTests(unittest.TestCase):
    def test_atomic_write_retries_on_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "retry.txt"
            orig_replace = Path.replace
            calls = {"n": 0}

            def flaky_replace(self_path: Path, dst: Path) -> Path:
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise PermissionError("locked")
                return orig_replace(self_path, dst)

            with patch.object(Path, "replace", flaky_replace), patch("src.storage.time.sleep"):
                atomic_write_text(target, "ok")

            self.assertEqual(target.read_text(encoding="utf-8"), "ok")
            self.assertEqual(calls["n"], 3)

    def test_save_and_load_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            save_state({"chan1": 42}, state_path)
            self.assertEqual(load_state(state_path), {"chan1": 42})
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_add_and_remove_subscriber(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            subs_path = Path(td) / "subscribers.json"
            self.assertTrue(add_subscriber("ai", 1001, subs_path))
            self.assertFalse(add_subscriber("ai", 1001, subs_path))
            self.assertTrue(add_subscriber("spb", 1001, subs_path))
            self.assertEqual(load_subscribers(subs_path), {"ai": [1001], "spb": [1001]})

            self.assertTrue(remove_subscriber("ai", 1001, subs_path))
            self.assertFalse(remove_subscriber("ai", 1001, subs_path))
            self.assertEqual(load_subscribers(subs_path), {"ai": [], "spb": [1001]})

    def test_welcome_and_help_messages(self) -> None:
        cfg = {"name": "ai", "description": "Технический дайджест AI/ML", "days_lookback": 3}
        w_new = build_welcome_message(cfg, is_new=True)
        w_old = build_welcome_message(cfg, is_new=False)
        help_msg = build_help_message(cfg)

        self.assertIn("Технический дайджест AI/ML", w_new)
        self.assertNotEqual(w_new, w_old)
        self.assertIn("/get_day", w_new)
        self.assertIn("Дайджест за сегодня", w_new)
        self.assertIn("раз в 3 дня", w_new)
        self.assertIn("/get_day", help_msg)
        self.assertIn("раз в 3 дня", help_msg)


class BotTests(unittest.TestCase):
    def test_resolve_command(self) -> None:
        self.assertEqual(bot.resolve_command("/get_day"), "get_day")
        self.assertEqual(bot.resolve_command("/get_today"), "get_day")
        self.assertEqual(bot.resolve_command("/get_day@my_bot"), "get_day")
        self.assertEqual(bot.resolve_command("📅 Дайджест за сегодня"), "get_day")
        self.assertEqual(bot.resolve_command("  Дайджест за сегодня "), "get_day")
        self.assertEqual(bot.resolve_command("/start"), "start")
        self.assertEqual(bot.resolve_command("/start@my_bot"), "start")
        self.assertEqual(bot.resolve_command("/help"), "help")
        self.assertEqual(bot.resolve_command("/stop"), "stop")
        self.assertIsNone(bot.resolve_command("случайный текст"))
        self.assertIsNone(bot.resolve_command("/unknown"))
        self.assertIsNone(bot.resolve_command("   "))

    def test_discover_bot_targets_rejects_duplicate_tokens(self) -> None:
        with patch("bot.list_profiles", return_value=["ai", "spb"]), \
             patch("bot.get_bot_token", return_value="SAME_TOKEN"), \
             patch("bot.load_profile_config", side_effect=lambda p: {"name": p}):
            with self.assertRaises(ValueError) as ctx:
                bot.discover_bot_targets()
            self.assertIn("409", str(ctx.exception))

    def test_build_and_send_day_digest_uses_lock_and_cache(self) -> None:
        cfg = {
            "name": "ai",
            "description": "AI",
            "days_lookback": 3,
            "max_pages_per_channel": 1,
            "prompt_path": Path("x"),
            "channels": ["c"],
        }
        sent: list[tuple[int, str]] = []
        gen_calls: list[tuple[int, bool]] = []

        def slow_generate(profile_cfg: dict, days_lookback: int, no_state: bool = False) -> DigestResult:
            gen_calls.append((days_lookback, no_state))
            time.sleep(0.15)
            return DigestResult(text="<b>Digest</b>", posts=[], is_empty=False)

        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            with patch("bot.generate_digest", side_effect=slow_generate), \
                 patch("bot.send_single_chat", side_effect=lambda text, chat_id, bot_token, client: sent.append((chat_id, text))), \
                 patch("bot.get_cached_day_digest", side_effect=lambda p, max_age_hours=5.0: get_cached_day_digest(p, max_age_hours, cache_dir=cdir)), \
                 patch("bot.save_day_digest_cache", side_effect=lambda p, r: save_day_digest_cache(p, r, cache_dir=cdir)):
                t1 = threading.Thread(target=bot.build_and_send_day_digest, args=(cfg, "tok", 111, 5.0))
                t2 = threading.Thread(target=bot.build_and_send_day_digest, args=(cfg, "tok", 222, 5.0))
                t1.start()
                t2.start()
                t1.join()
                t2.join()

        self.assertEqual(gen_calls, [(1, True)])
        self.assertEqual(len(sent), 2)
        self.assertTrue(any("🕒 Собрано в " in txt for _, txt in sent))

    def test_build_and_send_day_digest_error_does_not_cache(self) -> None:
        cfg = {
            "name": "ai",
            "description": "AI",
            "days_lookback": 3,
            "max_pages_per_channel": 1,
            "prompt_path": Path("x"),
            "channels": ["c"],
        }
        err_msgs: list[tuple[int, str]] = []

        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            with patch("bot.generate_digest", side_effect=RuntimeError("LLM down")), \
                 patch("bot.send_bot_message", side_effect=lambda c, t, cid, txt, **kw: err_msgs.append((cid, txt))), \
                 patch("bot.get_cached_day_digest", side_effect=lambda p, max_age_hours=5.0: get_cached_day_digest(p, max_age_hours, cache_dir=cdir)), \
                 patch("bot.save_day_digest_cache", side_effect=lambda p, r: save_day_digest_cache(p, r, cache_dir=cdir)):
                bot.build_and_send_day_digest(cfg, "tok", 333, 5.0)

            self.assertIsNone(get_cached_day_digest("ai", 5.0, cache_dir=cdir))
            self.assertEqual(err_msgs, [(333, "❌ Не удалось собрать дайджест, попробуйте позже.")])

    def test_build_and_send_day_digest_notifies_admin_on_error(self) -> None:
        cfg = {"name": "ai", "description": "AI"}
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp)
            with (
                patch("bot.get_cached_day_digest", lambda p, max_age_hours: get_cached_day_digest(p, max_age_hours, cache_dir=cdir)),
                patch("bot.generate_digest", side_effect=RuntimeError("Quota exceeded")),
                patch("bot.send_bot_message"),
                patch("bot.notify_admin") as mock_notify,
            ):
                bot.build_and_send_day_digest(cfg, "tok", 333, 5.0)

            mock_notify.assert_called_once()
            alert_text = mock_notify.call_args.args[0]
            self.assertIn("ai", alert_text)
            self.assertIn("333", alert_text)
            self.assertIn("Quota exceeded", alert_text)


class LoggingAndAlertsTests(unittest.TestCase):
    def test_setup_logging_writes_to_file(self) -> None:
        import logging
        from src.logging_config import setup_logging

        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp) / "logs"
            log_path = setup_logging("test_bot.log", logs_dir=logs_dir)
            test_logger = logging.getLogger("test.logging")
            test_logger.info("Тестовая запись в лог")

            for handler in logging.getLogger().handlers:
                handler.flush()
                handler.close()
            logging.getLogger().handlers.clear()

            self.assertTrue(log_path.exists())
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("Тестовая запись в лог", content)

    def test_notify_admin_skips_when_unset(self) -> None:
        from src.sender import notify_admin

        with patch("src.sender.get_admin_chat_id", return_value=None):
            self.assertFalse(notify_admin("alert", bot_token="tok"))

    def test_notify_admin_sends_plain_text_when_set(self) -> None:
        from src.sender import notify_admin

        mock_resp = MagicMock(is_error=False)
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp

        with patch("src.sender.get_admin_chat_id", return_value="999000"):
            sent = notify_admin("Ошибка LLM", bot_token="tok123", client=mock_client)

        self.assertTrue(sent)
        mock_client.post.assert_called_once()
        url, kwargs = mock_client.post.call_args.args[0], mock_client.post.call_args.kwargs
        self.assertIn("tok123", url)
        self.assertEqual(kwargs["json"]["chat_id"], "999000")
        self.assertEqual(kwargs["json"]["text"], "Ошибка LLM")
        self.assertNotIn("parse_mode", kwargs["json"])


if __name__ == "__main__":
    unittest.main()
