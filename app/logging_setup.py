from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DEFAULT_LOG_DIR = Path("logs")
DEFAULT_LOG_FILE = "robodownload.log"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
DEFAULT_BACKUP_COUNT = 5


def configure_logging(
    level: str | int = logging.INFO,
    log_dir: Path | str = DEFAULT_LOG_DIR,
    log_filename: str = DEFAULT_LOG_FILE,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Path:
    numeric_level = getattr(logging, level, logging.INFO) if isinstance(level, str) else int(level)
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)
    log_file_path = log_dir_path / log_filename

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Avoid duplicate handlers if configure_logging is called multiple times
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT)

    # Reconfigure stdout/stderr on systems (like Windows) where the default encoding may not be UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Console / Terminal Handler
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(numeric_level)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # Rotating File Handler with UTF-8 encoding
    file_handler = RotatingFileHandler(
        str(log_file_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    return log_file_path
