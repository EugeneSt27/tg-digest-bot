from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Лимит Telegram — 4096 символов
MAX_MESSAGE_LENGTH = 4000


def split_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    Разбивает длинный текст дайджеста на части не длиннее `max_length` символов.
    Разрез делается по двойным или одинарным переносам строк, чтобы не разорвать HTML-теги.
    """
    text = text.strip()
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > max_length:
        # Ищем последний двойной перенос строки (между пунктами или рубриками)
        split_idx = remaining.rfind("\n\n", 0, max_length)
        if split_idx == -1:
            # Если двойного нет, ищем одинарный перенос строки
            split_idx = remaining.rfind("\n", 0, max_length)
        if split_idx == -1:
            # Крайний случай — режем по пробелу
            split_idx = remaining.rfind(" ", 0, max_length)
        if split_idx == -1:
            split_idx = max_length

        chunks.append(remaining[:split_idx].strip())
        remaining = remaining[split_idx:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def send_telegram_message(
    text: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> None:
    """
    Отправляет готовый дайджест в Telegram через Bot API.
    Автоматически делит длинный текст на несколько сообщений.
    Если LLM сгенерировала битый HTML-тег, делает fallback-отправку обычным текстом.
    """
    load_dotenv()

    token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    target_chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()

    if not token:
        raise ValueError("Не задан TELEGRAM_BOT_TOKEN в .env")
    if not target_chat_id:
        raise ValueError("Не задан TELEGRAM_CHAT_ID в .env")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = split_message(text)

    with httpx.Client(timeout=20.0) as client:
        for idx, chunk in enumerate(chunks, start=1):
            payload = {
                "chat_id": target_chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            logger.info(
                "Отправка сообщения в Telegram (часть %d/%d, %d симв.)...",
                idx,
                len(chunks),
                len(chunk),
            )
            response = client.post(url, json=payload)

            if response.status_code == 400 and "can't parse entities" in response.text.lower():
                logger.warning(
                    "Telegram отклонил HTML-разметку в части %d/%d (%s). Повторная отправка без parse_mode...",
                    idx,
                    len(chunks),
                    response.text,
                )
                payload.pop("parse_mode", None)
                response = client.post(url, json=payload)

            if response.is_error:
                raise RuntimeError(
                    f"Ошибка Telegram API ({response.status_code}): {response.text}"
                )

    logger.info("Дайджест успешно отправлен в чат %s.", target_chat_id)
