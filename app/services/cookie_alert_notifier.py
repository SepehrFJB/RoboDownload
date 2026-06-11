from __future__ import annotations

import time

from aiogram import Bot

from app.db import Database
from app.models import Platform


async def maybe_send_cookie_expiry_alert(
    *,
    bot: Bot,
    db: Database,
    platform: Platform,
    enabled: bool,
    threshold: int,
    window_minutes: int,
    cooldown_minutes: int,
    default_recipient_ids: set[int] | None = None,
) -> None:
    if not enabled:
        return
    if platform not in {
        Platform.YOUTUBE,
        Platform.INSTAGRAM,
        Platform.TIKTOK,
        Platform.TWITTER,
        Platform.SOUNDCLOUD,
    }:
        return

    threshold_value = max(1, int(threshold))
    window_value = max(1, int(window_minutes))
    cooldown_seconds = max(0, int(cooldown_minutes) * 60)
    recent_count = await db.count_failed_category_for_platform_recent(
        platform=platform.value,
        error_category='cookie_required',
        window_minutes=window_value,
    )
    if recent_count < threshold_value:
        return

    setting_key = f'cookie_alert_last_sent:{platform.value}'
    now = int(time.time())
    last_raw = await db.get_bot_setting(setting_key)
    if last_raw:
        try:
            last_ts = int(float(last_raw))
        except ValueError:
            last_ts = 0
        if cooldown_seconds > 0 and (now - last_ts) < cooldown_seconds:
            return

    recipients = await _resolve_recipients(db=db, fallback_ids=default_recipient_ids or set())
    if not recipients:
        return

    platform_label = _platform_label(platform)
    sent_any = False
    for chat_id in recipients:
        try:
            lang = await db.get_user_language(chat_id)
            text = _cookie_alert_text(
                lang=lang,
                platform_label=platform_label,
                window_value=window_value,
                recent_count=recent_count,
            )
            await bot.send_message(chat_id=chat_id, text=text)
            sent_any = True
        except Exception:
            continue

    if sent_any:
        await db.set_bot_setting(setting_key, str(now))


async def _resolve_recipients(db: Database, fallback_ids: set[int]) -> list[int]:
    admins = await db.list_bot_admins()
    admin_ids = [
        int(item['user_id'])
        for item in admins
        if str(item.get('role') or '').strip().lower() in {'owner', 'admin'}
    ]
    if admin_ids:
        return sorted(set(admin_ids))
    if fallback_ids:
        return sorted({int(user_id) for user_id in fallback_ids})
    return []


def _platform_label(platform: Platform) -> str:
    if platform == Platform.YOUTUBE:
        return 'YouTube'
    if platform == Platform.INSTAGRAM:
        return 'Instagram'
    if platform == Platform.TIKTOK:
        return 'TikTok'
    if platform == Platform.TWITTER:
        return 'X/Twitter'
    if platform == Platform.SOUNDCLOUD:
        return 'SoundCloud'
    return platform.value


def _cookie_alert_text(
    *,
    lang: str,
    platform_label: str,
    window_value: int,
    recent_count: int,
) -> str:
    if str(lang).strip().lower() == 'en':
        return (
            '⚠️ Cookie alert\n\n'
            f'Platform: {platform_label}\n'
            f'Cookie errors in the last {window_value} minute(s): {recent_count}\n'
            'Cookie might be expired and may need to be refreshed.'
        )
    return (
        '⚠️ هشدار کوکی\n\n'
        f'پلتفرم: {platform_label}\n'
        f'تعداد خطای کوکی در {window_value} دقیقه اخیر: {recent_count}\n'
        'احتمال انقضای کوکی وجود دارد.'
    )
