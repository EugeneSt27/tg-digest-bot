from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from typing import Any

import httpx

from src.pipeline import list_profiles, load_profile_config
from src.sender import get_bot_token
from src.subscribers import (
    add_subscriber,
    build_help_message,
    build_welcome_message,
    remove_subscriber,
)

logger = logging.getLogger("digest.bot")

GET_DAY_BUTTON = "📅 Дайджест за сегодня"
ALIASES = {
    "/get_day": "get_day",
    "/get_today": "get_day",
    GET_DAY_BUTTON.lower(): "get_day",
    "дайджест за сегодня": "get_day",
    "/start": "start",
    "/help": "help",
    "/stop": "stop",
}

REPLY_KEYBOARD = {
    "keyboard": [[{"text": GET_DAY_BUTTON}]],
    "resize_keyboard": True,
    "is_persistent": True,
}

BOT_COMMANDS = [
    {"command": "start", "description": "Подписаться на рассылку"},
    {"command": "get_day", "description": "Дайджест за последние 24 часа"},
    {"command": "help", "description": "Справка по командам"},
    {"command": "stop", "description": "Отписаться от рассылки"},
]

POLL_TIMEOUT_SECONDS = 30
HTTP_CLIENT_TIMEOUT_SECONDS = 40.0
RETRY_DELAY_SECONDS = 5.0


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
    # Не засоряем лог каждые 30 секунд URL-ами с токеном при long polling
    logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Интерактивный Telegram-бот для подписки и получения дневного дайджеста."
    )
    parser.add_argument(
        "-p",
        "--profile",
        default=None,
        help="Запустить только указанный профиль из config/settings.yaml (по умолчанию: все профили с токенами)",
    )
    parser.add_argument(
        "--cache-hours",
        type=float,
        default=5.0,
        help="Время жизни кэша /get_day в часах (по умолчанию: 5)",
    )
    return parser.parse_args()


def resolve_command(text: str) -> str | None:
    """
    Нормализует текст сообщения и возвращает имя команды ('start', 'help', 'stop', 'get_day')
    или None, если текст не распознан как команда.
    """
    if not text:
        return None

    normalized = text.strip().lower()
    if not normalized:
        return None

    if normalized in ALIASES:
        return ALIASES[normalized]

    if normalized.startswith("/"):
        command_token = normalized.split()[0].split("@", 1)[0]
        return ALIASES.get(command_token)

    return None


def discover_bot_targets(
    selected_profile: str | None = None,
) -> list[tuple[dict[str, Any], str]]:
    """
    Находит профили с заданными токенами ботов.
    При обнаружении одинаковых токенов у разных профилей возбуждает ValueError (защита от 409).
    """
    if selected_profile:
        profile_cfg = load_profile_config(selected_profile)
        token = get_bot_token(selected_profile)
        if not token:
            raise ValueError(
                f"Не задан токен бота для профиля '{selected_profile}': "
                f"укажите TELEGRAM_BOT_TOKEN_{selected_profile.upper()} или TELEGRAM_BOT_TOKEN в .env"
            )
        return [(profile_cfg, token)]

    targets: list[tuple[dict[str, Any], str]] = []
    seen_tokens: dict[str, str] = {}

    for profile_name in list_profiles():
        token = get_bot_token(profile_name)
        if not token:
            logger.warning("Профиль '%s' пропущен: не задан токен бота.", profile_name)
            continue

        if token in seen_tokens:
            other_profile = seen_tokens[token]
            raise ValueError(
                f"Конфликт токенов (ошибка 409): профили '{other_profile}' и '{profile_name}' "
                "используют один и тот же токен бота. Задайте отдельные TELEGRAM_BOT_TOKEN_<NAME> в .env."
            )

        seen_tokens[token] = profile_name
        profile_cfg = load_profile_config(profile_name)
        targets.append((profile_cfg, token))

    if not targets:
        raise ValueError("Не найдено ни одного профиля с заданным токеном бота.")

    return targets


def register_bot_commands(client: httpx.Client, bot_token: str, profile: str) -> None:
    """Регистрирует команды в меню бота через Telegram setMyCommands."""
    url = f"https://api.telegram.org/bot{bot_token}/setMyCommands"
    try:
        resp = client.post(url, json={"commands": BOT_COMMANDS})
        if resp.is_error:
            logger.warning("[%s] Не удалось установить команды (setMyCommands %d): %s", profile, resp.status_code, resp.text)
        else:
            logger.info("[%s] Меню команд бота зарегистрировано.", profile)
    except Exception as exc:
        logger.warning("[%s] Ошибка при вызове setMyCommands: %s", profile, exc)


