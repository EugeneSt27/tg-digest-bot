from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from src.analyzer import build_digest
from src.fetcher import USER_AGENT, Post, fetch_channel_posts, normalize_channel_name
from src.storage import DEFAULT_STATE_PATH, load_state

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
EMPTY_DIGEST_TEXT = "📭 Новых постов за последние сутки не найдено."

logger = logging.getLogger(__name__)


@dataclass
class DigestResult:
    """Результат генерации дайджеста для одного профиля."""

    text: str
    posts: list[Post]
    is_empty: bool


def _load_raw_settings(settings_path: Path = SETTINGS_PATH) -> dict[str, Any]:
    """Читает и валидирует наличие YAML-файла конфигурации."""
    if not settings_path.exists():
        raise FileNotFoundError(f"Файл конфигурации не найден: {settings_path}")
    return yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}


def list_profiles(settings_path: Path = SETTINGS_PATH) -> list[str]:
    """Возвращает список имен профилей, объявленных в config/settings.yaml."""
    raw_cfg = _load_raw_settings(settings_path)
    profiles = raw_cfg.get("profiles") or {}
    return [str(name) for name in profiles.keys()]


def load_profile_config(profile_name: str, settings_path: Path = SETTINGS_PATH) -> dict[str, Any]:
    """
    Загружает настройки выбранного профиля из config/settings.yaml,
    объединяя их с секцией defaults.
    """
    raw_cfg = _load_raw_settings(settings_path)
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

    prompt_path = (settings_path.parent / prompt_rel_path).resolve()
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


def generate_digest(
    profile_cfg: dict[str, Any],
    days_lookback: int,
    no_state: bool = False,
) -> DigestResult:
    """
    Собирает посты из каналов профиля, при наличии постов генерирует дайджест
    через LLM и добавляет заголовок с датой. При отсутствии постов возвращает
    пустой результат без вызова LLM.
    """
    max_pages: int = profile_cfg["max_pages_per_channel"]
    channels: list[str] = profile_cfg["channels"]
    prompt_path: Path = profile_cfg["prompt_path"]

    state = {} if no_state else load_state(DEFAULT_STATE_PATH)
    all_posts: list[Post] = []

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
        follow_redirects=True,
        timeout=15.0,
    ) as http_client:
        for raw_channel in channels:
            norm_name = normalize_channel_name(raw_channel)
            last_seen_id = None if no_state else state.get(norm_name)
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
        return DigestResult(text=EMPTY_DIGEST_TEXT, posts=[], is_empty=True)

    logger.info("Всего собрано постов: %d. Генерация дайджеста через LLM...", len(all_posts))
    digest_body = build_digest(
        posts=all_posts,
        days_lookback=days_lookback,
        prompts_path=prompt_path,
    )

    today_str = datetime.now().strftime("%d.%m.%Y")
    header = f"📌 <b>{profile_cfg['description']} ({today_str})</b>\n\n"
    return DigestResult(text=header + digest_body, posts=all_posts, is_empty=False)
