from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from aiogram.types import FSInputFile

from app.db import Database

logger = logging.getLogger(__name__)


class BackupService:
    def __init__(
        self,
        *,
        bot: Bot,
        db: Database,
        db_path: Path,
        backup_dir: Path,
        enabled: bool = True,
        daily_time: str = '03:30',
        backup_tz: str = 'Asia/Tehran',
        keep_count: int = 10,
    ) -> None:
        self._bot = bot
        self._db = db
        self._db_path = db_path
        self._backup_dir = backup_dir
        self._enabled = bool(enabled)
        self._daily_time = str(daily_time or '03:30').strip() or '03:30'
        self._backup_tz = str(backup_tz or 'Asia/Tehran').strip() or 'Asia/Tehran'
        self._keep_count = max(1, int(keep_count))
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        if not self._enabled:
            logger.info('Backup service is disabled')
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def send_backup_to_chat(self, *, chat_id: int, lang: str | None = None) -> Path:
        backup_file = await asyncio.to_thread(self._create_backup_file)
        try:
            selected_lang = lang if lang is not None else await self._db.get_user_language(chat_id)
            await self._send_backup_file(
                chat_id=chat_id,
                backup_file=backup_file,
                lang=selected_lang,
            )
            return backup_file
        finally:
            await asyncio.to_thread(self._cleanup_old_backups)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            delay = self._seconds_until_next_run()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

            try:
                backup_file = await asyncio.to_thread(self._create_backup_file)
                await self._send_backup_to_owners(backup_file)
                await asyncio.to_thread(self._cleanup_old_backups)
                logger.info('Database backup completed: %s', backup_file.name)
            except Exception:
                logger.exception('Backup cycle failed')

    def _seconds_until_next_run(self) -> float:
        hour, minute = _parse_time(self._daily_time)
        tz = _resolve_tz(self._backup_tz)
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(tz)
        target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target_local <= now_local:
            target_local = target_local + timedelta(days=1)
        delta = (target_local - now_local).total_seconds()
        return max(1.0, float(delta))

    def _create_backup_file(self) -> Path:
        if not self._db_path.exists():
            raise FileNotFoundError(f'Database file not found: {self._db_path}')

        tz = _resolve_tz(self._backup_tz)
        stamp = datetime.now(tz).strftime('%Y%m%d-%H%M%S')
        backup_path = self._backup_dir / f'bot-{stamp}.db'
        with sqlite3.connect(str(self._db_path)) as src:
            with sqlite3.connect(str(backup_path)) as dst:
                src.backup(dst)

        if not backup_path.exists() or backup_path.stat().st_size <= 0:
            raise RuntimeError('Backup file is empty or missing')
        return backup_path

    async def _send_backup_to_owners(self, backup_file: Path) -> None:
        admins = await self._db.list_bot_admins()
        owner_ids = sorted(
            {
                int(item['user_id'])
                for item in admins
                if str(item.get('role') or '').strip().lower() == 'owner'
            }
        )
        if not owner_ids:
            return

        for owner_id in owner_ids:
            try:
                lang = await self._db.get_user_language(owner_id)
                await self._send_backup_file(
                    chat_id=owner_id,
                    backup_file=backup_file,
                    lang=lang,
                )
            except Exception:
                logger.exception('Failed to send backup to owner %s', owner_id)

    async def _send_backup_file(self, *, chat_id: int, backup_file: Path, lang: str) -> None:
        input_file = FSInputFile(str(backup_file), filename=backup_file.name)
        normalized_lang = str(lang).strip().lower()
        timestamp_tz = timezone.utc if normalized_lang == 'en' else _resolve_tz(self._backup_tz)
        backup_timestamp = datetime.now(timestamp_tz).strftime('%Y-%m-%d %H:%M:%S')
        caption = _backup_caption(
            lang=lang,
            backup_filename=backup_file.name,
            backup_size_bytes=backup_file.stat().st_size,
            backup_timestamp=backup_timestamp,
        )
        await self._bot.send_document(
            chat_id=chat_id,
            document=input_file,
            caption=caption,
        )

    def _cleanup_old_backups(self) -> None:
        backups = sorted(
            (
                path for path in self._backup_dir.glob('bot-*.db')
                if path.is_file()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[self._keep_count :]:
            with contextlib.suppress(OSError):
                stale.unlink()


def _parse_time(raw: str) -> tuple[int, int]:
    value = str(raw or '').strip()
    if not value:
        return 3, 30
    parts = value.split(':')
    if len(parts) != 2:
        return 3, 30
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return 3, 30
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return 3, 30
    return hour, minute


def _resolve_tz(raw: str) -> tzinfo:
    tz_name = str(raw or '').strip() or 'UTC'
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        fixed = _fallback_fixed_tz(tz_name)
        if fixed is not None:
            return fixed
        return timezone.utc


def _fallback_fixed_tz(tz_name: str) -> tzinfo | None:
    lowered = str(tz_name or '').strip().lower()
    if lowered in {'utc', 'z', 'gmt'}:
        return timezone.utc
    if lowered == 'asia/tehran':
        return timezone(timedelta(hours=3, minutes=30))
    match = re.fullmatch(r'([+-])(\d{2}):?(\d{2})', lowered)
    if not match:
        return None
    sign = 1 if match.group(1) == '+' else -1
    hours = int(match.group(2))
    minutes = int(match.group(3))
    if hours > 23 or minutes > 59:
        return None
    return timezone(timedelta(hours=hours, minutes=minutes) * sign)


def _backup_caption(
    *,
    lang: str,
    backup_filename: str,
    backup_size_bytes: int,
    backup_timestamp: str,
) -> str:
    backup_size_kb = backup_size_bytes / 1024
    rendered_size = f'{backup_size_kb:.1f}'.rstrip('0').rstrip('.') + ' KB'
    if str(lang).strip().lower() == 'en':
        return (
            '🗂 Daily database backup\n'
            f'File: {backup_filename}\n'
            f'Size: {rendered_size}\n'
            f'UTC: {backup_timestamp}'
        )
    return (
        '🗂 بکاپ روزانه دیتابیس\n'
        f'فایل: {backup_filename}\n'
        f'حجم: {rendered_size}\n'
        f'تاریخ: {backup_timestamp}'
    )