def send_bot_message(
    client: httpx.Client,
    bot_token: str,
    chat_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode: str | None = "HTML",
) -> None:
    """Отправляет короткое сервисное сообщение пользователю."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        resp = client.post(url, json=payload)
        if resp.is_error:
            logger.warning("Ошибка sendMessage (%d) в чат %s: %s", resp.status_code, chat_id, resp.text)
    except Exception as exc:
        logger.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, exc)


def handle_update(
    update: dict[str, Any],
    profile_cfg: dict[str, Any],
    bot_token: str,
    client: httpx.Client,
    cache_hours: float,
) -> None:
    """Обрабатывает один входящий апдейт от Telegram."""
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text") or ""

    if not chat_id:
        return

    profile = profile_cfg["name"]
    command = resolve_command(text)

    if command == "start":
        is_new = add_subscriber(profile=profile, chat_id=int(chat_id))
        msg = build_welcome_message(profile_cfg, is_new=is_new)
        send_bot_message(client, bot_token, int(chat_id), msg, reply_markup=REPLY_KEYBOARD)
        return

    if command == "stop":
        removed = remove_subscriber(profile=profile, chat_id=int(chat_id))
        msg = (
            "❌ Вы отписались от плановой рассылки.\n"
            "Кнопка «📅 Дайджест за сегодня» и команда /get_day по-прежнему доступны."
            if removed
            else "ℹ️ Вы не были подписаны на рассылку."
        )
        send_bot_message(client, bot_token, int(chat_id), msg, parse_mode=None)
        return

    if command == "help":
        msg = build_help_message(profile_cfg)
        send_bot_message(client, bot_token, int(chat_id), msg, reply_markup=REPLY_KEYBOARD)
        return

    if command == "get_day":
        send_bot_message(
            client,
            bot_token,
            int(chat_id),
            "⏳ Обработчик /get_day будет подключен на следующем шаге.",
            parse_mode=None,
        )
        return

    if text.strip():
        send_bot_message(
            client,
            bot_token,
            int(chat_id),
            "Нажмите кнопку «📅 Дайджест за сегодня» или /help.",
            parse_mode=None,
        )


def run_polling_loop(
    profile_cfg: dict[str, Any],
    bot_token: str,
    cache_hours: float,
    stop_event: threading.Event,
) -> None:
    """Бесконечный цикл long polling для одного профиля."""
    profile = profile_cfg["name"]
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    offset: int | None = None

    logger.info("[%s] Запуск long polling (%s)...", profile, profile_cfg["description"])

    with httpx.Client(timeout=HTTP_CLIENT_TIMEOUT_SECONDS) as client:
        register_bot_commands(client, bot_token, profile)

        while not stop_event.is_set():
            params: dict[str, Any] = {
                "timeout": POLL_TIMEOUT_SECONDS,
                "allowed_updates": '["message"]',
            }
            if offset is not None:
                params["offset"] = offset

            try:
                resp = client.get(url, params=params)
                if resp.status_code == 409:
                    logger.error(
                        "[%s] Конфликт 409: другой процесс уже опрашивает getUpdates для этого бота.",
                        profile,
                    )
                    stop_event.wait(RETRY_DELAY_SECONDS)
                    continue
                if resp.is_error:
                    logger.warning("[%s] Ошибка getUpdates (%d): %s", profile, resp.status_code, resp.text)
                    stop_event.wait(RETRY_DELAY_SECONDS)
                    continue

                updates = resp.json().get("result", [])
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int) and (offset is None or update_id >= offset):
                        offset = update_id + 1
                    try:
                        handle_update(update, profile_cfg, bot_token, client, cache_hours)
                    except Exception:
                        logger.exception("[%s] Ошибка при обработке апдейта %s", profile, update_id)

            except httpx.HTTPError as exc:
                logger.warning("[%s] Сетевая ошибка getUpdates: %s. Повтор через %.0f с...", profile, exc, RETRY_DELAY_SECONDS)
                stop_event.wait(RETRY_DELAY_SECONDS)
            except Exception:
                logger.exception("[%s] Непредвиденная ошибка в polling. Повтор через %.0f с...", profile, RETRY_DELAY_SECONDS)
                stop_event.wait(RETRY_DELAY_SECONDS)


def main() -> None:
    setup_logging()
    args = parse_args()

    try:
        targets = discover_bot_targets(args.profile)
    except Exception as exc:
        logger.error("Ошибка запуска: %s", exc)
        sys.exit(1)

    stop_event = threading.Event()
    threads: list[threading.Thread] = []

    for profile_cfg, token in targets:
        thread = threading.Thread(
            target=run_polling_loop,
            args=(profile_cfg, token, args.cache_hours, stop_event),
            name=f"bot-{profile_cfg['name']}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    logger.info("Запущено ботов: %d. Для остановки нажмите Ctrl+C.", len(threads))

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Получен сигнал остановки (Ctrl+C). Завершение работы...")
        stop_event.set()


if __name__ == "__main__":
    main()
