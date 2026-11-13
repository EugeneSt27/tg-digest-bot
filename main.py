from __future__ import annotations

import argparse
import logging

from src.logging_config import setup_logging
from src.pipeline import generate_digest, load_profile_config
from src.sender import broadcast_digest, get_bot_token, get_env_chat_id, notify_admin
from src.storage import (
    DEFAULT_STATE_PATH,
    load_state,
    save_digest_to_archive,
    save_state,
    update_state_with_posts,
)
from src.subscribers import load_subscribers, sync_subscribers

logger = logging.getLogger("digest")


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
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Не вызывать getUpdates (используется при запущенном bot.py), брать подписчиков из subscribers.json",
    )
    return parser.parse_args()


def main() -> None:
    setup_logging("digest.log")
    args = parse_args()

    try:
        _run_digest(args)
    except Exception as exc:
        logger.exception("Критическая ошибка при выполнении дайджеста (профиль '%s').", args.profile)
        if not args.dry_run:
            notify_admin(
                f"⚠️ [main / {args.profile}] Ошибка при запуске дайджеста:\n"
                f"{type(exc).__name__}: {exc}",
                profile=args.profile,
            )
        raise


def _run_digest(args: argparse.Namespace) -> None:
    profile_cfg = load_profile_config(args.profile)
    days_lookback: int = args.days if args.days is not None else profile_cfg["days_lookback"]
    channels: list[str] = profile_cfg["channels"]

    logger.info(
        "=== Запуск профиля '%s' (%s) | Дней: %d | Каналов: %d ===",
        profile_cfg["name"],
        profile_cfg["description"],
        days_lookback,
        len(channels),
    )

    bot_token = get_bot_token(args.profile)
    recipients: list[str | int] = []

    if not args.dry_run:
        if not bot_token:
            raise ValueError(
                f"Не задан токен бота: укажите TELEGRAM_BOT_TOKEN_{args.profile.upper()} или TELEGRAM_BOT_TOKEN в .env"
            )

        if args.no_sync:
            subscribers = load_subscribers().get(args.profile, [])
        else:
            # Синхронизируем подписчиков (/start, /stop) перед началом сбора и генерации
            subscribers = sync_subscribers(bot_token=bot_token, profile=args.profile)

        # Единый список получателей: подписчики + .env + chat_id из settings.yaml
        recipients = list(subscribers)
        for extra_chat_id in (get_env_chat_id(args.profile), profile_cfg.get("chat_id")):
            if extra_chat_id is not None and str(extra_chat_id).strip():
                recipients.append(str(extra_chat_id).strip())

        # Дедупликация по строковому значению с сохранением порядка
        recipients = list(dict.fromkeys(str(cid) for cid in recipients))
        if not recipients:
            raise ValueError(
                f"Нет получателей для профиля '{args.profile}': "
                "нет подписчиков (никто не отправил /start) и не задан TELEGRAM_CHAT_ID / chat_id в профиле. "
                "Сбор постов и обращение к LLM отменены."
            )
        logger.info("Подготовлен список получателей дайджеста (%d): %s", len(recipients), recipients)
    elif bot_token and not args.no_sync:
        # В режиме dry-run, если токен есть, синхронизируем подписчиков без обязательного требования их наличия
        sync_subscribers(bot_token=bot_token, profile=args.profile)

    result = generate_digest(
        profile_cfg=profile_cfg,
        days_lookback=days_lookback,
        no_state=args.no_state,
    )

    if result.is_empty:
        logger.info("Новых постов за выбранный период нет. Завершение работы.")
        return

    full_digest = result.text

    if args.dry_run:
        print("\n" + "=" * 60)
        print("РЕЖИМ DRY-RUN (текст не отправлен в Telegram, state.json не изменен):")
        print("=" * 60 + "\n")
        print(full_digest)
        print("\n" + "=" * 60)
        return

    stats = broadcast_digest(text=full_digest, chat_ids=recipients, bot_token=bot_token)
    if stats["success"] == 0:
        # state.json не обновляем, чтобы посты не были потеряны
        raise RuntimeError("Дайджест не доставлен ни одному получателю.")

    # Фиксируем отправленный дайджест в архиве data/digests/
    save_digest_to_archive(
        profile=args.profile,
        text=full_digest,
        days_lookback=days_lookback,
        posts_count=len(result.posts),
    )

    # Обновляем state.json только после успешной отправки в Telegram
    full_state = load_state(DEFAULT_STATE_PATH)
    updated_state = update_state_with_posts(full_state, result.posts)
    save_state(updated_state, DEFAULT_STATE_PATH)


if __name__ == "__main__":
    main()
