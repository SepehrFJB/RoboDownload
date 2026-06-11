from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.db import Database
from app.services.backup import BackupService
from app.services.download_manager import DownloadManager
from app.services.downloader import DownloaderService
from app.utils.rate_limiter import RateLimiter
from app.utils.request_store import RequestStore


@dataclass(slots=True)
class AppContext:
    db: Database
    request_store: RequestStore
    limiter: RateLimiter
    downloader: DownloaderService
    manager: DownloadManager
    backup_service: BackupService
    admin_ids: set[int]
    group_welcome_photo_path: Path | None
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
