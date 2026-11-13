from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 МБ
DEFAULT_LOG_BACKUP_COUNT = 5


def setup_logging(
    log_filename: str,
    logs_dir: Path = DEFAULT_LOGS_DIR,
    level: int = logging.INFO,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> Path:
    """
    Настраивает логирование одновременно в консоль и в файл с ротацией.
    Возвращает путь к файлу лога.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / log_filename

    stream_handler = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[stream_handler, file_handler],
        force=True,
    )
    # Не засоряем лог каждые 30 секунд URL-ами с токеном при long polling
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return log_path
