from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from src.analyzer import build_digest
from src.fetcher import USER_AGENT, Post, fetch_channel_posts, normalize_channel_name
from src.sender import send_telegram_message
from src.storage import DEFAULT_STATE_PATH, load_state, save_state, update_state_with_posts

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"

logger = logging.getLogger("digest")


def setup_logging() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_profile_config(profile_name: str, settings_path: Path = SETTINGS_PATH) -> dict[str, Any]:
    """
    Загружает настройки выбранного профиля из config/settings.yaml,
    объединяя их с секцией defaults.
    """
    if not settings_path.exists():
        raise FileNotFoundError(f"Файл конфигурации не найден: {settings_path}")

    raw_cfg = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    defaults = raw_cfg.get("defaults", {})
    profiles = raw_cfg.get("profiles", {})

    if profile_name not in profiles:
        available = ", ".join(profiles.keys()) if profiles else "нет доступных профилей"
        raise ValueError(
            f"Профиль '{profile_name}' не найден в {settings_path.name}. "
            f"Доступные профили: {available}"
        )

    profile = profiles[profile_name]
    days_lookback = int(profile.get("days_lookback", defaults.get("days_lookback", 3)))
    max_pages = int(profile.get("max_pages_per_channel", defaults.get("max_pages_per_channel", 3)))
    prompt_rel_path = profile.get("prompt_file")
    if not prompt_rel_path:
        raise ValueError(f"В профиле '{profile_name}' не указан параметр 'prompt_file'.")

    prompt_path = (CONFIG_DIR / prompt_rel_path).resolve()
    if not prompt_path.exists():
        raise FileNotFoundError(f"Файл промпта не найден: {prompt_path}")

    channels = profile.get("channels") or []
    if not channels:
        raise ValueError(f"В профиле '{profile_name}' пустой список каналов 'channels'.")

    return {
        "name": profile_name,
        "description": profile.get("description", profile_name),
        "days_lookback": days_lookback,
        "max_pages_per_channel": max_pages,
        "prompt_path": prompt_path,
        "channels": channels,
        "chat_id": profile.get("chat_id"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сбор и генерация тематических Telegram-дайджестов с помощью LLM."
    )
    parser.add_argument(
        "-p",
        "--profile",
        default="ai",
        help="Имя профиля из config/settings.yaml (по умолчанию: ai)",
    )
    parser.add_argument(
        "-d",
        "--days",
        type=int,
        default=None,
        help="Переопределить глубину поиска в днях (days_lookback)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Тестовый запуск: вывести дайджест в консоль, НЕ отправлять в Telegram и НЕ обновлять state.json",
    )
    parser.add_argument(
        "--no-state",
        action="store_true",
        help="Игнорировать сохраненный state.json (собрать все посты за указанное число дней заново)",
    )
    return parser.parse_args()


def main() -> None:
    setup_logging()
    args = parse_args()

    profile_cfg = load_profile_config(args.profile)
    days_lookback: int = args.days if args.days is not None else profile_cfg["days_lookback"]
    max_pages: int = profile_cfg["max_pages_per_channel"]
    channels: list[str] = profile_cfg["channels"]
    prompt_path: Path = profile_cfg["prompt_path"]

    logger.info(
        "=== Запуск профиля '%s' (%s) | Дней: %d | Каналов: %d ===",
        profile_cfg["name"],
        profile_cfg["description"],
        days_lookback,
        len(channels),
    )

    state = {} if args.no_state else load_state(DEFAULT_STATE_PATH)
    all_posts: list[Post] = []

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
        follow_redirects=True,
        timeout=15.0,
    ) as http_client:
        for raw_channel in channels:
            norm_name = normalize_channel_name(raw_channel)
            last_seen_id = None if args.no_state else state.get(norm_name)
            channel_posts = fetch_channel_posts(
                raw_channel=raw_channel,
                days_lookback=days_lookback,
                last_seen_id=last_seen_id,
                max_pages=max_pages,
                client=http_client,
            )
            logger.info("Канал @%s: собрано новых постов — %d", norm_name, len(channel_posts))
            all_posts.extend(channel_posts)

    if not all_posts:
        logger.info("Новых постов за выбранный период нет. Завершение работы.")
        return

    logger.info("Всего собрано постов: %d. Генерация дайджеста через LLM...", len(all_posts))
    digest_body = build_digest(
        posts=all_posts,
        days_lookback=days_lookback,
        prompts_path=prompt_path,
    )

    today_str = datetime.now().strftime("%d.%m.%Y")
    header = f"📌 <b>{profile_cfg['description']} ({today_str})</b>\n\n"
    full_digest = header + digest_body

    if args.dry_run:
        print("\n" + "=" * 60)
        print("РЕЖИМ DRY-RUN (текст не отправлен в Telegram, state.json не изменен):")
        print("=" * 60 + "\n")
        print(full_digest)
        print("\n" + "=" * 60)
        return

    send_telegram_message(
        text=full_digest,
        chat_id=profile_cfg.get("chat_id"),
    )

    # Обновляем state.json только после успешной отправки в Telegram
    full_state = load_state(DEFAULT_STATE_PATH)
    updated_state = update_state_with_posts(full_state, all_posts)
    save_state(updated_state, DEFAULT_STATE_PATH)


if __name__ == "__main__":
    main()
