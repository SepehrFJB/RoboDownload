from __future__ import annotations

import aiosqlite
import json
from contextlib import asynccontextmanager
from typing import Any

from app.models import DownloadMode
from app.models import Platform
from app.services.error_classifier import classify_download_error

COOKIE_PLATFORMS = {'youtube', 'instagram', 'tiktok', 'twitter', 'soundcloud'}


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._busy_timeout_ms = 15_000

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self._path)
        try:
            await db.execute('PRAGMA journal_mode=WAL')
            await db.execute(f'PRAGMA busy_timeout={self._busy_timeout_ms}')
            await db.execute('PRAGMA synchronous=NORMAL')
            yield db
        finally:
            await db.close()

    async def init(self) -> None:
        async with self._connect() as db:
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    default_mode TEXT NOT NULL DEFAULT 'video_720',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                '''
            )
            await self._ensure_users_lang_column(db)
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    file_size INTEGER,
                    error TEXT,
                    error_category TEXT,
                    error_hint TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                '''
            )
            await self._ensure_downloads_error_columns(db)
            await db.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_downloads_created_at
                ON downloads(created_at)
                '''
            )
            await db.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_downloads_platform
                ON downloads(platform)
                '''
            )
            await db.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_downloads_status
                ON downloads(status)
                '''
            )
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS required_channels (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    title TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                '''
            )
            await db.execute(
                '''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_required_channels_username
                ON required_channels(username)
                WHERE username IS NOT NULL
                '''
            )
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS group_chats (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    title TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                '''
            )
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                '''
            )
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS bot_admins (
                    user_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL CHECK(role IN ('owner', 'admin')),
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                '''
            )
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS blocked_targets (
                    target_id INTEGER PRIMARY KEY,
                    target_type TEXT NOT NULL CHECK(target_type IN ('user', 'group')),
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                '''
            )
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS platform_cookies (
                    platform TEXT PRIMARY KEY CHECK(platform IN ('youtube', 'instagram', 'tiktok', 'twitter', 'soundcloud')),
                    cookie_text TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                '''
            )
            await db.execute(
                '''
                CREATE TABLE IF NOT EXISTS download_cache (
                    cache_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    title TEXT NOT NULL,
                    quality_label TEXT,
                    artifacts_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                '''
            )
            await db.commit()

    @staticmethod
    async def _ensure_users_lang_column(db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(users)")
        rows = await cursor.fetchall()
        columns = {str(row[1]).lower() for row in rows}
        if 'lang' not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'fa'")

    @staticmethod
    async def _ensure_downloads_error_columns(db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(downloads)")
        rows = await cursor.fetchall()
        columns = {str(row[1]).lower() for row in rows}
        if 'error_category' not in columns:
            await db.execute("ALTER TABLE downloads ADD COLUMN error_category TEXT")
        if 'error_hint' not in columns:
            await db.execute("ALTER TABLE downloads ADD COLUMN error_hint TEXT")

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO users(user_id, username, first_name, updated_at)
                VALUES(?, ?, ?, datetime('now'))
                ON CONFLICT(user_id)
                DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    updated_at = datetime('now')
                ''',
                (user_id, username, first_name),
            )
            await db.commit()


    async def get_user_language(self, user_id: int) -> str:
        async with self._connect() as db:
            cursor = await db.execute('SELECT lang FROM users WHERE user_id = ?', (user_id,))
            row = await cursor.fetchone()
            if row is None:
                return 'fa'
            value = str(row[0] or '').strip().lower()
            if value in {'fa', 'en'}:
                return value
            return 'fa'

    async def set_user_language(self, user_id: int, lang: str) -> None:
        normalized = lang.strip().lower()
        if normalized not in {'fa', 'en'}:
            normalized = 'fa'

        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO users(user_id, lang, updated_at)
                VALUES(?, ?, datetime('now'))
                ON CONFLICT(user_id)
                DO UPDATE SET
                    lang = excluded.lang,
                    updated_at = datetime('now')
                ''',
                (user_id, normalized),
            )
            await db.commit()

    async def log_download(
        self,
        user_id: int,
        url: str,
        platform: str,
        mode: str,
        status: str,
        file_size: int | None,
        error: str | None,
        error_category: str | None = None,
        error_hint: str | None = None,
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO downloads(
                    user_id, url, platform, mode, status, file_size, error, error_category, error_hint
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    user_id,
                    url,
                    platform,
                    mode,
                    status,
                    file_size,
                    error,
                    str(error_category or '').strip().lower() or None,
                    str(error_hint or '').strip() or None,
                ),
            )
            await db.commit()


    async def get_detailed_stats(self) -> dict[str, Any]:
        async with self._connect() as db:
            total_users = await self._fetch_scalar(db, 'SELECT COUNT(*) FROM users')
            total_downloads = await self._fetch_scalar(db, 'SELECT COUNT(*) FROM downloads')
            successful = await self._fetch_scalar(
                db, "SELECT COUNT(*) FROM downloads WHERE status = 'success'"
            )
            failed = await self._fetch_scalar(
                db, "SELECT COUNT(*) FROM downloads WHERE status = 'failed'"
            )
            downloads_24h = await self._fetch_scalar(
                db,
                "SELECT COUNT(*) FROM downloads WHERE created_at >= datetime('now', '-1 day')",
            )
            downloads_7d = await self._fetch_scalar(
                db,
                "SELECT COUNT(*) FROM downloads WHERE created_at >= datetime('now', '-7 day')",
            )
            active_users_24h = await self._fetch_scalar(
                db,
                (
                    "SELECT COUNT(DISTINCT user_id) FROM downloads "
                    "WHERE created_at >= datetime('now', '-1 day')"
                ),
            )
            active_users_7d = await self._fetch_scalar(
                db,
                (
                    "SELECT COUNT(DISTINCT user_id) FROM downloads "
                    "WHERE created_at >= datetime('now', '-7 day')"
                ),
            )
            uploaded_bytes = await self._fetch_scalar(
                db,
                (
                    "SELECT COALESCE(SUM(file_size), 0) FROM downloads "
                    "WHERE status = 'success' AND file_size IS NOT NULL"
                ),
            )

            avg_cursor = await db.execute(
                (
                    "SELECT AVG(file_size) FROM downloads "
                    "WHERE status = 'success' AND file_size IS NOT NULL AND file_size > 0"
                )
            )
            avg_row = await avg_cursor.fetchone()
            avg_upload_bytes = int(avg_row[0]) if avg_row and avg_row[0] is not None else 0

            platform_cursor = await db.execute(
                '''
                SELECT platform, COUNT(*) AS cnt
                FROM downloads
                WHERE status = 'success'
                GROUP BY platform
                ORDER BY cnt DESC, platform ASC
                '''
            )
            platform_rows = await platform_cursor.fetchall()
            platform_counts = {
                str(row[0]): int(row[1])
                for row in platform_rows
                if row and row[0] is not None
            }

            error_cursor = await db.execute(
                '''
                SELECT COALESCE(NULLIF(lower(error_category), ''), 'unknown') AS category, COUNT(*) AS cnt
                FROM downloads
                WHERE status = 'failed'
                GROUP BY category
                ORDER BY cnt DESC, category ASC
                '''
            )
            error_rows = await error_cursor.fetchall()
            error_category_counts = {
                str(row[0]): int(row[1])
                for row in error_rows
                if row and row[0] is not None
            }

            error_24h_cursor = await db.execute(
                '''
                SELECT COALESCE(NULLIF(lower(error_category), ''), 'unknown') AS category, COUNT(*) AS cnt
                FROM downloads
                WHERE status = 'failed' AND created_at >= datetime('now', '-1 day')
                GROUP BY category
                ORDER BY cnt DESC, category ASC
                '''
            )
            error_24h_rows = await error_24h_cursor.fetchall()
            error_category_counts_24h = {
                str(row[0]): int(row[1])
                for row in error_24h_rows
                if row and row[0] is not None
            }

            last_cursor = await db.execute('SELECT MAX(created_at) FROM downloads')
            last_row = await last_cursor.fetchone()
            last_download_at = str(last_row[0]) if last_row and last_row[0] else None

        success_rate = (successful / total_downloads * 100.0) if total_downloads > 0 else 0.0
        return {
            'users': total_users,
            'downloads': total_downloads,
            'success': successful,
            'failed': failed,
            'success_rate': success_rate,
            'downloads_24h': downloads_24h,
            'downloads_7d': downloads_7d,
            'active_users_24h': active_users_24h,
            'active_users_7d': active_users_7d,
            'uploaded_bytes': uploaded_bytes,
            'avg_upload_bytes': avg_upload_bytes,
            'platform_counts': platform_counts,
            'error_category_counts': error_category_counts,
            'error_category_counts_24h': error_category_counts_24h,
            'last_download_at': last_download_at,
        }

    async def list_user_ids(self) -> list[int]:
        async with self._connect() as db:
            cursor = await db.execute('SELECT user_id FROM users ORDER BY created_at ASC')
            rows = await cursor.fetchall()
        return [int(row[0]) for row in rows]

    async def count_success_between(self, start_utc: str, end_utc: str) -> int:
        start_value = str(start_utc or '').strip()
        end_value = str(end_utc or '').strip()
        if not start_value or not end_value:
            return 0
        async with self._connect() as db:
            cursor = await db.execute(
                '''
                SELECT COUNT(*)
                FROM downloads
                WHERE status = 'success'
                  AND created_at >= ?
                  AND created_at < ?
                ''',
                (start_value, end_value),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def count_user_success_between(self, user_id: int, start_utc: str, end_utc: str) -> int:
        start_value = str(start_utc or '').strip()
        end_value = str(end_utc or '').strip()
        if not start_value or not end_value:
            return 0
        async with self._connect() as db:
            cursor = await db.execute(
                '''
                SELECT COUNT(*)
                FROM downloads
                WHERE user_id = ?
                  AND status = 'success'
                  AND created_at >= ?
                  AND created_at < ?
                ''',
                (int(user_id), start_value, end_value),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def count_failed_category_for_platform_recent(
        self,
        platform: str,
        error_category: str,
        window_minutes: int,
    ) -> int:
        normalized_platform = str(platform or '').strip().lower()
        normalized_category = str(error_category or '').strip().lower()
        minutes = max(1, int(window_minutes))
        if not normalized_platform or not normalized_category:
            return 0
        async with self._connect() as db:
            cursor = await db.execute(
                '''
                SELECT COUNT(*)
                FROM downloads
                WHERE status = 'failed'
                  AND lower(platform) = ?
                  AND COALESCE(NULLIF(lower(error_category), ''), 'unknown') = ?
                  AND created_at >= datetime('now', ?)
                ''',
                (normalized_platform, normalized_category, f'-{minutes} minutes'),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def get_user_detailed_stats(self, user_id: int) -> dict[str, Any]:
        target_id = int(user_id)
        async with self._connect() as db:
            user_cursor = await db.execute(
                '''
                SELECT user_id, username, first_name, created_at
                FROM users
                WHERE user_id = ?
                ''',
                (target_id,),
            )
            user_row = await user_cursor.fetchone()

            total_downloads = await self._fetch_scalar_with_params(
                db,
                'SELECT COUNT(*) FROM downloads WHERE user_id = ?',
                (target_id,),
            )
            successful = await self._fetch_scalar_with_params(
                db,
                "SELECT COUNT(*) FROM downloads WHERE user_id = ? AND status = 'success'",
                (target_id,),
            )
            failed = await self._fetch_scalar_with_params(
                db,
                "SELECT COUNT(*) FROM downloads WHERE user_id = ? AND status = 'failed'",
                (target_id,),
            )
            downloads_24h = await self._fetch_scalar_with_params(
                db,
                (
                    "SELECT COUNT(*) FROM downloads "
                    "WHERE user_id = ? AND created_at >= datetime('now', '-1 day')"
                ),
                (target_id,),
            )
            uploaded_bytes = await self._fetch_scalar_with_params(
                db,
                (
                    "SELECT COALESCE(SUM(file_size), 0) FROM downloads "
                    "WHERE user_id = ? AND status = 'success' AND file_size IS NOT NULL"
                ),
                (target_id,),
            )

            avg_cursor = await db.execute(
                (
                    "SELECT AVG(file_size) FROM downloads "
                    "WHERE user_id = ? AND status = 'success' AND file_size IS NOT NULL AND file_size > 0"
                ),
                (target_id,),
            )
            avg_row = await avg_cursor.fetchone()
            avg_upload_bytes = int(avg_row[0]) if avg_row and avg_row[0] is not None else 0

            platform_cursor = await db.execute(
                '''
                SELECT platform, COUNT(*) AS cnt
                FROM downloads
                WHERE user_id = ? AND status = 'success'
                GROUP BY platform
                ORDER BY cnt DESC, platform ASC
                ''',
                (target_id,),
            )
            platform_rows = await platform_cursor.fetchall()
            platform_counts = {
                str(row[0]): int(row[1])
                for row in platform_rows
                if row and row[0] is not None
            }

            error_cursor = await db.execute(
                '''
                SELECT COALESCE(NULLIF(lower(error_category), ''), 'unknown') AS category, COUNT(*) AS cnt
                FROM downloads
                WHERE user_id = ? AND status = 'failed'
                GROUP BY category
                ORDER BY cnt DESC, category ASC
                ''',
                (target_id,),
            )
            error_rows = await error_cursor.fetchall()
            error_category_counts = {
                str(row[0]): int(row[1])
                for row in error_rows
                if row and row[0] is not None
            }

        return {
            'exists': user_row is not None,
            'user_id': target_id,
            'username': str(user_row[1]) if user_row and user_row[1] else None,
            'first_name': str(user_row[2]) if user_row and user_row[2] else None,
            'created_at': str(user_row[3]) if user_row and user_row[3] else None,
            'downloads': total_downloads,
            'success': successful,
            'failed': failed,
            'downloads_24h': downloads_24h,
            'uploaded_bytes': uploaded_bytes,
            'avg_upload_bytes': avg_upload_bytes,
            'platform_counts': platform_counts,
            'error_category_counts': error_category_counts,
        }

    async def upsert_group_chat(
        self,
        chat_id: int,
        username: str | None,
        title: str | None,
    ) -> None:
        normalized_username = self._normalize_username(username)
        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO group_chats(chat_id, username, title, updated_at)
                VALUES(?, ?, ?, datetime('now'))
                ON CONFLICT(chat_id)
                DO UPDATE SET
                    username = excluded.username,
                    title = excluded.title,
                    updated_at = datetime('now')
                ''',
                (int(chat_id), normalized_username, title),
            )
            await db.commit()

    async def remove_group_chat(self, chat_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                'DELETE FROM group_chats WHERE chat_id = ?',
                (int(chat_id),),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def list_broadcast_group_chat_ids(self) -> list[int]:
        async with self._connect() as db:
            cursor = await db.execute(
                'SELECT chat_id FROM group_chats ORDER BY created_at ASC'
            )
            rows = await cursor.fetchall()
        return [int(row[0]) for row in rows]

    async def block_target(self, target_id: int, target_type: str) -> None:
        normalized_type = str(target_type).strip().lower()
        if normalized_type not in {'user', 'group'}:
            normalized_type = 'group' if int(target_id) < 0 else 'user'

        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO blocked_targets(target_id, target_type)
                VALUES(?, ?)
                ON CONFLICT(target_id)
                DO UPDATE SET target_type = excluded.target_type
                ''',
                (int(target_id), normalized_type),
            )
            await db.commit()

    async def unblock_target(self, target_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                'DELETE FROM blocked_targets WHERE target_id = ?',
                (int(target_id),),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def list_blocked_targets(self) -> list[dict[str, int | str]]:
        async with self._connect() as db:
            cursor = await db.execute(
                '''
                SELECT target_id, target_type
                FROM blocked_targets
                ORDER BY
                    CASE target_type WHEN 'group' THEN 0 ELSE 1 END ASC,
                    created_at ASC,
                    target_id ASC
                '''
            )
            rows = await cursor.fetchall()
        return [{'target_id': int(row[0]), 'target_type': str(row[1])} for row in rows]

    async def is_user_blocked(self, user_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT 1 FROM blocked_targets WHERE target_id = ? AND target_type = 'user' LIMIT 1",
                (int(user_id),),
            )
            row = await cursor.fetchone()
        return row is not None

    async def is_group_blocked(self, chat_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT 1 FROM blocked_targets WHERE target_id = ? AND target_type = 'group' LIMIT 1",
                (int(chat_id),),
            )
            row = await cursor.fetchone()
        return row is not None

    async def list_required_channels(self) -> list[dict[str, int | str | None]]:
        async with self._connect() as db:
            cursor = await db.execute(
                '''
                SELECT chat_id, username, title
                FROM required_channels
                ORDER BY created_at ASC
                '''
            )
            rows = await cursor.fetchall()

        channels: list[dict[str, int | str | None]] = []
        for row in rows:
            channels.append(
                {
                    'chat_id': int(row[0]),
                    'username': str(row[1]) if row[1] else None,
                    'title': str(row[2]) if row[2] else None,
                }
            )
        return channels

    async def add_required_channel(
        self,
        chat_id: int,
        username: str | None,
        title: str | None,
    ) -> None:
        normalized_username = self._normalize_username(username)
        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO required_channels(chat_id, username, title)
                VALUES(?, ?, ?)
                ON CONFLICT(chat_id)
                DO UPDATE SET
                    username = excluded.username,
                    title = excluded.title
                ''',
                (chat_id, normalized_username, title),
            )
            await db.commit()

    async def remove_required_channel(self, identifier: str) -> int:
        raw = identifier.strip()
        if not raw:
            return 0

        async with self._connect() as db:
            if raw.lstrip('-').isdigit():
                cursor = await db.execute(
                    'DELETE FROM required_channels WHERE chat_id = ?',
                    (int(raw),),
                )
            else:
                normalized_username = self._normalize_username(raw)
                cursor = await db.execute(
                    'DELETE FROM required_channels WHERE lower(username) = ?',
                    (normalized_username,),
                )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def is_force_sub_enabled(self) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT value FROM bot_settings WHERE key = 'force_sub_enabled'"
            )
            row = await cursor.fetchone()
        if row is None:
            return True
        value = str(row[0]).strip().lower()
        return value not in {'0', 'false', 'off', 'disabled'}

    async def set_force_sub_enabled(self, enabled: bool) -> None:
        value = '1' if enabled else '0'
        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO bot_settings(key, value)
                VALUES('force_sub_enabled', ?)
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value
                ''',
                (value,),
            )
            await db.commit()

    async def toggle_force_sub_enabled(self) -> bool:
        current = await self.is_force_sub_enabled()
        new_state = not current
        await self.set_force_sub_enabled(new_state)
        return new_state

    async def get_bot_setting(self, key: str) -> str | None:
        normalized_key = str(key or '').strip()
        if not normalized_key:
            return None
        async with self._connect() as db:
            cursor = await db.execute(
                'SELECT value FROM bot_settings WHERE key = ? LIMIT 1',
                (normalized_key,),
            )
            row = await cursor.fetchone()
        if not row or row[0] is None:
            return None
        return str(row[0])

    async def set_bot_setting(self, key: str, value: str) -> None:
        normalized_key = str(key or '').strip()
        if not normalized_key:
            return
        normalized_value = str(value or '').strip()
        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO bot_settings(key, value)
                VALUES(?, ?)
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value
                ''',
                (normalized_key, normalized_value),
            )
            await db.commit()

    async def ensure_owner_admins(self, owner_ids: set[int]) -> None:
        if not owner_ids:
            return
        async with self._connect() as db:
            for owner_id in owner_ids:
                await db.execute(
                    '''
                    INSERT INTO bot_admins(user_id, role)
                    VALUES(?, 'owner')
                    ON CONFLICT(user_id)
                    DO UPDATE SET role = 'owner'
                    ''',
                    (int(owner_id),),
                )
            await db.commit()

    async def list_bot_admins(self) -> list[dict[str, int | str]]:
        async with self._connect() as db:
            cursor = await db.execute(
                '''
                SELECT user_id, role
                FROM bot_admins
                ORDER BY
                    CASE role WHEN 'owner' THEN 0 ELSE 1 END ASC,
                    created_at ASC,
                    user_id ASC
                '''
            )
            rows = await cursor.fetchall()
        return [{'user_id': int(row[0]), 'role': str(row[1])} for row in rows]

    async def get_bot_admin_role(self, user_id: int) -> str | None:
        async with self._connect() as db:
            cursor = await db.execute(
                'SELECT role FROM bot_admins WHERE user_id = ?',
                (int(user_id),),
            )
            row = await cursor.fetchone()
        if not row or row[0] is None:
            return None
        role = str(row[0]).strip().lower()
        if role in {'owner', 'admin'}:
            return role
        return None

    async def upsert_bot_admin(self, user_id: int, role: str = 'admin') -> None:
        normalized_role = str(role).strip().lower()
        if normalized_role not in {'owner', 'admin'}:
            normalized_role = 'admin'
        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO bot_admins(user_id, role)
                VALUES(?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET role = excluded.role
                ''',
                (int(user_id), normalized_role),
            )
            await db.commit()

    async def remove_bot_admin(self, user_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                "DELETE FROM bot_admins WHERE user_id = ? AND role = 'admin'",
                (int(user_id),),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def list_platform_cookies(self) -> list[dict[str, str]]:
        async with self._connect() as db:
            cursor = await db.execute(
                '''
                SELECT platform, cookie_text, updated_at
                FROM platform_cookies
                ORDER BY
                    CASE platform
                        WHEN 'youtube' THEN 0
                        WHEN 'instagram' THEN 1
                        WHEN 'tiktok' THEN 2
                        WHEN 'twitter' THEN 3
                        WHEN 'soundcloud' THEN 4
                        ELSE 9
                    END ASC
                '''
            )
            rows = await cursor.fetchall()
        return [
            {
                'platform': str(row[0]),
                'cookie_text': str(row[1]),
                'updated_at': str(row[2]),
            }
            for row in rows
        ]

    async def get_platform_cookie(self, platform: str) -> str | None:
        normalized = self._normalize_cookie_platform(platform)
        if normalized is None:
            return None
        async with self._connect() as db:
            cursor = await db.execute(
                'SELECT cookie_text FROM platform_cookies WHERE platform = ?',
                (normalized,),
            )
            row = await cursor.fetchone()
        if not row or row[0] is None:
            return None
        return str(row[0])

    async def upsert_platform_cookie(self, platform: str, cookie_text: str) -> None:
        normalized = self._normalize_cookie_platform(platform)
        if normalized is None:
            raise ValueError('Unsupported cookie platform')
        payload = str(cookie_text or '').strip()
        if not payload:
            raise ValueError('Cookie text cannot be empty')

        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO platform_cookies(platform, cookie_text, updated_at)
                VALUES(?, ?, datetime('now'))
                ON CONFLICT(platform)
                DO UPDATE SET
                    cookie_text = excluded.cookie_text,
                    updated_at = datetime('now')
                ''',
                (normalized, payload),
            )
            await db.commit()

    async def remove_platform_cookie(self, platform: str) -> int:
        normalized = self._normalize_cookie_platform(platform)
        if normalized is None:
            return 0
        async with self._connect() as db:
            cursor = await db.execute(
                'DELETE FROM platform_cookies WHERE platform = ?',
                (normalized,),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def get_download_cache(self, cache_key: str) -> dict[str, Any] | None:
        key = str(cache_key or '').strip()
        if not key:
            return None

        async with self._connect() as db:
            cursor = await db.execute(
                '''
                SELECT cache_key, platform, mode, title, quality_label, artifacts_json, updated_at
                FROM download_cache
                WHERE cache_key = ?
                LIMIT 1
                ''',
                (key,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None

        artifacts_raw = str(row[5] or '[]')
        try:
            artifacts = json.loads(artifacts_raw)
        except json.JSONDecodeError:
            artifacts = []

        if not isinstance(artifacts, list):
            artifacts = []

        return {
            'cache_key': str(row[0]),
            'platform': str(row[1]),
            'mode': str(row[2]),
            'title': str(row[3]),
            'quality_label': str(row[4]) if row[4] is not None else None,
            'artifacts': artifacts,
            'updated_at': str(row[6]) if row[6] is not None else None,
        }

    async def upsert_download_cache(
        self,
        cache_key: str,
        platform: str,
        mode: str,
        title: str,
        quality_label: str | None,
        artifacts: list[dict[str, Any]],
    ) -> None:
        key = str(cache_key or '').strip()
        if not key:
            raise ValueError('cache_key is required')

        serialized_artifacts = json.dumps(artifacts, ensure_ascii=False)
        async with self._connect() as db:
            await db.execute(
                '''
                INSERT INTO download_cache(
                    cache_key, platform, mode, title, quality_label, artifacts_json, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(cache_key)
                DO UPDATE SET
                    platform = excluded.platform,
                    mode = excluded.mode,
                    title = excluded.title,
                    quality_label = excluded.quality_label,
                    artifacts_json = excluded.artifacts_json,
                    updated_at = datetime('now')
                ''',
                (
                    key,
                    str(platform).strip().lower(),
                    str(mode).strip().lower(),
                    str(title or '').strip() or 'Untitled',
                    quality_label,
                    serialized_artifacts,
                ),
            )
            await db.commit()

    async def remove_download_cache(self, cache_key: str) -> int:
        key = str(cache_key or '').strip()
        if not key:
            return 0

        async with self._connect() as db:
            cursor = await db.execute(
                'DELETE FROM download_cache WHERE cache_key = ?',
                (key,),
            )
            await db.commit()
            return int(cursor.rowcount or 0)

    async def backfill_failed_error_categories(self) -> int:
        async with self._connect() as db:
            await self._ensure_downloads_error_columns(db)
            await db.commit()
            cursor = await db.execute(
                '''
                SELECT id, platform, error
                FROM downloads
                WHERE status = 'failed'
                  AND (error_category IS NULL OR trim(error_category) = '')
                ORDER BY id ASC
                '''
            )
            rows = await cursor.fetchall()
            updated = 0
            for row in rows:
                row_id = int(row[0])
                platform_raw = str(row[1] or '').strip().lower()
                error_text = str(row[2] or '').strip()
                try:
                    platform_value = Platform(platform_raw)
                except ValueError:
                    platform_value = None
                classification = classify_download_error(error_text, platform=platform_value)
                await db.execute(
                    '''
                    UPDATE downloads
                    SET error_category = ?, error_hint = ?
                    WHERE id = ?
                    ''',
                    (
                        classification.category,
                        classification.hint_en,
                        row_id,
                    ),
                )
                updated += 1
            await db.commit()
            return updated

    @staticmethod
    def _normalize_username(username: str | None) -> str | None:
        if not username:
            return None
        value = username.strip().lstrip('@').lower()
        if not value:
            return None
        return value

    @staticmethod
    def _normalize_cookie_platform(platform: str | None) -> str | None:
        value = str(platform or '').strip().lower()
        if value in COOKIE_PLATFORMS:
            return value
        return None

    @staticmethod
    async def _fetch_scalar(db: aiosqlite.Connection, query: str) -> int:
        cursor = await db.execute(query)
        row = await cursor.fetchone()
        if row is None:
            return 0
        return int(row[0])

    @staticmethod
    async def _fetch_scalar_with_params(
        db: aiosqlite.Connection,
        query: str,
        params: tuple[Any, ...],
    ) -> int:
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        if row is None:
            return 0
        return int(row[0])

