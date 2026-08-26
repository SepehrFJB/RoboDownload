from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.models import DownloadMode, Platform
from app.utils.mode_policy import default_modes_for_platform, youtube_mode_sort_key


MODE_LABELS: dict[str, dict[DownloadMode, str]] = {
    'fa': {
        DownloadMode.VIDEO_1080: '🎬 1080p',
        DownloadMode.VIDEO_720: '🎬 720p',
        DownloadMode.VIDEO_480: '🎬 480p',
        DownloadMode.VIDEO_360: '🎬 360p',
        DownloadMode.VIDEO_240: '🎬 240p',
        DownloadMode.AUDIO_MP3: '🎵 Audio',
        DownloadMode.BEST: '⭐ بهترین کیفیت',
    },
    'en': {
        DownloadMode.VIDEO_1080: '🎬 1080p',
        DownloadMode.VIDEO_720: '🎬 720p',
        DownloadMode.VIDEO_480: '🎬 480p',
        DownloadMode.VIDEO_360: '🎬 360p',
        DownloadMode.VIDEO_240: '🎬 240p',
        DownloadMode.AUDIO_MP3: '🎵 Audio',
        DownloadMode.BEST: '⭐ Best Quality',
    },
}

ADMIN_BUTTONS: dict[str, dict[str, str]] = {
    'fa': {
        'inspect_user': '🔎 بررسی کاربر',
        'broadcast': '✍️ پیام همگانی',
        'cookie': '🍪 کوکی',
        'cookie_list': '📜 لیست کوکی‌ها',
        'cookie_set': '➕ افزودن/ویرایش کوکی',
        'cookie_remove': '➖ حذف کوکی',
        'broadcast_target_users': '👤 کاربران',
        'broadcast_target_groups': '👥 گروه‌ها',
        'broadcast_mode_normal': '📩 عادی',
        'broadcast_mode_forward': '↗️ فوروارد',
        'stats': '📊 آمار',
        'fsub_menu': '🥷 اجبار عضویت',
        'admins_menu': '👨‍✈️ مدیران',
        'admins_list': '📜 لیست مدیران',
        'admins_add': '➕ افزودن مدیر',
        'admins_remove': '➖ حذف مدیر',
        'block_target': '⛔️ مسدود کردن کاربر یا گروه',
        'blocks_list': '📜 لیست مسدودی‌ها',
        'blocks_add': '➕ افزودن مسدودی',
        'blocks_remove': '➖ حذف مسدودی',
        'database_backup': '📥 دریافت دیتابیس و لاگ',
        'fsub_status': '📊 وضعیت',
        'fsub_add': '💠 افزودن کانال',
        'fsub_remove': '♨️ حذف کانال',
        'fsub_toggle_disable': '⛔ غیرفعالسازی',
        'fsub_toggle_enable': '✅ فعالسازی',
        'back': '🔙 بازگشت',
        'home': '🏠 خانه',
        'cancel': '❌ لغو عملیات',
    },
    'en': {
        'inspect_user': '🔎 Check User',
        'broadcast': '✍️ Broadcast',
        'cookie': '🍪 Cookie',
        'cookie_list': '📜 Cookie list',
        'cookie_set': '➕ Add/Update cookie',
        'cookie_remove': '➖ Remove cookie',
        'broadcast_target_users': '👤 Users',
        'broadcast_target_groups': '👥 Groups',
        'broadcast_mode_normal': '📩 Normal',
        'broadcast_mode_forward': '↗️ Forward',
        'stats': '📊 Stats',
        'fsub_menu': '🥷 Force Join',
        'admins_menu': '👨‍✈️ Admins',
        'admins_list': '📜 Managers list',
        'admins_add': '➕ Add manager',
        'admins_remove': '➖ Remove manager',
        'block_target': '⛔️ Block User or Group',
        'blocks_list': '📜 Blocked list',
        'blocks_add': '➕ Add block',
        'blocks_remove': '➖ Remove block',
        'database_backup': '📥 Get Database & Logs',
        'fsub_status': '📊 Status',
        'fsub_add': '💠 Add channel',
        'fsub_remove': '♨️ Remove channel',
        'fsub_toggle_disable': '⛔ Disable',
        'fsub_toggle_enable': '✅ Enable',
        'back': '🔙 Back',
        'home': '🏠 Home',
        'cancel': '❌ Cancel',
    },
}

COOKIE_PLATFORM_LABELS: dict[str, dict[str, str]] = {
    'fa': {
        'youtube': '▶️ یوتیوب',
        'instagram': '📸 اینستاگرام',
        'tiktok': '🎬 تیک‌تاک',
        'twitter': '🔷 توییتر',
        'soundcloud': '🎵 ساندکلاد',
    },
    'en': {
        'youtube': '▶️ YouTube',
        'instagram': '📸 Instagram',
        'tiktok': '🎬 TikTok',
        'twitter': '🔷 Twitter',
        'soundcloud': '🎵 SoundCloud',
    },
}


