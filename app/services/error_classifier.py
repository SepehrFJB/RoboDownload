from __future__ import annotations

from dataclasses import dataclass
import re

from app.models import Platform

ERROR_CATEGORIES: tuple[str, ...] = (
    'cookie_required',
    'rate_limited',
    'not_found',
    'geo_blocked',
    'private_content',
    'telegram_limit',
    'network_timeout',
    'unknown',
)

ERROR_HINTS_FA: dict[str, str] = {
    'cookie_required': 'این پلتفرم به کوکی معتبر نیاز دارد.',
    'rate_limited': 'محدودیت درخواست فعال شده؛ کمی بعد دوباره تلاش کنید.',
    'not_found': 'لینک نامعتبر است یا محتوا حذف شده.',
    'geo_blocked': 'این محتوا در منطقه سرور در دسترس نیست.',
    'private_content': 'محتوا خصوصی است یا نیاز به دسترسی دارد.',
    'telegram_limit': 'حجم فایل برای ارسال در تلگرام مجاز نیست.',
    'network_timeout': 'ارتباط شبکه ناپایدار است؛ دوباره تلاش کنید.',
    'unknown': 'دلیل دقیق مشخص نیست؛ بعدا دوباره تست کنید.',
}

ERROR_HINTS_EN: dict[str, str] = {
    'cookie_required': 'This platform requires a valid cookie.',
    'rate_limited': 'Rate limit is active. Please try again later.',
    'not_found': 'URL is invalid or content was removed.',
    'geo_blocked': 'This content is not available in the server region.',
    'private_content': 'Content is private or requires access.',
    'telegram_limit': 'File size exceeds Telegram sending limits.',
    'network_timeout': 'Network connection is unstable. Try again later.',
    'unknown': 'Exact reason is unknown. Please retry later.',
}

ERROR_LABELS_FA: dict[str, str] = {
    'cookie_required': 'نیاز به کوکی',
    'rate_limited': 'محدودیت درخواست',
    'not_found': 'یافت نشد',
    'geo_blocked': 'محدودیت منطقه‌ای',
    'private_content': 'محتوای خصوصی',
    'telegram_limit': 'محدودیت تلگرام',
    'network_timeout': 'خطای شبکه/تایم‌اوت',
    'unknown': 'نامشخص',
}

ERROR_LABELS_EN: dict[str, str] = {
    'cookie_required': 'Cookie required',
    'rate_limited': 'Rate limited',
    'not_found': 'Not found',
    'geo_blocked': 'Geo blocked',
    'private_content': 'Private content',
    'telegram_limit': 'Telegram limit',
    'network_timeout': 'Network timeout',
    'unknown': 'Unknown',
}


@dataclass(slots=True)
class ErrorClassification:
    category: str
    hint_fa: str
    hint_en: str

    def hint(self, lang: str) -> str:
        if str(lang).strip().lower() == 'en':
            return self.hint_en
        return self.hint_fa


def classify_download_error(reason: str, platform: Platform | None = None) -> ErrorClassification:
    text = str(reason or '').strip()
    lowered = text.lower()

    if _has_any(
        lowered,
        (
            'cookie',
            'cookies',
            'sign in',
            'sign-in',
            'ytdlp_cookies_file',
            'cookies-from-browser',
            'cookies from browser',
            'set ytdlp_cookies_file',
            'confirm you’re not a bot',
            'confirm you are not a bot',
            'login required',
            'to login',
            'redirecting anonymous requests to login',
            'authentication required',
            'use --cookies',
        ),
    ):
        return _build('cookie_required')

    if _has_any(
        lowered,
        (
            '429',
            'too many requests',
            'rate limit',
            'rate-limited',
            'try again later',
        ),
    ):
        return _build('rate_limited')

    if _has_any(
        lowered,
        (
            'not available in your country',
            'not available in this country',
            'geo',
            'region blocked',
            'country',
        ),
    ):
        return _build('geo_blocked')

    if _has_any(
        lowered,
        (
            'private',
            'private video',
            'private account',
            'this post is private',
            'members only',
            'subscriber-only',
            'requires login',
            'login to view',
        ),
    ):
        return _build('private_content')

    if _has_any(
        lowered,
        (
            'file is too big',
            'request entity too large',
            'message is too long',
            'telegram',
            'bad request: file',
        ),
    ):
        return _build('telegram_limit')

    if _has_any(
        lowered,
        (
            'timed out',
            'timeout',
            'connection reset',
            'connection aborted',
            'network is unreachable',
            'temporary failure',
            'name resolution',
            'dns',
            'ssl',
        ),
    ):
        return _build('network_timeout')

    if _has_any(
        lowered,
        (
            '404',
            'not found',
            'video unavailable',
            'does not exist',
            'unavailable',
            'removed',
            'deleted',
        ),
    ):
        return _build('not_found')

    # Platform-biased fallback: SoundCloud 404 metadata issues are very often URL/content-not-found.
    if platform == Platform.SOUNDCLOUD and _has_any(lowered, ('http error 404', 'unable to download json')):
        return _build('not_found')

    return _build('unknown')


def error_category_label(category: str, lang: str) -> str:
    normalized = str(category or 'unknown').strip().lower()
    if normalized not in ERROR_CATEGORIES:
        normalized = 'unknown'
    if str(lang).strip().lower() == 'en':
        return ERROR_LABELS_EN[normalized]
    return ERROR_LABELS_FA[normalized]


def should_hide_technical_reason(category: str) -> bool:
    normalized = str(category or 'unknown').strip().lower()
    return normalized in ERROR_CATEGORIES and normalized != 'unknown'


def compact_error_reason(reason: str, limit: int = 220) -> str:
    text = str(reason or '').strip()
    if not text:
        return ''
    # Remove ANSI color/format sequences from yt-dlp errors.
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    text = text.replace('\r', ' ').replace('\n', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + '…'
    return text


def _build(category: str) -> ErrorClassification:
    normalized = str(category or 'unknown').strip().lower()
    if normalized not in ERROR_CATEGORIES:
        normalized = 'unknown'
    return ErrorClassification(
        category=normalized,
        hint_fa=ERROR_HINTS_FA[normalized],
        hint_en=ERROR_HINTS_EN[normalized],
    )


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(token in text for token in needles)
