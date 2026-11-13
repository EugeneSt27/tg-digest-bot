from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.storage import atomic_write_text

if TYPE_CHECKING:
    from src.pipeline import DigestResult

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"


@dataclass
class CachedDigest:
    """Закэшированный дневной дайджест профиля."""

    profile: str
    generated_at: float
    posts_count: int
    is_empty: bool
    text: str


def get_cache_path(profile: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    """Возвращает путь к файлу кэша для указанного профиля."""
    return cache_dir / f"{profile}_day_cache.json"


def get_cached_day_digest(
    profile: str,
    max_age_hours: float = 5.0,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> CachedDigest | None:
    """
    Возвращает закэшированный дневной дайджест, если файл валиден и
    его возраст строго меньше max_age_hours. При ошибке чтения/парсинга
    или истечении срока возвращает None.
    """
    if max_age_hours <= 0:
        return None

    cache_path = get_cache_path(profile, cache_dir=cache_dir)
    if not cache_path.exists():
        return None

    try:
        raw_text = cache_path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return None
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Не удалось прочитать кэш %s (%s). Игнорируем.", cache_path, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("Некорректный формат кэша %s (ожидался JSON-объект).", cache_path)
        return None

    raw_generated_at = data.get("generated_at")
    raw_text_val = data.get("text")
    raw_is_empty = data.get("is_empty")
    raw_posts_count = data.get("posts_count", 0)

    if (
        isinstance(raw_generated_at, bool)
        or not isinstance(raw_generated_at, (int, float))
        or not isinstance(raw_text_val, str)
        or not isinstance(raw_is_empty, bool)
        or isinstance(raw_posts_count, bool)
        or not isinstance(raw_posts_count, int)
    ):
        logger.warning("Некорректные поля в файле кэша %s. Игнорируем.", cache_path)
        return None

    generated_at = float(raw_generated_at)
    if time.time() - generated_at >= max_age_hours * 3600:
        return None

    return CachedDigest(
        profile=str(data.get("profile") or profile),
        generated_at=generated_at,
        posts_count=raw_posts_count,
        is_empty=raw_is_empty,
        text=raw_text_val,
    )


def save_day_digest_cache(
    profile: str,
    result: DigestResult,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    generated_at: float | None = None,
) -> Path:
    """
    Атомарно сохраняет результат генерации дневного дайджеста в кэш
    (включая пустой результат).
    """
    cache_path = get_cache_path(profile, cache_dir=cache_dir)
    payload = {
        "profile": profile,
        "generated_at": time.time() if generated_at is None else float(generated_at),
        "posts_count": len(result.posts),
        "is_empty": result.is_empty,
        "text": result.text,
    }
    content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(cache_path, content)
    logger.info("Кэш дневного дайджеста сохранен: %s (posts=%d, is_empty=%s)", cache_path, len(result.posts), result.is_empty)
    return cache_path
