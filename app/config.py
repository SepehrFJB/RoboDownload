from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DOWNLOAD_DIR = BASE_DIR / 'downloads'
STATIC_DB_PATH = BASE_DIR / 'data' / 'bot.db'
STATIC_BACKUP_DIR = BASE_DIR / 'backups'
STATIC_LOG_DIR = BASE_DIR / 'logs'
STATIC_LOG_FILE_PATH = STATIC_LOG_DIR / 'robodownload.log'

STATIC_DAILY_RESET_TIME = '00:00'
STATIC_DAILY_RESET_TZ = 'Asia/Tehran'
STATIC_DAILY_OWNER_UNLIMITED = True

STATIC_BACKUP_ENABLED = True
STATIC_BACKUP_TIME = '03:30'
STATIC_BACKUP_TZ = 'Asia/Tehran'
STATIC_BACKUP_KEEP_COUNT = 10


@dataclass(slots=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    download_dir: Path
    db_path: Path
    backup_dir: Path
    log_dir: Path
    log_file_path: Path
    group_welcome_photo_path: Path | None
    max_concurrent_downloads: int
    max_file_size_mb: int
    youtube_max_duration_minutes: int
    daily_user_success_limit: int
    daily_admin_success_limit: int
    daily_global_success_limit: int
    daily_limit_reset_time: str
    daily_limit_reset_tz: str
    daily_owner_unlimited: bool
    cookie_alert_enabled: bool
    cookie_alert_threshold: int
    cookie_alert_window_minutes: int
    cookie_alert_cooldown_minutes: int
    backup_enabled: bool
    backup_daily_time: str
    backup_tz: str
    backup_keep_count: int
    request_ttl_seconds: int
    user_cooldown_seconds: int
    ffmpeg_binary: str
    ytdlp_js_runtimes: tuple[str, ...]
    ytdlp_remote_components: tuple[str, ...]
    probe_worker_threads: int
    download_worker_threads: int
    download_timeout_seconds: int


def _parse_admin_ids(raw: str) -> set[int]:
    if not raw.strip():
        return set()
    ids: set[int] = set()
    for item in raw.split(','):
        item = item.strip()
        if not item:
            continue
        ids.add(int(item))
    return ids


def _parse_csv_values(raw: str) -> tuple[str, ...]:
    value = str(raw or '').strip()
    if not value:
        return ()
    items = [item.strip() for item in value.split(',')]
    normalized = tuple(item for item in items if item)
    return normalized


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv('BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('BOT_TOKEN is required')

    download_dir = STATIC_DOWNLOAD_DIR
    db_path = STATIC_DB_PATH
    backup_dir = STATIC_BACKUP_DIR
    log_dir = STATIC_LOG_DIR
    log_file_path = STATIC_LOG_FILE_PATH

    group_welcome_photo_raw = os.getenv('GROUP_WELCOME_PHOTO_PATH', '').strip()
    group_welcome_photo_path = (BASE_DIR / group_welcome_photo_raw).resolve() if group_welcome_photo_raw else None

    return Settings(
        bot_token=token,
        admin_ids=_parse_admin_ids(os.getenv('ADMIN_IDS', '')),
        download_dir=download_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        log_dir=log_dir,
        log_file_path=log_file_path,
        group_welcome_photo_path=group_welcome_photo_path,
        max_concurrent_downloads=int(os.getenv('MAX_CONCURRENT_DOWNLOADS', '3')),
        max_file_size_mb=int(os.getenv('MAX_FILE_SIZE_MB', '49')),
        youtube_max_duration_minutes=int(os.getenv('YOUTUBE_MAX_DURATION_MINUTES', '30')),
        daily_user_success_limit=int(os.getenv('DAILY_USER_SUCCESS_LIMIT', '50')),
        daily_admin_success_limit=int(os.getenv('DAILY_ADMIN_SUCCESS_LIMIT', '50')),
        daily_global_success_limit=int(os.getenv('DAILY_GLOBAL_SUCCESS_LIMIT', '5000')),
        daily_limit_reset_time=STATIC_DAILY_RESET_TIME,
        daily_limit_reset_tz=STATIC_DAILY_RESET_TZ,
        daily_owner_unlimited=STATIC_DAILY_OWNER_UNLIMITED,
        cookie_alert_enabled=os.getenv('COOKIE_ALERT_ENABLED', '1').strip().lower() not in {'0', 'false', 'off'},
        cookie_alert_threshold=int(os.getenv('COOKIE_ALERT_THRESHOLD', '3')),
        cookie_alert_window_minutes=int(os.getenv('COOKIE_ALERT_WINDOW_MINUTES', '60')),
        cookie_alert_cooldown_minutes=int(os.getenv('COOKIE_ALERT_COOLDOWN_MINUTES', '300')),
        backup_enabled=STATIC_BACKUP_ENABLED,
        backup_daily_time=STATIC_BACKUP_TIME,
        backup_tz=STATIC_BACKUP_TZ,
        backup_keep_count=STATIC_BACKUP_KEEP_COUNT,
        request_ttl_seconds=int(os.getenv('REQUEST_TTL_SECONDS', '900')),
        user_cooldown_seconds=int(os.getenv('USER_COOLDOWN_SECONDS', '10')),
        ffmpeg_binary=os.getenv('FFMPEG_BINARY', 'ffmpeg').strip() or 'ffmpeg',
        ytdlp_js_runtimes=_parse_csv_values(os.getenv('YTDLP_JS_RUNTIMES', 'node')),
        ytdlp_remote_components=_parse_csv_values(os.getenv('YTDLP_REMOTE_COMPONENTS', 'ejs:github')),
        probe_worker_threads=max(1, int(os.getenv('PROBE_WORKER_THREADS', '4'))),
        download_worker_threads=max(1, int(os.getenv('DOWNLOAD_WORKER_THREADS', '6'))),
        download_timeout_seconds=max(0, int(os.getenv('DOWNLOAD_TIMEOUT_SECONDS', '600'))),
    )
