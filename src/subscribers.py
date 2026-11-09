from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DEFAULT_SUBSCRIBERS_PATH = Path(__file__).resolve().parent.parent / "data" / "subscribers.json"


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
    Сохраняет словарь подписчиков в JSON с форматированием.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    raw_text = json.dumps(data, indent=2, ensure_ascii=False)
    file_path.write_text(raw_text + "\n", encoding="utf-8")


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
    subscribers_data = load_subscribers(file_path)
    profile_subs = set(subscribers_data.get(profile, []))

    if not bot_token:
        logger.warning("Токен бота не передан, синхронизация подписчиков пропущена.")
        return sorted(profile_subs)

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params={"allowed_updates": '["message"]'})
            if resp.is_error:
                logger.warning("Ошибка getUpdates (%d): %s", resp.status_code, resp.text)
                return sorted(profile_subs)

            result = resp.json().get("result", [])
            if not result:
                return sorted(profile_subs)

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
                    if chat_id not in profile_subs:
                        logger.info("Новый подписчик [%s] chat_id: %s", profile, chat_id)
                        profile_subs.add(chat_id)
                        msg = "✅ Вы успешно подписались на дайджест!"
                    else:
                        msg = "✅ Вы уже подписаны на дайджест."
                    try:
                        client.post(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            json={"chat_id": chat_id, "text": msg},
                        )
                    except Exception as send_err:
                        logger.warning("Не удалось отправить ответ на /start в chat_id %s: %s", chat_id, send_err)

                elif text.startswith("/stop"):
                    if chat_id in profile_subs:
                        logger.info("Отписка [%s] chat_id: %s", profile, chat_id)
                        profile_subs.discard(chat_id)
                        msg = "❌ Вы успешно отписались от дайджеста."
                    else:
                        msg = "ℹ️ Вы не были подписаны на дайджест."
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

            subscribers_data[profile] = sorted(profile_subs)
            save_subscribers(subscribers_data, file_path)

    except Exception as exc:
        logger.warning("Исключение при опросе getUpdates: %s", exc)

    return sorted(profile_subs)
