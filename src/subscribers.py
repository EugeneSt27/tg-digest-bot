from __future__ import annotations

import html
import json
import logging
import threading
from pathlib import Path
from typing import Any

import httpx

from src.storage import atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_SUBSCRIBERS_PATH = Path(__file__).resolve().parent.parent / "data" / "subscribers.json"
_SUBSCRIBERS_LOCK = threading.Lock()


def load_subscribers(file_path: Path = DEFAULT_SUBSCRIBERS_PATH) -> dict[str, list[int]]:
    """
    Загружает словарь подписчиков вида {"profile_name": [chat_id, ...]} из JSON.
    Если файл отсутствует или поврежден, возвращает пустой словарь.
    """
    if not file_path.exists():
        return {}

    try:
        raw_text = file_path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return {}
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            logger.warning("Некорректный формат %s (ожидался dict), возвращаем пустой словарь.", file_path)
            return {}
        result: dict[str, list[int]] = {}
        for key, value in data.items():
            if isinstance(value, list):
                result[str(key)] = [int(item) for item in value if str(item).lstrip("-").isdigit()]
        return result
    except Exception as exc:
        logger.warning("Не удалось прочитать %s: %s. Возвращаем пустой словарь.", file_path, exc)
        return {}


def save_subscribers(data: dict[str, list[int]], file_path: Path = DEFAULT_SUBSCRIBERS_PATH) -> None:
    """
    Атомарно сохраняет словарь подписчиков в JSON с форматированием.
    """
    raw_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(file_path, raw_text)


def add_subscriber(
    profile: str,
    chat_id: int,
    file_path: Path = DEFAULT_SUBSCRIBERS_PATH,
) -> bool:
    """
    Добавляет chat_id в подписчики профиля.
    Возвращает True, если подписчик новый, и False, если уже был подписан.
    """
    with _SUBSCRIBERS_LOCK:
        subscribers_data = load_subscribers(file_path)
        profile_subs = set(subscribers_data.get(profile, []))
        if chat_id in profile_subs:
            return False

        profile_subs.add(chat_id)
        subscribers_data[profile] = sorted(profile_subs)
        save_subscribers(subscribers_data, file_path)
        logger.info("Новый подписчик [%s] chat_id: %s", profile, chat_id)
        return True


def remove_subscriber(
    profile: str,
    chat_id: int,
    file_path: Path = DEFAULT_SUBSCRIBERS_PATH,
) -> bool:
    """
    Удаляет chat_id из подписчиков профиля.
    Возвращает True, если подписчик был удален, и False, если не был подписан.
    """
    with _SUBSCRIBERS_LOCK:
        subscribers_data = load_subscribers(file_path)
        profile_subs = set(subscribers_data.get(profile, []))
        if chat_id not in profile_subs:
            return False

        profile_subs.discard(chat_id)
        subscribers_data[profile] = sorted(profile_subs)
        save_subscribers(subscribers_data, file_path)
        logger.info("Отписка [%s] chat_id: %s", profile, chat_id)
        return True


def _format_frequency(days_lookback: int) -> str:
    """Форматирует период рассылки по-русски."""
    if days_lookback <= 1:
        return "каждый день"
    if 11 <= (days_lookback % 100) <= 14:
        unit = "дней"
    elif days_lookback % 10 == 1:
        unit = "день"
    elif days_lookback % 10 in (2, 3, 4):
        unit = "дня"
    else:
        unit = "дней"
    return f"раз в {days_lookback} {unit}"


def build_welcome_message(profile_cfg: dict[str, Any], is_new: bool) -> str:
    """Формирует приветственное сообщение для /start."""
    description = html.escape(str(profile_cfg.get("description") or profile_cfg.get("name") or "дайджест"))
    days_lookback = int(profile_cfg.get("days_lookback", 3))
    freq = _format_frequency(days_lookback)

    status_line = (
        f"✅ Добро пожаловать на <b>{description}</b>!"
        if is_new
        else f"Я понимаю, что вы хотите знать как можно больше, но Вы уже подписаны на <b>{description}</b>."
    )
    return (
        f"{status_line}\n\n"
        f"📬 Плановая рассылка приходит {freq}.\n"
        "📅 Нажмите кнопку <b>«📅 Дайджест за сегодня»</b> или отправьте /get_day, "
        "чтобы получить сводку за сегодня прямо сейчас.\n\n"
        "ℹ️ /help — справка · /stop — отписаться от рассылки"
    )


