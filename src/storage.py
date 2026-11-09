from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.fetcher import Post

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "state.json"
DEFAULT_DIGESTS_DIR = Path(__file__).resolve().parent.parent / "data" / "digests"


def load_state(state_path: Path = DEFAULT_STATE_PATH) -> dict[str, int]:
    """
    Загружает словарь {channel_username: last_seen_post_id} из JSON-файла.
    Если файла нет или он поврежден, возвращает пустой словарь.
    """
    if not state_path.exists():
        logger.info("Файл состояния %s не найден, начинаем с чистого листа.", state_path)
        return {}

    try:
        raw_text = state_path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return {}
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            logger.warning("Некорректный формат %s (ожидался JSON-объект). Сброс состояния.", state_path)
            return {}

        # Гарантируем, что ключи — строки, а значения — int
        clean_state: dict[str, int] = {}
        for channel, post_id in data.items():
            try:
                clean_state[str(channel)] = int(post_id)
            except (ValueError, TypeError):
                logger.warning("Пропуск некорректной записи в state.json: %s -> %s", channel, post_id)
        return clean_state
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Не удалось прочитать %s (%s). Начинаем с пустого состояния.", state_path, exc)
        return {}


def update_state_with_posts(state: dict[str, int], posts: list[Post]) -> dict[str, int]:
    """
    Возвращает копию словаря состояния, обновленную максимальными post_id
    из списка собранных постов.
    """
    updated = dict(state)
    for post in posts:
        current_max = updated.get(post.channel, 0)
        if post.post_id > current_max:
            updated[post.channel] = post.post_id
    return updated


def save_state(state: dict[str, int], state_path: Path = DEFAULT_STATE_PATH) -> None:
    """
    Атомарно сохраняет словарь состояния в JSON-файл (через временный файл),
    чтобы не повредить данные при внезапном прерывании.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(".json.tmp")
    content = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(state_path)
    logger.info("Состояние успешно сохранено в %s (%d каналов).", state_path, len(state))


def save_digest_to_archive(
    profile: str,
    text: str,
    days_lookback: int,
    posts_count: int = 0,
    digests_dir: Path = DEFAULT_DIGESTS_DIR,
) -> Path:
    """
    Сохраняет отправленный дайджест в markdown-файл в папку data/digests/.
    Формат имени: {profile}_digest_{dd}_{mm}_{yyyy}_{days_lookback}_days_before.md
    """
    digests_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    date_str = now.strftime("%d_%m_%Y")
    base_filename = f"{profile}_digest_{date_str}_{days_lookback}_days_before.md"
    file_path = digests_dir / base_filename

    # Если файл с таким именем уже существует,
    # добавляем время, чтобы не перезатереть историю
    if file_path.exists():
        time_str = now.strftime("%H%M%S")
        file_path = digests_dir / f"{profile}_digest_{date_str}_{time_str}_{days_lookback}_days_before.md"

    metadata_header = (
        f"<!--\n"
        f"Дата отправки: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Профиль: {profile}\n"
        f"Глубина поиска: {days_lookback} дн.\n"
        f"Обработано постов: {posts_count}\n"
        f"-->\n\n"
    )

    content = metadata_header + text.strip() + "\n"
    file_path.write_text(content, encoding="utf-8")
    logger.info("Дайджест сохранен в архив: %s", file_path)
    return file_path
