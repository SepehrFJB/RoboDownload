from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(slots=True)
class DailyWindow:
    start_utc: str
    end_utc: str
    reset_time_local: str
    reset_tz: str
    next_reset_local: str


def resolve_daily_window(reset_time: str, reset_tz: str) -> DailyWindow:
    hour, minute = _parse_reset_time(reset_time)
    tz, tz_label = _resolve_tz(reset_tz)

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    reset_point_today = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if now_local >= reset_point_today:
        start_local = reset_point_today
    else:
        start_local = reset_point_today - timedelta(days=1)
    end_local = start_local + timedelta(days=1)

    start_utc_dt = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc_dt = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    next_reset_local = end_local.strftime('%Y-%m-%d %H:%M')
    return DailyWindow(
        start_utc=start_utc_dt.strftime('%Y-%m-%d %H:%M:%S'),
        end_utc=end_utc_dt.strftime('%Y-%m-%d %H:%M:%S'),
        reset_time_local=f'{hour:02d}:{minute:02d}',
        reset_tz=tz_label,
        next_reset_local=next_reset_local,
    )


def _parse_reset_time(value: str) -> tuple[int, int]:
    raw = str(value or '').strip()
    if not raw:
        return 0, 0
    parts = raw.split(':')
    if len(parts) != 2:
        return 0, 0
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return 0, 0
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return 0, 0
    return hour, minute


def _resolve_tz(value: str) -> tuple[tzinfo, str]:
    tz_name = str(value or '').strip() or 'UTC'
    try:
        return ZoneInfo(tz_name), tz_name
    except ZoneInfoNotFoundError:
        pass

    fixed = _fallback_fixed_tz(tz_name)
    if fixed is not None:
        return fixed, tz_name

    return timezone.utc, 'UTC'


def _fallback_fixed_tz(tz_name: str) -> tzinfo | None:
    lowered = str(tz_name or '').strip().lower()
    if not lowered:
        return timezone.utc
    if lowered in {'utc', 'z', 'gmt'}:
        return timezone.utc
    if lowered == 'asia/tehran':
        return timezone(timedelta(hours=3, minutes=30))

    # Support values like +03:30, -04:00, +0330, -0400
    match = re.fullmatch(r'([+-])(\d{2}):?(\d{2})', lowered)
    if match:
        sign = 1 if match.group(1) == '+' else -1
        hours = int(match.group(2))
        minutes = int(match.group(3))
        if hours <= 23 and minutes <= 59:
            delta = timedelta(hours=hours, minutes=minutes) * sign
            return timezone(delta)
    return None