def build_help_message(profile_cfg: dict[str, Any]) -> str:
    """Формирует справочное сообщение для /help."""
    description = html.escape(str(profile_cfg.get("description") or profile_cfg.get("name") or "дайджест"))
    days_lookback = int(profile_cfg.get("days_lookback", 3))
    freq = _format_frequency(days_lookback)

    return (
        f"ℹ️ <b>{description}</b>\n"
        f"Плановая рассылка приходит {freq}.\n\n"
        "<b>Доступные команды:</b>\n"
        "• /get_day (или кнопка <b>«📅 Дайджест за сегодня»</b>) — сводка за последние 24 часа\n"
        "• /start — подписаться на плановую рассылку\n"
        "• /stop — отписаться от рассылки (кнопка и /get_day останутся доступны)\n"
        "• /help — эта справка"
    )


def sync_subscribers(
    bot_token: str,
    profile: str,
    file_path: Path = DEFAULT_SUBSCRIBERS_PATH,
    timeout: float = 10.0,
) -> list[int]:
    """
    Опрашивает Telegram API методом getUpdates для указанного токена бота,
    обрабатывает команды /start (добавление) и /stop (удаление),
    подтверждает апдейты через offset и сохраняет обновленный список в file_path.
    Возвращает актуальный список подписчиков для профиля.
    """
    if not bot_token:
        logger.warning("Токен бота не передан, синхронизация подписчиков пропущена.")
        return sorted(set(load_subscribers(file_path).get(profile, [])))

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params={"allowed_updates": '["message"]'})
            if resp.is_error:
                logger.warning("Ошибка getUpdates (%d): %s", resp.status_code, resp.text)
                return sorted(set(load_subscribers(file_path).get(profile, [])))

            result = resp.json().get("result", [])
            if not result:
                return sorted(set(load_subscribers(file_path).get(profile, [])))

            max_update_id = 0
            for update in result:
                update_id = update.get("update_id", 0)
                if update_id > max_update_id:
                    max_update_id = update_id

                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                text = (message.get("text") or "").strip()

                if not chat_id:
                    continue

                if text.startswith("/start"):
                    is_new = add_subscriber(profile=profile, chat_id=chat_id, file_path=file_path)
                    msg = (
                        "✅ Вы успешно подписались на дайджест!"
                        if is_new
                        else "✅ Вы уже подписаны на дайджест."
                    )
                    try:
                        client.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": msg},
                        )
                    except Exception as send_err:
                        logger.warning("Не удалось отправить ответ на /start в chat_id %s: %s", chat_id, send_err)

                elif text.startswith("/stop"):
                    removed = remove_subscriber(profile=profile, chat_id=chat_id, file_path=file_path)
                    msg = (
                        "❌ Вы успешно отписались от дайджеста."
                        if removed
                        else "ℹ️ Вы не были подписаны на дайджест."
                    )
                    try:
                        client.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": msg},
                        )
                    except Exception as send_err:
                        logger.warning("Не удалось отправить ответ на /stop в chat_id %s: %s", chat_id, send_err)

                elif text.startswith("/help"):
                    try:
                        client.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": (
                                    "ℹ️ <b>Команды бота:</b>\n"
                                    "/start — подписаться на дайджест\n"
                                    "/stop — отписаться от рассылки\n"
                                    "/help — справка"
                                ),
                                "parse_mode": "HTML",
                            },
                        )
                    except Exception as send_err:
                        logger.warning("Не удалось отправить ответ на /help в chat_id %s: %s", chat_id, send_err)

            # Подтверждаем обработанные апдейты через offset
            if max_update_id > 0:
                client.get(url, params={"offset": max_update_id + 1, "limit": 1})

    except Exception as exc:
        logger.warning("Исключение при опросе getUpdates: %s", exc)

    return sorted(set(load_subscribers(file_path).get(profile, [])))
