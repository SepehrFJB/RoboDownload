from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.config import load_settings
from app.context import AppContext
from app.db import Database
from app.handlers import build_router
from app.logging_setup import configure_logging
from app.services.backup import BackupService
from app.services.cleanup import CleanupService
from app.services.download_manager import DownloadManager
from app.services.downloader import DownloaderService
from app.utils.rate_limiter import RateLimiter
from app.utils.request_store import RequestStore


async def run() -> None:
    settings = load_settings()
    configure_logging(log_dir=settings.log_dir, log_filename=settings.log_file_path.name)

    settings.download_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    db = Database(str(settings.db_path))
    await db.init()
    backfilled = await db.backfill_failed_error_categories()
    if backfilled:
        logging.getLogger(__name__).info('Backfilled %s failed download error categories', backfilled)
    await db.ensure_owner_admins(settings.admin_ids)
    stored_admin_ids = {
        int(item['user_id'])
        for item in await db.list_bot_admins()
    }
    effective_admin_ids = set(settings.admin_ids) | stored_admin_ids

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    platform_cookies = {
        str(item['platform']): str(item['cookie_text'])
        for item in await db.list_platform_cookies()
    }

    downloader = DownloaderService(
        download_dir=settings.download_dir,
        ffmpeg_binary=settings.ffmpeg_binary,
        platform_cookies=platform_cookies,
        ytdlp_js_runtimes=settings.ytdlp_js_runtimes,
        ytdlp_remote_components=settings.ytdlp_remote_components,
        probe_worker_threads=settings.probe_worker_threads,
        download_worker_threads=settings.download_worker_threads,
    )
    limiter = RateLimiter(settings.user_cooldown_seconds)
    request_store = RequestStore(settings.request_ttl_seconds)
    manager = DownloadManager(
        bot=bot,
        db=db,
        downloader=downloader,
        limiter=limiter,
        max_concurrent_downloads=settings.max_concurrent_downloads,
        max_file_size_mb=settings.max_file_size_mb,
        ffmpeg_binary=settings.ffmpeg_binary,
        download_timeout_seconds=settings.download_timeout_seconds,
        alert_enabled=settings.cookie_alert_enabled,
        alert_threshold=settings.cookie_alert_threshold,
        alert_window_minutes=settings.cookie_alert_window_minutes,
        alert_cooldown_minutes=settings.cookie_alert_cooldown_minutes,
        alert_recipient_ids=effective_admin_ids,
    )

    cleanup_service = CleanupService(settings.download_dir)
    cleanup_service.start()
    backup_service = BackupService(
        bot=bot,
        db=db,
        db_path=settings.db_path,
        backup_dir=settings.backup_dir,
        log_file_path=settings.log_file_path,
        enabled=settings.backup_enabled,
        daily_time=settings.backup_daily_time,
        backup_tz=settings.backup_tz,
        keep_count=settings.backup_keep_count,
    )
    backup_service.start()

    ctx = AppContext(
        db=db,
        request_store=request_store,
        limiter=limiter,
        downloader=downloader,
        manager=manager,
        backup_service=backup_service,
        admin_ids=effective_admin_ids,
        group_welcome_photo_path=settings.group_welcome_photo_path,
        youtube_max_duration_minutes=settings.youtube_max_duration_minutes,
        daily_user_success_limit=settings.daily_user_success_limit,
        daily_admin_success_limit=settings.daily_admin_success_limit,
        daily_global_success_limit=settings.daily_global_success_limit,
        daily_limit_reset_time=settings.daily_limit_reset_time,
        daily_limit_reset_tz=settings.daily_limit_reset_tz,
        daily_owner_unlimited=settings.daily_owner_unlimited,
        cookie_alert_enabled=settings.cookie_alert_enabled,
        cookie_alert_threshold=settings.cookie_alert_threshold,
        cookie_alert_window_minutes=settings.cookie_alert_window_minutes,
        cookie_alert_cooldown_minutes=settings.cookie_alert_cooldown_minutes,
    )

    dp.include_router(build_router(ctx))

    logger = logging.getLogger(__name__)
    logger.info('Bot started')

    try:
        await dp.start_polling(bot)
    finally:
        await manager.shutdown()
        await downloader.shutdown()
        await cleanup_service.stop()
        await backup_service.stop()
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(run())
