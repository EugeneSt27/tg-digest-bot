from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Лимит Telegram — 4096 символов
MAX_MESSAGE_LENGTH = 4000


def get_bot_token(profile: Optional[str] = None) -> str:
    """
    Возвращает токен бота для указанного профиля.
    Приоритет:
    1. TELEGRAM_BOT_TOKEN_{PROFILE.upper()}
    2. TELEGRAM_BOT_TOKEN
    """
    load_dotenv()
    if profile:
        profile_key = f"TELEGRAM_BOT_TOKEN_{profile.upper()}"
        val = os.getenv(profile_key, "").strip()
        if val:
            return val
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def get_env_chat_id(profile: Optional[str] = None) -> Optional[str]:
    """
    Возвращает chat_id из .env для указанного профиля (канал или админ).
    Приоритет:
    1. TELEGRAM_CHAT_ID_{PROFILE.upper()}
    2. TELEGRAM_CHAT_ID
    """
    load_dotenv()
    if profile:
        profile_key = f"TELEGRAM_CHAT_ID_{profile.upper()}"
        val = os.getenv(profile_key, "").strip()
        if val:
            return val
    val = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return val if val else None


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


def send_single_chat(
    text: str,
    chat_id: str | int,
    bot_token: str,
    client: httpx.Client,
) -> None:
    """
    Отправляет текст в конкретный чат, разбивая его на части при необходимости.
    В случае ошибки парсинга HTML делает fallback-отправку без parse_mode.
    При других ошибках Telegram API возбуждает RuntimeError.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chunks = split_message(text)

    for idx, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if len(chunks) > 1:
            logger.debug(
                "Отправка сообщения в чат %s (часть %d/%d, %d симв.)...",
                chat_id,
                idx,
                len(chunks),
                len(chunk),
            )
        response = client.post(url, json=payload)

        if response.status_code == 400 and "can't parse entities" in response.text.lower():
            logger.warning(
                "Telegram отклонил HTML-разметку для чата %s в части %d/%d (%s). Повторная отправка без parse_mode...",
                chat_id,
                idx,
                len(chunks),
                response.text,
            )
            payload.pop("parse_mode", None)
            response = client.post(url, json=payload)

        if response.is_error:
            raise RuntimeError(
                f"Ошибка Telegram API ({response.status_code}) для чата {chat_id}: {response.text}"
            )


def broadcast_digest(
    text: str,
    chat_ids: list[str | int],
    bot_token: str,
    timeout: float = 20.0,
) -> dict[str, int]:
    """
    Рассылает дайджест по списку chat_id.
    Ошибки отправки конкретным пользователям (например, бот заблокирован) логируются,
    рассылка остальным получателям продолжается.
    Возвращает статистику вида: {"success": int, "failed": int}.
    """
    if not bot_token:
        raise ValueError("Не передан токен Telegram бота для рассылки.")

    # Дедупликация списка получателей с сохранением порядка
    unique_chat_ids: list[str | int] = []
    seen = set()
    for cid in chat_ids:
        if cid is not None and str(cid).strip() and str(cid) not in seen:
            seen.add(str(cid))
            unique_chat_ids.append(cid)

    if not unique_chat_ids:
        logger.warning("Список получателей для рассылки пуст.")
        return {"success": 0, "failed": 0}

    logger.info("Начало рассылки дайджеста по %d получателям...", len(unique_chat_ids))

    success_count = 0
    fail_count = 0

    with httpx.Client(timeout=timeout) as client:
        for cid in unique_chat_ids:
            try:
                send_single_chat(
                    text=text,
                    chat_id=cid,
                    bot_token=bot_token,
                    client=client,
                )
                success_count += 1
                logger.info("Успешно отправлено в чат: %s", cid)
            except Exception as exc:
                fail_count += 1
                logger.warning("Не удалось отправить дайджест в чат %s: %s", cid, exc)

    logger.info(
        "Рассылка завершена: успешно — %d, с ошибками — %d (всего адресатов: %d)",
        success_count,
        fail_count,
        len(unique_chat_ids),
    )
    return {"success": success_count, "failed": fail_count}


def send_telegram_message(
    text: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    profile: Optional[str] = None,
) -> None:
    """
    Отправляет дайджест в один чат (для обратной совместимости).
    Если bot_token или chat_id не указаны, они подтягиваются с учетом профиля.
    """
    token = (bot_token or get_bot_token(profile)).strip()
    target_chat_id = (chat_id or get_env_chat_id(profile) or "").strip()

    if not token:
        raise ValueError("Не задан TELEGRAM_BOT_TOKEN")
    if not target_chat_id:
        raise ValueError("Не задан TELEGRAM_CHAT_ID")

    with httpx.Client(timeout=20.0) as client:
        send_single_chat(
            text=text,
            chat_id=target_chat_id,
            bot_token=token,
            client=client,
        )

    logger.info("Дайджест успешно отправлен в чат %s.", target_chat_id)
