from __future__ import annotations

from typing import Any


DEFAULT_LANG = 'fa'
SUPPORTED_LANGS = {'fa', 'en'}

_MESSAGES: dict[str, dict[str, str]] = {
    'fa': {
        'intro_text': (
            'ربات دانلودر پیشرفته\n\n'
            'لینک YouTube، Instagram، SoundCloud، TikTok یا Twitter/X را بفرست تا کیفیت را انتخاب کنی.\n'
            'امکانات:\n'
            '- دانلود ویدیو یوتیوب\n'
            '- استخراج MP3\n'
            '- صف دانلود + نمایش پیشرفت\n'
            '- آمار ادمین\n\n'
            'برای هر لینک، کیفیت را همان‌جا انتخاب کن.'
        ),
        'admins_only': 'فقط ادمین.',
        'supported_urls': 'لینک‌های پشتیبانی‌شده: YouTube, Instagram, SoundCloud, TikTok, Twitter/X.',
        'invalid_message_or_link': (
            '⛔️ پیام یا لینک اشتباه ارسال شده است.\n'
            'ربات از Instagram، YouTube، TikTok، Twitter/X و SoundCloud پشتیبانی می‌کند.'
        ),
        'reading_media': 'در حال خواندن اطلاعات رسانه...',
        'cannot_process_url': 'امکان پردازش این لینک نیست.\nدلیل: {reason}',
        'cannot_process_url_with_hint': (
            'امکان پردازش این لینک نیست.\n'
            'دلیل: {reason}\n'
            '{hint}'
        ),
        'cannot_process_url_simple_hint': 'امکان پردازش این لینک نیست.\n{hint}',
        'could_not_read_url': 'اطلاعات این لینک خوانده نشد. عمومی بودن و صحت لینک را بررسی کن.',
        'media_info': (
            'عنوان: {title}\n'
            'آپلودر: {uploader}\n'
            'مدت: {duration}\n'
            'پلتفرم: {platform}\n\n'
            'کیفیت را انتخاب کن:'
        ),
        'invalid_action': 'عملیات نامعتبر است',
        'request_expired': 'این درخواست منقضی شده. لینک را دوباره بفرست.',
        'button_other_user': 'این دکمه برای کاربر دیگری است.',
        'unsupported_mode': 'حالت دانلود پشتیبانی نمی‌شود',
        'mode_unavailable_instagram': 'این حالت برای Instagram قابل استفاده نیست.',
        'mode_unavailable_tiktok': 'این حالت برای TikTok قابل استفاده نیست.',
        'mode_unavailable_twitter': 'این حالت برای Twitter/X قابل استفاده نیست.',
        'mode_unavailable_soundcloud': 'این حالت برای SoundCloud قابل استفاده نیست.',
        'rate_limited': 'درخواست‌ها زیاد است. کمی بعد دوباره تلاش کن.',
        'rate_limited_active_job': (
            '🚧 هم‌اکنون یک دانلود فعال دارید.\n'
            'لطفاً بعد از اتمام دانلود فعلی دوباره تلاش کنید.'
        ),
        'rate_limited_wait': 'لطفا {seconds} ثانیه صبر کن و دوباره تلاش کن.',
        'daily_limit_user_reached': (
            'سهمیه روزانه شما تمام شده است.\n'
            'امروز: {used}/{limit}\n'
            'ریست: {reset_at} ({tz})'
        ),
        'daily_limit_admin_reached': (
            'سهمیه روزانه مدیر تمام شده است.\n'
            'امروز: {used}/{limit}\n'
            'ریست: {reset_at} ({tz})'
        ),
        'daily_limit_global_reached': (
            'ظرفیت روزانه ربات تکمیل شده است.\n'
            'امروز: {used}/{limit}\n'
            'ریست: {reset_at} ({tz})'
        ),
        'daily_limit_unlimited': 'بدون محدودیت',
        'queued_preparing': 'در صف: {mode}\nدر حال آماده‌سازی دانلود...',
        'queued_short': 'در صف قرار گرفت',
        'bot_stats': (
            'آمار ربات\n'
            'کاربران: {users}\n'
            'دانلودها: {downloads}\n'
            'موفق: {success}\n'
            'ناموفق: {failed}'
        ),
        'lang_choose': 'زبان ربات را انتخاب کن:',
        'lang_saved': 'زبان ذخیره شد.',
        'lang_invalid': 'زبان نامعتبر است',
        'lang_fa': 'فارسی',
        'lang_en': 'English',
        'force_sub_required': 'برای استفاده از ربات، اول باید در کانال‌های زیر عضو بشی:',
        'force_sub_check': '✅ بررسی عضویت',
        'force_sub_ok': 'عضویت تایید شد. حالا می‌تونی از ربات استفاده کنی.',
        'force_sub_still_missing': 'هنوز عضو همه کانال‌ها نشدی.',
        'force_sub_add_usage': 'یوزرنیم یا لینک کانال را بفرست.',
        'force_sub_remove_usage': 'یوزرنیم یا chat_id کانال را بفرست.',
        'force_sub_added': '✅ کانال با موفقیت به لیست عضویت اجباری اضافه شد.',
        'force_sub_removed': '✅ کانال با موفقیت از لیست عضویت اجباری حذف شد.',
        'force_sub_not_found': 'کانالی با این مشخصات پیدا نشد.',
        'force_sub_public_only': 'فقط کانال‌های عمومی (دارای یوزرنیم) قابل اضافه شدن هستند.',
        'force_sub_cannot_read': 'امکان خواندن این کانال نیست. بات را ادمین کن و دوباره تلاش کن.',
        'force_sub_private_unavailable': 'لینک عضویت این کانال در دسترس نیست. لطفاً با ادمین ربات تماس بگیر.',
        'force_sub_list_empty': 'هیچ کانالی برای عضویت اجباری ثبت نشده.',
        'force_sub_list_header': 'لیست کانال‌های عضویت اجباری:\n{items}',
        'broadcast_usage': (
            'برای پیام همگانی:\n'
            '1) روی یک پیام ریپلای کن و /broadcast بزن\n'
            'یا\n'
            '2) /broadcast متن پیام'
        ),
        'broadcast_no_users': 'هیچ کاربری برای ارسال پیدا نشد.',
        'broadcast_started': 'ارسال همگانی شروع شد...\nکل کاربران: {total}',
        'broadcast_done': (
            'ارسال همگانی تمام شد.\n'
            'موفق: {sent}\n'
            'بلاک/غیرفعال: {blocked}\n'
            'ناموفق: {failed}'
        ),
        'download_started_fetching': 'دانلود شروع شد. در حال دریافت فایل...',
        'download_timeout_reached': '⛔️ دانلود بیش از {minutes} دقیقه طول کشید و متوقف شد. دوباره تلاش کنید.',
        'finished_summary': (
            'تمام شد: {sent_files} فایل ارسال شد، {skipped_files} فایل رد شد.\n'
            'حجم ارسال‌شده: {sent_bytes}'
        ),
        'failed_download_reason': 'دانلود ناموفق بود.\nدلیل: {reason}',
        'failed_download_reason_with_hint': (
            'دانلود ناموفق بود.\n'
            'دلیل: {reason}\n'
            '{hint}'
        ),
        'failed_download_simple_hint': 'دانلود ناموفق بود.\n{hint}',
        'unexpected_error_processing': 'خطای غیرمنتظره هنگام پردازش این درخواست.',
        'unexpected_error_processing_with_hint': (
            'خطای غیرمنتظره هنگام پردازش این درخواست.\n'
            '{hint}'
        ),
        'skipped_file_size': 'فایل {name} رد شد چون حجم {size} از محدودیت {limit} بیشتر است.',
        'progress_downloading': (
            'در حال دانلود: {progress}\n'
            'حجم: {ratio}\n'
            'سرعت: {speed}\n'
            'زمان باقی‌مانده: {eta}'
        ),
        'progress_finished': 'دانلود تمام شد، در حال آماده‌سازی ارسال...',
        'progress_upload_started': 'ارسال شروع شد...',
    },
    'en': {
        'intro_text': (
            'Advanced media downloader bot\n\n'
            'Send a YouTube, Instagram, SoundCloud, TikTok, or Twitter/X URL and choose format.\n'
            'Features:\n'
            '- YouTube video download\n'
            '- MP3 extraction\n'
            '- Download queue + progress updates\n'
            '- Admin stats\n\n'
            'Send a supported link to choose quality for that link.'
        ),
        'admins_only': 'Admins only.',
        'supported_urls': 'Supported URLs: YouTube, Instagram, SoundCloud, TikTok, Twitter/X.',
        'invalid_message_or_link': (
            '⛔️ An incorrect message or link was sent.\n'
            'The bot supports downloads from Instagram, YouTube, TikTok, Twitter/X, and SoundCloud.'
        ),
        'reading_media': 'Reading media info...',
        'cannot_process_url': 'Cannot process this URL.\nReason: {reason}',
        'cannot_process_url_with_hint': (
            'Cannot process this URL.\n'
            'Reason: {reason}\n'
            '{hint}'
        ),
        'cannot_process_url_simple_hint': 'Cannot process this URL.\n{hint}',
        'could_not_read_url': 'Could not read this URL. Make sure it is public and valid.',
        'media_info': (
            'Title: {title}\n'
            'Uploader: {uploader}\n'
            'Duration: {duration}\n'
            'Platform: {platform}\n\n'
            'Choose format:'
        ),
        'invalid_action': 'Invalid action',
        'request_expired': 'This request expired. Send the URL again.',
        'button_other_user': 'This button belongs to another user.',
        'unsupported_mode': 'Unsupported mode',
        'mode_unavailable_instagram': 'This mode is unavailable for Instagram.',
        'mode_unavailable_tiktok': 'This mode is unavailable for TikTok.',
        'mode_unavailable_twitter': 'This mode is unavailable for Twitter/X.',
        'mode_unavailable_soundcloud': 'This mode is unavailable for SoundCloud.',
        'rate_limited': 'Too many requests. Please try again shortly.',
        'rate_limited_active_job': (
            '🚧 You are currently downloading one file.\n'
            'Please try again after your current download is finished.'
        ),
        'rate_limited_wait': 'Please wait {seconds}s before starting a new request.',
        'daily_limit_user_reached': (
            'Your daily quota is reached.\n'
            'Today: {used}/{limit}\n'
            'Reset: {reset_at} ({tz})'
        ),
        'daily_limit_admin_reached': (
            'Daily admin quota is reached.\n'
            'Today: {used}/{limit}\n'
            'Reset: {reset_at} ({tz})'
        ),
        'daily_limit_global_reached': (
            'Bot daily capacity is reached.\n'
            'Today: {used}/{limit}\n'
            'Reset: {reset_at} ({tz})'
        ),
        'daily_limit_unlimited': 'Unlimited',
        'queued_preparing': 'Queued: {mode}\nPreparing download...',
        'queued_short': 'Queued',
        'bot_stats': (
            'Bot stats\n'
            'Users: {users}\n'
            'Downloads: {downloads}\n'
            'Success: {success}\n'
            'Failed: {failed}'
        ),
        'lang_choose': 'Choose bot language:',
        'lang_saved': 'Language saved.',
        'lang_invalid': 'Invalid language',
        'lang_fa': 'Persian',
        'lang_en': 'English',
        'force_sub_required': 'To use the bot, first join these channels:',
        'force_sub_check': '✅ Check membership',
        'force_sub_ok': 'Membership verified. You can use the bot now.',
        'force_sub_still_missing': 'You have not joined all required channels yet.',
        'force_sub_add_usage': 'Send channel username or link.',
        'force_sub_remove_usage': 'Send channel username or chat_id.',
        'force_sub_added': '✅ Channel was added to force-sub list successfully.',
        'force_sub_removed': '✅ Channel was removed from force-sub list successfully.',
        'force_sub_not_found': 'Channel not found in required list.',
        'force_sub_public_only': 'Only public channels (with username) can be added.',
        'force_sub_cannot_read': 'Cannot read this channel. Add the bot as admin and try again.',
        'force_sub_private_unavailable': 'This channel join link is unavailable. Please contact the bot admin.',
        'force_sub_list_empty': 'No required channels are configured.',
        'force_sub_list_header': 'Required channels:\n{items}',
        'broadcast_usage': (
            'Broadcast usage:\n'
            '1) Reply to a message then send /broadcast\n'
            'or\n'
            '2) /broadcast your text'
        ),
        'broadcast_no_users': 'No users found for broadcast.',
        'broadcast_started': 'Broadcast started...\nTotal users: {total}',
        'broadcast_done': (
            'Broadcast finished.\n'
            'Sent: {sent}\n'
            'Blocked/Inactive: {blocked}\n'
            'Failed: {failed}'
        ),
        'download_started_fetching': 'Download started. Fetching media...',
        'download_timeout_reached': '⛔️ Download exceeded {minutes} minutes and was stopped. Please try again.',
        'finished_summary': (
            'Finished: {sent_files} file(s) sent, {skipped_files} skipped.\n'
            'Total uploaded: {sent_bytes}'
        ),
        'failed_download_reason': 'Failed to download media.\nReason: {reason}',
        'failed_download_reason_with_hint': (
            'Failed to download media.\n'
            'Reason: {reason}\n'
            '{hint}'
        ),
        'failed_download_simple_hint': 'Failed to download media.\n{hint}',
        'unexpected_error_processing': 'Unexpected error while processing this request.',
        'unexpected_error_processing_with_hint': (
            'Unexpected error while processing this request.\n'
            '{hint}'
        ),
        'skipped_file_size': 'Skipped {name} because size {size} is above limit {limit}.',
        'progress_downloading': (
            'Downloading: {progress}\n'
            'Size: {ratio}\n'
            'Speed: {speed}\n'
            'ETA: {eta}'
        ),
        'progress_finished': 'Download complete, preparing upload...',
        'progress_upload_started': 'Upload started...',
    },
}


def normalize_lang(lang: str | None) -> str:
    if not lang:
        return DEFAULT_LANG
    lowered = lang.strip().lower()
    if lowered in SUPPORTED_LANGS:
        return lowered
    return DEFAULT_LANG


def tr(lang: str | None, key: str, **kwargs: Any) -> str:
    normalized = normalize_lang(lang)
    template = _MESSAGES.get(normalized, _MESSAGES[DEFAULT_LANG]).get(key)
    if template is None:
        template = _MESSAGES[DEFAULT_LANG].get(key, key)
    try:
        return template.format(**kwargs)
    except Exception:
        return template