def mode_label(mode: DownloadMode, lang: str) -> str:
    normalized = _normalize_lang(lang)
    return MODE_LABELS[normalized][mode]


def build_download_options(
    request_id: str,
    platform: Platform,
    lang: str,
    modes: list[DownloadMode] | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    default_modes = default_modes_for_platform(platform)
    selected_modes = list(modes) if modes is not None else list(default_modes)
    if platform == Platform.YOUTUBE:
        selected_modes.sort(key=youtube_mode_sort_key)

    for mode in selected_modes:
        label = mode_label(mode, lang)
        kb.button(text=label, callback_data=f'dl:{request_id}:{mode.value}')

    has_audio = DownloadMode.AUDIO_MP3 in selected_modes
    if platform == Platform.YOUTUBE:
        video_modes = [mode for mode in selected_modes if mode != DownloadMode.AUDIO_MP3]
        row_pattern_list: list[int] = []
        if len(video_modes) >= 2:
            row_pattern_list.extend([2] * (len(video_modes) // 2))
            if len(video_modes) % 2:
                row_pattern_list.append(1)
        elif len(video_modes) == 1:
            row_pattern_list.append(1)
        if has_audio:
            row_pattern_list.append(1)
        if not row_pattern_list:
            row_pattern_list.append(1)
        row_pattern = tuple(row_pattern_list)
    elif len(selected_modes) == 2 and has_audio:
        row_pattern = (1, 1)
    elif len(selected_modes) >= 4:
        row_pattern = (2, 1, 1)
    elif len(selected_modes) == 3:
        row_pattern = (2, 1)
    elif len(selected_modes) == 2:
        row_pattern = (2,)
    else:
        row_pattern = (1,)

    kb.adjust(*row_pattern)
    return kb.as_markup()


def build_language_keyboard(current_lang: str) -> InlineKeyboardMarkup:
    current = _normalize_lang(current_lang)
    kb = InlineKeyboardBuilder()

    fa_text = '🇮🇷 فارسی'
    en_text = '🇬🇧 English'
    if current == 'fa':
        fa_text = f'✅ {fa_text}'
    else:
        en_text = f'✅ {en_text}'

    kb.button(text=fa_text, callback_data='lang:set:fa')
    kb.button(text=en_text, callback_data='lang:set:en')
    kb.adjust(2)
    return kb.as_markup()


def build_force_sub_keyboard(
    channels: list[dict[str, str]],
    check_label: str,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    for channel in channels:
        title = channel.get('title') or channel.get('username') or 'Channel'
        url = channel.get('url') or ''
        if url:
            kb.button(text=title, url=url)
        else:
            kb.button(text=title, callback_data='force:unavailable')

    kb.button(text=check_label, callback_data='force:check')
    kb.adjust(1)
    return kb.as_markup()


def admin_button_text(name: str, lang: str) -> str:
    normalized = _normalize_lang(lang)
    return ADMIN_BUTTONS[normalized][name]


def all_admin_button_variants(name: str) -> set[str]:
    return {
        ADMIN_BUTTONS['fa'][name],
        ADMIN_BUTTONS['en'][name],
    }


def force_sub_toggle_button_variants() -> set[str]:
    return (
        all_admin_button_variants('fsub_toggle_disable')
        | all_admin_button_variants('fsub_toggle_enable')
    )


def broadcast_mode_button_variants(mode_name: str) -> set[str]:
    return {
        ADMIN_BUTTONS['fa'][mode_name],
        ADMIN_BUTTONS['en'][mode_name],
    }


def broadcast_target_button_variants(target_name: str) -> set[str]:
    return {
        ADMIN_BUTTONS['fa'][target_name],
        ADMIN_BUTTONS['en'][target_name],
    }


def cookie_platform_button_variants(platform: str) -> set[str]:
    key = str(platform).strip().lower()
    return {
        COOKIE_PLATFORM_LABELS['fa'].get(key, key),
        COOKIE_PLATFORM_LABELS['en'].get(key, key),
    }


def cookie_platform_label(platform: str, lang: str) -> str:
    normalized = _normalize_lang(lang)
    key = str(platform).strip().lower()
    return COOKIE_PLATFORM_LABELS[normalized].get(key, key)


def build_admin_panel_keyboard(lang: str) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=admin_button_text('inspect_user', lang))
    kb.button(text=admin_button_text('cookie', lang))
    kb.button(text=admin_button_text('stats', lang))
    kb.button(text=admin_button_text('broadcast', lang))
    kb.button(text=admin_button_text('fsub_menu', lang))
    kb.button(text=admin_button_text('admins_menu', lang))
    kb.button(text=admin_button_text('block_target', lang))
    kb.button(text=admin_button_text('database_backup', lang))
    kb.adjust(3, 3, 2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_force_sub_admin_keyboard(lang: str, force_sub_enabled: bool = True) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=admin_button_text('fsub_status', lang))
    kb.button(text=admin_button_text('fsub_remove', lang))
    kb.button(text=admin_button_text('fsub_add', lang))
    toggle_key = 'fsub_toggle_disable' if force_sub_enabled else 'fsub_toggle_enable'
    kb.button(text=admin_button_text(toggle_key, lang))
    kb.button(text=admin_button_text('back', lang))
    kb.adjust(1, 2, 2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_force_sub_action_keyboard(lang: str) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=admin_button_text('home', lang))
    kb.button(text=admin_button_text('back', lang))
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_back_only_keyboard(lang: str) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=admin_button_text('back', lang))
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_broadcast_mode_keyboard(lang: str) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=admin_button_text('broadcast_mode_forward', lang))
    kb.button(text=admin_button_text('broadcast_mode_normal', lang))
    kb.button(text=admin_button_text('home', lang))
    kb.button(text=admin_button_text('back', lang))
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_broadcast_target_keyboard(lang: str) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=admin_button_text('broadcast_target_users', lang))
    kb.button(text=admin_button_text('broadcast_target_groups', lang))
    kb.button(text=admin_button_text('back', lang))
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_admin_managers_keyboard(lang: str) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=admin_button_text('admins_list', lang))
    kb.button(text=admin_button_text('admins_remove', lang))
    kb.button(text=admin_button_text('admins_add', lang))
    kb.button(text=admin_button_text('back', lang))
    kb.adjust(1, 2, 1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_admin_cookie_keyboard(lang: str) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=admin_button_text('cookie_list', lang))
    kb.button(text=admin_button_text('cookie_remove', lang))
    kb.button(text=admin_button_text('cookie_set', lang))
    kb.button(text=admin_button_text('back', lang))
    kb.adjust(1, 2, 1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_cookie_platform_keyboard(lang: str, platforms: list[str]) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    valid_platforms = [str(item).strip().lower() for item in platforms if str(item).strip()]
    for platform in valid_platforms:
        kb.button(text=cookie_platform_label(platform, lang))
    kb.button(text=admin_button_text('home', lang))
    kb.button(text=admin_button_text('back', lang))

    row_sizes: list[int] = [2] * (len(valid_platforms) // 2)
    if len(valid_platforms) % 2:
        row_sizes.append(1)
    row_sizes.append(2)
    kb.adjust(*row_sizes)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_admin_blocks_keyboard(lang: str) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=admin_button_text('blocks_list', lang))
    kb.button(text=admin_button_text('blocks_remove', lang))
    kb.button(text=admin_button_text('blocks_add', lang))
    kb.button(text=admin_button_text('back', lang))
    kb.adjust(1, 2, 1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_admin_remove_keyboard(lang: str, admin_user_ids: list[int]) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for user_id in admin_user_ids:
        kb.button(text=str(abs(int(user_id))))
    kb.button(text=admin_button_text('back', lang))

    row_sizes: list[int] = [2] * (len(admin_user_ids) // 2)
    if len(admin_user_ids) % 2:
        row_sizes.append(1)
    row_sizes.append(1)
    kb.adjust(*row_sizes)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_block_remove_keyboard(lang: str, target_ids: list[int]) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for target_id in target_ids:
        kb.button(text=str(int(target_id)))
    kb.button(text=admin_button_text('back', lang))

    row_sizes: list[int] = [2] * (len(target_ids) // 2)
    if len(target_ids) % 2:
        row_sizes.append(1)
    row_sizes.append(1)
    kb.adjust(*row_sizes)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_force_sub_remove_keyboard(
    lang: str,
    channels: list[dict[str, int | str | None]],
) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for channel in channels:
        chat_id = channel.get('chat_id')
        if chat_id is None:
            continue
        kb.button(text=str(abs(int(chat_id))))

    kb.button(text=admin_button_text('home', lang))
    kb.button(text=admin_button_text('back', lang))

    channel_count = sum(1 for channel in channels if channel.get('chat_id') is not None)
    row_sizes: list[int] = [2] * (channel_count // 2)
    if channel_count % 2:
        row_sizes.append(1)
    row_sizes.append(2)
    kb.adjust(*row_sizes)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=False)


def _normalize_lang(lang: str | None) -> str:
    if str(lang).strip().lower() == 'en':
        return 'en'
    return 'fa'

