from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
import time
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.filters import Command, Filter
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, ChatMemberUpdated, FSInputFile, Message, ReplyKeyboardRemove

from app.context import AppContext
from app.i18n import tr
from app.keyboards import (
    all_admin_button_variants,
    broadcast_mode_button_variants,
    broadcast_target_button_variants,
    cookie_platform_button_variants,
    cookie_platform_label,
    build_admin_panel_keyboard,
    build_admin_cookie_keyboard,
    build_admin_managers_keyboard,
    build_admin_blocks_keyboard,
    build_admin_remove_keyboard,
    build_back_only_keyboard,
    build_block_remove_keyboard,
    build_broadcast_mode_keyboard,
    build_broadcast_target_keyboard,
    build_cookie_platform_keyboard,
    build_force_sub_action_keyboard,
    build_force_sub_admin_keyboard,
    build_force_sub_remove_keyboard,
    build_download_options,
    build_force_sub_keyboard,
    build_language_keyboard,
    force_sub_toggle_button_variants,
    mode_label,
)
from app.models import DownloadMode, DownloadRequest, PendingRequest, Platform
from app.services.error_classifier import (
    ERROR_CATEGORIES,
    classify_download_error,
    compact_error_reason,
    error_category_label,
    should_hide_technical_reason,
)
from app.services.cookie_alert_notifier import maybe_send_cookie_expiry_alert
from app.services.downloader import DownloaderError
from app.utils.formatting import human_bytes, human_duration, safe_caption
from app.utils.mode_policy import default_modes_for_platform, is_video_resolution_mode
from app.utils.daily_window import resolve_daily_window
from app.utils.request_store import build_request_id
from app.utils.url_tools import detect_platform, extract_first_url, is_supported_url

logger = logging.getLogger(__name__)

ADMIN_STATE_WAIT_BROADCAST_TARGET = 'wait_broadcast_target'
ADMIN_STATE_WAIT_BROADCAST_MODE = 'wait_broadcast_mode'
ADMIN_STATE_WAIT_BROADCAST_NORMAL = 'wait_broadcast_normal'
ADMIN_STATE_WAIT_BROADCAST_FORWARD = 'wait_broadcast_forward'
ADMIN_STATE_WAIT_USER_INSPECT = 'wait_user_inspect'
ADMIN_STATE_WAIT_ADMIN_ADD = 'wait_admin_add'
ADMIN_STATE_WAIT_ADMIN_REMOVE = 'wait_admin_remove'
ADMIN_STATE_WAIT_BLOCK_ADD = 'wait_block_add'
ADMIN_STATE_WAIT_BLOCK_REMOVE = 'wait_block_remove'
ADMIN_STATE_WAIT_FSUB_ADD = 'wait_fsub_add'
ADMIN_STATE_WAIT_FSUB_REMOVE = 'wait_fsub_remove'
ADMIN_STATE_WAIT_COOKIE_SET_PLATFORM = 'wait_cookie_set_platform'
ADMIN_STATE_WAIT_COOKIE_SET_CONTENT = 'wait_cookie_set_content'
ADMIN_STATE_WAIT_COOKIE_REMOVE_PLATFORM = 'wait_cookie_remove_platform'

COOKIE_PLATFORM_KEYS = ['youtube', 'instagram', 'tiktok', 'twitter', 'soundcloud']
COOKIE_FILE_MAX_BYTES = 1_500_000
GROUP_TRACK_REFRESH_SECONDS = 12 * 60 * 60
_GROUP_TRACK_NEXT_ALLOWED_AT: dict[int, float] = {}


def build_router(ctx: AppContext) -> Router:
    router = Router(name='main')
    admin_states: dict[int, str] = {}
    broadcast_targets: dict[int, str] = {}
    pending_cookie_platforms: dict[int, str] = {}
    group_admin_block_keys = (
        'inspect_user',
        'broadcast',
        'cookie',
        'cookie_list',
        'cookie_set',
        'cookie_remove',
        'broadcast_target_users',
        'broadcast_target_groups',
        'broadcast_mode_normal',
        'broadcast_mode_forward',
        'stats',
        'fsub_menu',
        'admins_menu',
        'admins_list',
        'admins_add',
        'admins_remove',
        'block_target',
        'blocks_list',
        'blocks_add',
        'blocks_remove',
        'database_backup',
        'fsub_status',
        'fsub_add',
        'fsub_remove',
        'fsub_toggle_disable',
        'fsub_toggle_enable',
        'back',
        'home',
        'cancel',
    )
    group_blocked_admin_texts: set[str] = set()
    for key in group_admin_block_keys:
        group_blocked_admin_texts |= all_admin_button_variants(key)

    @router.message(F.text.in_(group_blocked_admin_texts), F.chat.type.in_({'group', 'supergroup'}))
    async def ignore_admin_buttons_in_groups(message: Message) -> None:
        return

    @router.message(Command('start'))
    async def start_handler(message: Message) -> None:
        if not message.from_user:
            return
        if not _is_private_chat(message.chat.type):
            return
        await _track_group_chat_from_message(db=ctx.db, message=message)
        user_id = message.from_user.id
        await ctx.db.upsert_user(
            user_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            if await _is_request_blocked(db=ctx.db, user_id=user_id, chat_id=message.chat.id):
                if not _is_group_chat(message.chat.type):
                    await message.answer(_blocked_access_text(lang))
                return
        if user_id not in ctx.admin_ids:
            is_allowed = await _ensure_membership_for_message(
                message=message,
                ctx=ctx,
                user_id=user_id,
                lang=lang,
            )
            if not is_allowed:
                return
        start_text = _start_welcome_text(lang)
        reply_markup = (
            build_admin_panel_keyboard(lang)
            if user_id in ctx.admin_ids and _is_private_chat(message.chat.type)
            else None
        )
        await message.answer(
            start_text,
            reply_markup=reply_markup,
        )

    @router.message(Command('lang'))
    async def lang_handler(message: Message) -> None:
        if not message.from_user:
            return
        if not _is_private_chat(message.chat.type):
            return
        await _track_group_chat_from_message(db=ctx.db, message=message)
        user_id = message.from_user.id
        await ctx.db.upsert_user(
            user_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            if await _is_request_blocked(db=ctx.db, user_id=user_id, chat_id=message.chat.id):
                if not _is_group_chat(message.chat.type):
                    await message.answer(_blocked_access_text(lang))
                return
        await message.answer(
            tr(lang, 'lang_choose'),
            reply_markup=build_language_keyboard(lang),
        )

    @router.callback_query(F.data.startswith('lang:set:'))
    async def lang_set_callback(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            return
        user_id = callback.from_user.id
        _, _, lang_value = callback.data.partition('lang:set:')
        if lang_value not in {'fa', 'en'}:
            current_lang = await ctx.db.get_user_language(user_id)
            await callback.answer(tr(current_lang, 'lang_invalid'), show_alert=True)
            return

        await ctx.db.set_user_language(user_id, lang_value)
        await _edit_message_content(
            callback.message,
            tr(lang_value, 'lang_saved'),
            reply_markup=build_language_keyboard(lang_value),
        )
        await callback.answer(tr(lang_value, 'lang_saved'))

    @router.my_chat_member()
    async def my_chat_member_handler(event: ChatMemberUpdated) -> None:
        if not _is_group_chat(event.chat.type):
            return
        old_status = _chat_member_status_value(event.old_chat_member)
        status = _chat_member_status_value(event.new_chat_member)
        if status in {'member', 'administrator', 'creator', 'restricted'}:
            await ctx.db.upsert_group_chat(
                chat_id=event.chat.id,
                username=event.chat.username,
                title=event.chat.title,
            )
            if old_status in {'left', 'kicked'}:
                await _send_group_welcome(event=event, ctx=ctx)
            return
        if status in {'left', 'kicked'}:
            await ctx.db.remove_group_chat(event.chat.id)

    @router.message(F.text.in_(all_admin_button_variants('broadcast')))
    async def admin_broadcast_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_BROADCAST_TARGET
        broadcast_targets.pop(user_id, None)
        await message.answer(
            _broadcast_target_select_text(lang),
            reply_markup=build_broadcast_target_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('inspect_user')))
    async def admin_inspect_user_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_USER_INSPECT
        await message.answer(
            _admin_user_inspect_prompt_text(lang),
            reply_markup=build_back_only_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('fsub_menu')))
    async def admin_force_sub_menu_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        admin_states.pop(user_id, None)
        broadcast_targets.pop(user_id, None)
        force_sub_enabled = await ctx.db.is_force_sub_enabled()
        await message.answer(
            _admin_force_sub_menu_text(lang),
            reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=force_sub_enabled),
        )

    @router.message(F.text.in_(all_admin_button_variants('admins_menu')))
    async def admin_managers_menu_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        admin_states.pop(user_id, None)
        broadcast_targets.pop(user_id, None)
        await message.answer(
            _admin_managers_menu_text(lang),
            reply_markup=build_admin_managers_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('admins_list')))
    async def admin_managers_list_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        admins = await ctx.db.list_bot_admins()
        await message.answer(
            _admin_managers_list_text(lang, admins),
            reply_markup=build_admin_managers_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('admins_add')))
    async def admin_managers_add_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        role = await ctx.db.get_bot_admin_role(user_id)
        if role != 'owner':
            await message.answer(
                _admin_add_owner_only_text(lang),
                reply_markup=build_admin_managers_keyboard(lang),
            )
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_ADMIN_ADD
        await message.answer(
            _admin_add_prompt_text(lang),
            reply_markup=build_force_sub_action_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('admins_remove')))
    async def admin_managers_remove_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        role = await ctx.db.get_bot_admin_role(user_id)
        if role != 'owner':
            await message.answer(
                _admin_remove_owner_only_text(lang),
                reply_markup=build_admin_managers_keyboard(lang),
            )
            return

        admins = await ctx.db.list_bot_admins()
        removable_admin_ids = [
            int(item['user_id'])
            for item in admins
            if str(item.get('role', '')).lower() == 'admin'
        ]
        if not removable_admin_ids:
            await message.answer(
                _admin_remove_empty_text(lang),
                reply_markup=build_admin_managers_keyboard(lang),
            )
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_ADMIN_REMOVE
        await message.answer(
            _admin_remove_select_text(lang),
            reply_markup=build_admin_remove_keyboard(lang, removable_admin_ids),
        )

    @router.message(F.text.in_(all_admin_button_variants('fsub_add')))
    async def admin_add_channel_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_FSUB_ADD
        await message.answer(
            _admin_action_prompt(lang, ADMIN_STATE_WAIT_FSUB_ADD),
            reply_markup=build_force_sub_action_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('block_target')))
    async def admin_block_target_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        role = await ctx.db.get_bot_admin_role(user_id)
        if role != 'owner':
            await message.answer(
                _admin_block_owner_only_text(lang),
                reply_markup=build_admin_panel_keyboard(lang),
            )
            return

        admin_states.pop(user_id, None)
        await message.answer(
            _admin_blocks_menu_text(lang),
            reply_markup=build_admin_blocks_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('blocks_list')))
    async def admin_blocks_list_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        role = await ctx.db.get_bot_admin_role(user_id)
        if role != 'owner':
            await message.answer(
                _admin_block_owner_only_text(lang),
                reply_markup=build_admin_panel_keyboard(lang),
            )
            return

        blocked = await ctx.db.list_blocked_targets()
        await message.answer(
            _admin_blocks_list_text(lang, blocked),
            reply_markup=build_admin_blocks_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('blocks_add')))
    async def admin_blocks_add_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        role = await ctx.db.get_bot_admin_role(user_id)
        if role != 'owner':
            await message.answer(
                _admin_block_owner_only_text(lang),
                reply_markup=build_admin_panel_keyboard(lang),
            )
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_BLOCK_ADD
        await message.answer(
            _admin_block_prompt_text(lang),
            reply_markup=build_force_sub_action_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('blocks_remove')))
    async def admin_blocks_remove_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        role = await ctx.db.get_bot_admin_role(user_id)
        if role != 'owner':
            await message.answer(
                _admin_block_owner_only_text(lang),
                reply_markup=build_admin_panel_keyboard(lang),
            )
            return

        blocked = await ctx.db.list_blocked_targets()
        target_ids = [int(item['target_id']) for item in blocked]
        if not target_ids:
            await message.answer(
                _admin_block_list_empty_text(lang),
                reply_markup=build_admin_blocks_keyboard(lang),
            )
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_BLOCK_REMOVE
        await message.answer(
            _admin_block_remove_select_text(lang),
            reply_markup=build_block_remove_keyboard(lang, target_ids),
        )

    @router.message(F.text.in_(all_admin_button_variants('fsub_remove')))
    async def admin_remove_channel_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        channels = await ctx.db.list_required_channels()
        if not channels:
            force_sub_enabled = await ctx.db.is_force_sub_enabled()
            await message.answer(
                tr(lang, 'force_sub_list_empty'),
                reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=force_sub_enabled),
            )
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_FSUB_REMOVE
        await message.answer(
            _force_sub_remove_select_text(lang),
            reply_markup=build_force_sub_remove_keyboard(lang, channels),
        )

    @router.message(F.text.in_(all_admin_button_variants('fsub_status')))
    async def admin_force_sub_status_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        await _send_force_sub_status(message=message, ctx=ctx, lang=lang)

    @router.message(F.text.in_(all_admin_button_variants('stats')))
    async def admin_stats_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        await _send_admin_stats(message=message, ctx=ctx)

    @router.message(F.text.in_(all_admin_button_variants('database_backup')))
    async def admin_database_backup_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        role = await ctx.db.get_bot_admin_role(user_id)
        if role != 'owner':
            await message.answer(
                _admin_backup_owner_only_text(lang),
                reply_markup=build_admin_panel_keyboard(lang),
            )
            return

        admin_states.pop(user_id, None)
        broadcast_targets.pop(user_id, None)
        pending_cookie_platforms.pop(user_id, None)
        preparing_message = await message.answer(_admin_backup_preparing_text(lang))
        try:
            await ctx.backup_service.send_backup_to_chat(chat_id=message.chat.id, lang=lang)
            with contextlib.suppress(TelegramBadRequest):
                await preparing_message.delete()
        except Exception:
            logger.exception('Manual database backup failed for owner %s', user_id)
            await message.answer(
                _admin_backup_failed_text(lang),
                reply_markup=build_admin_panel_keyboard(lang),
            )

    @router.message(F.text.in_(all_admin_button_variants('cookie')))
    async def admin_cookie_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        admin_states.pop(user_id, None)
        pending_cookie_platforms.pop(user_id, None)
        await message.answer(
            _admin_cookie_menu_text(lang),
            reply_markup=build_admin_cookie_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('cookie_list')))
    async def admin_cookie_list_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        admin_states.pop(user_id, None)
        pending_cookie_platforms.pop(user_id, None)
        cookies = await ctx.db.list_platform_cookies()
        await message.answer(
            _admin_cookie_list_text(lang, cookies),
            reply_markup=build_admin_cookie_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('cookie_set')))
    async def admin_cookie_set_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_COOKIE_SET_PLATFORM
        pending_cookie_platforms.pop(user_id, None)
        await message.answer(
            _admin_cookie_pick_platform_text(lang, action='set'),
            reply_markup=build_cookie_platform_keyboard(lang, COOKIE_PLATFORM_KEYS),
        )

    @router.message(F.text.in_(all_admin_button_variants('cookie_remove')))
    async def admin_cookie_remove_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return

        cookies = await ctx.db.list_platform_cookies()
        platforms = [str(item.get('platform') or '').strip().lower() for item in cookies if item.get('platform')]
        if not platforms:
            await message.answer(
                _admin_cookie_remove_empty_text(lang),
                reply_markup=build_admin_cookie_keyboard(lang),
            )
            return

        admin_states[user_id] = ADMIN_STATE_WAIT_COOKIE_REMOVE_PLATFORM
        pending_cookie_platforms.pop(user_id, None)
        await message.answer(
            _admin_cookie_pick_platform_text(lang, action='remove'),
            reply_markup=build_cookie_platform_keyboard(lang, platforms),
        )

    @router.message(F.text.in_(force_sub_toggle_button_variants()))
    async def admin_force_sub_toggle_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        admin_states.pop(user_id, None)
        broadcast_targets.pop(user_id, None)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return
        enabled = await ctx.db.toggle_force_sub_enabled()
        notice = (
            '🟢 عضویت اجباری ربات فعال شد.' if enabled else '🔴 عضویت اجباری ربات غیرفعال شد.'
        ) if lang == 'fa' else (
            'Force-sub enabled ✅' if enabled else 'Force-sub disabled ⛔'
        )
        await message.answer(
            notice,
            reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=enabled),
        )

    @router.message(F.text.in_(all_admin_button_variants('back')))
    async def admin_back_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        previous_state = admin_states.pop(user_id, None)
        pending_cookie_platforms.pop(user_id, None)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return
        if previous_state in {
            ADMIN_STATE_WAIT_BROADCAST_NORMAL,
            ADMIN_STATE_WAIT_BROADCAST_FORWARD,
        }:
            admin_states[user_id] = ADMIN_STATE_WAIT_BROADCAST_MODE
            await message.answer(
                _broadcast_mode_select_text(lang, broadcast_targets.get(user_id) or 'users'),
                reply_markup=build_broadcast_mode_keyboard(lang),
            )
            return
        if previous_state == ADMIN_STATE_WAIT_BROADCAST_MODE:
            admin_states[user_id] = ADMIN_STATE_WAIT_BROADCAST_TARGET
            broadcast_targets.pop(user_id, None)
            await message.answer(
                _broadcast_target_select_text(lang),
                reply_markup=build_broadcast_target_keyboard(lang),
            )
            return
        if previous_state == ADMIN_STATE_WAIT_BROADCAST_TARGET:
            broadcast_targets.pop(user_id, None)
            await message.answer(
                _admin_home_text(lang),
                reply_markup=build_admin_panel_keyboard(lang),
            )
            return
        if previous_state == ADMIN_STATE_WAIT_USER_INSPECT:
            await message.answer(
                _admin_home_text(lang),
                reply_markup=build_admin_panel_keyboard(lang),
            )
            return
        if previous_state == ADMIN_STATE_WAIT_ADMIN_ADD:
            await message.answer(
                _admin_managers_menu_text(lang),
                reply_markup=build_admin_managers_keyboard(lang),
            )
            return
        if previous_state == ADMIN_STATE_WAIT_ADMIN_REMOVE:
            await message.answer(
                _admin_managers_menu_text(lang),
                reply_markup=build_admin_managers_keyboard(lang),
            )
            return
        if previous_state in {ADMIN_STATE_WAIT_BLOCK_ADD, ADMIN_STATE_WAIT_BLOCK_REMOVE}:
            await message.answer(
                _admin_blocks_menu_text(lang),
                reply_markup=build_admin_blocks_keyboard(lang),
            )
            return
        if previous_state in {ADMIN_STATE_WAIT_FSUB_ADD, ADMIN_STATE_WAIT_FSUB_REMOVE}:
            force_sub_enabled = await ctx.db.is_force_sub_enabled()
            await message.answer(
                _admin_force_sub_menu_text(lang),
                reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=force_sub_enabled),
            )
            return
        if previous_state == ADMIN_STATE_WAIT_COOKIE_SET_CONTENT:
            admin_states[user_id] = ADMIN_STATE_WAIT_COOKIE_SET_PLATFORM
            await message.answer(
                _admin_cookie_pick_platform_text(lang, action='set'),
                reply_markup=build_cookie_platform_keyboard(lang, COOKIE_PLATFORM_KEYS),
            )
            return
        if previous_state in {ADMIN_STATE_WAIT_COOKIE_SET_PLATFORM, ADMIN_STATE_WAIT_COOKIE_REMOVE_PLATFORM}:
            await message.answer(
                _admin_cookie_menu_text(lang),
                reply_markup=build_admin_cookie_keyboard(lang),
            )
            return
        await message.answer(
            _admin_home_text(lang),
            reply_markup=build_admin_panel_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('home')))
    async def admin_home_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        admin_states.pop(user_id, None)
        broadcast_targets.pop(user_id, None)
        pending_cookie_platforms.pop(user_id, None)
        if user_id not in ctx.admin_ids:
            await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
            return
        await message.answer(
            _admin_home_text(lang),
            reply_markup=build_admin_panel_keyboard(lang),
        )

    @router.message(F.text.in_(all_admin_button_variants('cancel')))
    async def admin_cancel_action_button_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id in admin_states:
            admin_states.pop(user_id, None)
        if user_id in broadcast_targets:
            broadcast_targets.pop(user_id, None)
        if user_id in pending_cookie_platforms:
            pending_cookie_platforms.pop(user_id, None)
        if user_id in ctx.admin_ids:
            await message.answer(
                _admin_cancel_text(lang),
                reply_markup=build_admin_panel_keyboard(lang),
            )

    @router.message(_AdminStateFilter(admin_states))
    async def admin_pending_action_handler(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        if user_id not in ctx.admin_ids:
            return
        state = admin_states.get(user_id)
        if not state:
            return

        lang = await ctx.db.get_user_language(user_id)

        if state == ADMIN_STATE_WAIT_BROADCAST_TARGET:
            selected_target = _resolve_broadcast_target(message.text or '')
            if selected_target is None:
                await message.answer(
                    _broadcast_invalid_target_text(lang),
                    reply_markup=build_broadcast_target_keyboard(lang),
                )
                return
            broadcast_targets[user_id] = selected_target
            admin_states[user_id] = ADMIN_STATE_WAIT_BROADCAST_MODE
            await message.answer(
                _broadcast_mode_select_text(lang, selected_target),
                reply_markup=build_broadcast_mode_keyboard(lang),
            )
            return

        if state == ADMIN_STATE_WAIT_BROADCAST_MODE:
            selected_target = broadcast_targets.get(user_id)
            if selected_target not in {'users', 'groups'}:
                admin_states[user_id] = ADMIN_STATE_WAIT_BROADCAST_TARGET
                await message.answer(
                    _broadcast_target_select_text(lang),
                    reply_markup=build_broadcast_target_keyboard(lang),
                )
                return
            selected_mode = _resolve_broadcast_mode(message.text or '')
            if selected_mode == 'normal':
                admin_states[user_id] = ADMIN_STATE_WAIT_BROADCAST_NORMAL
                await message.answer(
                    _broadcast_normal_prompt_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            if selected_mode == 'forward':
                admin_states[user_id] = ADMIN_STATE_WAIT_BROADCAST_FORWARD
                await message.answer(
                    _broadcast_forward_prompt_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            await message.answer(
                _broadcast_invalid_mode_text(lang),
                reply_markup=build_broadcast_mode_keyboard(lang),
            )
            return

        if state == ADMIN_STATE_WAIT_BROADCAST_NORMAL:
            selected_target = broadcast_targets.get(user_id) or 'users'
            selected_mode = _resolve_broadcast_mode(message.text or '')
            if selected_mode == 'normal':
                await message.answer(
                    _broadcast_normal_prompt_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            if selected_mode == 'forward':
                admin_states[user_id] = ADMIN_STATE_WAIT_BROADCAST_FORWARD
                await message.answer(
                    _broadcast_forward_prompt_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            text_payload = (message.text or '').strip()
            if not text_payload:
                await message.answer(
                    _broadcast_normal_prompt_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            await _run_broadcast(
                message=message,
                db=ctx.db,
                lang=lang,
                target=selected_target,
                mode='normal',
                source_message=None,
                text_payload=text_payload,
            )
            admin_states.pop(user_id, None)
            broadcast_targets.pop(user_id, None)
            return

        if state == ADMIN_STATE_WAIT_BROADCAST_FORWARD:
            selected_target = broadcast_targets.get(user_id) or 'users'
            selected_mode = _resolve_broadcast_mode(message.text or '')
            if selected_mode == 'normal':
                admin_states[user_id] = ADMIN_STATE_WAIT_BROADCAST_NORMAL
                await message.answer(
                    _broadcast_normal_prompt_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            if selected_mode == 'forward':
                await message.answer(
                    _broadcast_forward_prompt_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            forward_channel_ref = _extract_forwarded_channel_reference(message)
            if forward_channel_ref is None:
                await message.answer(
                    _broadcast_forward_required_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            await _run_broadcast(
                message=message,
                db=ctx.db,
                lang=lang,
                target=selected_target,
                mode='forward',
                source_message=message,
                text_payload='',
            )
            admin_states.pop(user_id, None)
            broadcast_targets.pop(user_id, None)
            return

        if state == ADMIN_STATE_WAIT_USER_INSPECT:
            text_value = (message.text or '').strip()
            if not text_value.isdigit():
                await message.answer(
                    _admin_user_inspect_invalid_input_text(lang),
                    reply_markup=build_back_only_keyboard(lang),
                )
                return

            target_user_id = int(text_value)
            if target_user_id <= 0:
                await message.answer(
                    _admin_user_inspect_invalid_input_text(lang),
                    reply_markup=build_back_only_keyboard(lang),
                )
                return

            admin_states.pop(user_id, None)
            await _send_user_stats(
                message=message,
                ctx=ctx,
                lang=lang,
                target_user_id=target_user_id,
            )
            return

        if state == ADMIN_STATE_WAIT_ADMIN_ADD:
            text_value = (message.text or '').strip()
            if not text_value.isdigit():
                await message.answer(
                    _admin_add_invalid_input_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return

            target_user_id = int(text_value)
            if target_user_id <= 0:
                await message.answer(
                    _admin_add_invalid_input_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return

            try:
                target_chat = await message.bot.get_chat(target_user_id)
            except (TelegramBadRequest, TelegramForbiddenError):
                await message.answer(
                    _admin_add_invalid_input_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return

            if str(getattr(target_chat, 'type', '')).lower() != 'private':
                await message.answer(
                    _admin_add_invalid_input_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return

            await ctx.db.upsert_bot_admin(target_user_id, role='admin')
            ctx.admin_ids.add(target_user_id)
            admin_states.pop(user_id, None)
            await message.answer(
                _admin_add_success_text(lang, target_user_id),
                reply_markup=build_admin_managers_keyboard(lang),
            )
            return

        if state == ADMIN_STATE_WAIT_ADMIN_REMOVE:
            text_value = (message.text or '').strip()
            if not text_value.isdigit():
                admins = await ctx.db.list_bot_admins()
                removable_admin_ids = [
                    int(item['user_id'])
                    for item in admins
                    if str(item.get('role', '')).lower() == 'admin'
                ]
                if not removable_admin_ids:
                    admin_states.pop(user_id, None)
                    await message.answer(
                        _admin_remove_empty_text(lang),
                        reply_markup=build_admin_managers_keyboard(lang),
                    )
                    return

                await message.answer(
                    _admin_remove_select_text(lang),
                    reply_markup=build_admin_remove_keyboard(lang, removable_admin_ids),
                )
                return

            target_user_id = int(text_value)
            removed = await ctx.db.remove_bot_admin(target_user_id)
            if removed > 0:
                if target_user_id in ctx.admin_ids:
                    ctx.admin_ids.discard(target_user_id)
                admin_states.pop(user_id, None)
                await message.answer(
                    _admin_remove_success_text(lang, target_user_id),
                    reply_markup=build_admin_managers_keyboard(lang),
                )
                return

            admins = await ctx.db.list_bot_admins()
            removable_admin_ids = [
                int(item['user_id'])
                for item in admins
                if str(item.get('role', '')).lower() == 'admin'
            ]
            if not removable_admin_ids:
                admin_states.pop(user_id, None)
                await message.answer(
                    _admin_remove_empty_text(lang),
                    reply_markup=build_admin_managers_keyboard(lang),
                )
                return

            await message.answer(
                _admin_remove_select_text(lang),
                reply_markup=build_admin_remove_keyboard(lang, removable_admin_ids),
            )
            return

        if state == ADMIN_STATE_WAIT_BLOCK_ADD:
            raw = (message.text or '').strip()
            if not raw or not raw.lstrip('-').isdigit():
                await message.answer(
                    _admin_block_invalid_input_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return

            target_id = int(raw)
            if target_id == 0:
                await message.answer(
                    _admin_block_invalid_input_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return

            if target_id > 0 and target_id in ctx.admin_ids:
                await message.answer(
                    _admin_block_admin_forbidden_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return

            target_type = 'group' if target_id < 0 else 'user'
            await ctx.db.block_target(target_id, target_type=target_type)
            notified = await _notify_block_status_to_target(
                bot=message.bot,
                db=ctx.db,
                target_id=target_id,
                target_type=target_type,
                blocked=True,
                fallback_lang=lang,
            )
            admin_states.pop(user_id, None)
            await message.answer(
                _with_target_notification_status(
                    _admin_block_success_text(lang, target_id, target_type),
                    lang=lang,
                    notified=notified,
                ),
                reply_markup=build_admin_blocks_keyboard(lang),
            )
            return

        if state == ADMIN_STATE_WAIT_BLOCK_REMOVE:
            raw = (message.text or '').strip()
            if not raw or not raw.lstrip('-').isdigit():
                blocked = await ctx.db.list_blocked_targets()
                target_ids = [int(item['target_id']) for item in blocked]
                if not target_ids:
                    admin_states.pop(user_id, None)
                    await message.answer(
                        _admin_block_list_empty_text(lang),
                        reply_markup=build_admin_blocks_keyboard(lang),
                    )
                    return
                await message.answer(
                    _admin_block_remove_select_text(lang),
                    reply_markup=build_block_remove_keyboard(lang, target_ids),
                )
                return

            target_id = int(raw)
            target_type = 'group' if target_id < 0 else 'user'
            removed = await ctx.db.unblock_target(target_id)
            if removed > 0:
                notified = await _notify_block_status_to_target(
                    bot=message.bot,
                    db=ctx.db,
                    target_id=target_id,
                    target_type=target_type,
                    blocked=False,
                    fallback_lang=lang,
                )
                admin_states.pop(user_id, None)
                await message.answer(
                    _with_target_notification_status(
                        _admin_unblock_success_text(lang, target_id),
                        lang=lang,
                        notified=notified,
                    ),
                    reply_markup=build_admin_blocks_keyboard(lang),
                )
                return

            blocked = await ctx.db.list_blocked_targets()
            target_ids = [int(item['target_id']) for item in blocked]
            if not target_ids:
                admin_states.pop(user_id, None)
                await message.answer(
                    _admin_block_list_empty_text(lang),
                    reply_markup=build_admin_blocks_keyboard(lang),
                )
                return
            await message.answer(
                _admin_block_remove_select_text(lang),
                reply_markup=build_block_remove_keyboard(lang, target_ids),
            )
            return

        if state == ADMIN_STATE_WAIT_COOKIE_SET_PLATFORM:
            selected_platform = _resolve_cookie_platform(message.text or '')
            if selected_platform is None:
                await message.answer(
                    _admin_cookie_invalid_platform_text(lang),
                    reply_markup=build_cookie_platform_keyboard(lang, COOKIE_PLATFORM_KEYS),
                )
                return

            pending_cookie_platforms[user_id] = selected_platform
            admin_states[user_id] = ADMIN_STATE_WAIT_COOKIE_SET_CONTENT
            await message.answer(
                _admin_cookie_set_payload_prompt_text(lang, selected_platform),
                reply_markup=build_force_sub_action_keyboard(lang),
            )
            return

        if state == ADMIN_STATE_WAIT_COOKIE_SET_CONTENT:
            selected_platform = pending_cookie_platforms.get(user_id)
            if selected_platform is None:
                admin_states[user_id] = ADMIN_STATE_WAIT_COOKIE_SET_PLATFORM
                await message.answer(
                    _admin_cookie_pick_platform_text(lang, action='set'),
                    reply_markup=build_cookie_platform_keyboard(lang, COOKIE_PLATFORM_KEYS),
                )
                return

            cookie_payload = (message.text or '').strip()
            if message.document is not None:
                cookie_payload = await _read_cookie_document_text(message, max_bytes=COOKIE_FILE_MAX_BYTES)
            if not _looks_like_cookie_payload(cookie_payload):
                await message.answer(
                    _admin_cookie_payload_invalid_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return

            try:
                await ctx.db.upsert_platform_cookie(selected_platform, cookie_payload)
                ctx.downloader.set_platform_cookie(Platform(selected_platform), cookie_payload)
            except ValueError:
                await message.answer(
                    _admin_cookie_payload_invalid_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            except OSError:
                await message.answer(
                    _admin_cookie_store_error_text(lang),
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return

            admin_states.pop(user_id, None)
            pending_cookie_platforms.pop(user_id, None)
            await message.answer(
                _admin_cookie_set_success_text(lang, selected_platform),
                reply_markup=build_admin_cookie_keyboard(lang),
            )
            return

        if state == ADMIN_STATE_WAIT_COOKIE_REMOVE_PLATFORM:
            cookies = await ctx.db.list_platform_cookies()
            existing_platforms = [
                str(item.get('platform') or '').strip().lower()
                for item in cookies
                if item.get('platform')
            ]
            if not existing_platforms:
                admin_states.pop(user_id, None)
                await message.answer(
                    _admin_cookie_remove_empty_text(lang),
                    reply_markup=build_admin_cookie_keyboard(lang),
                )
                return

            selected_platform = _resolve_cookie_platform(message.text or '')
            if selected_platform is None or selected_platform not in existing_platforms:
                await message.answer(
                    _admin_cookie_invalid_platform_text(lang),
                    reply_markup=build_cookie_platform_keyboard(lang, existing_platforms),
                )
                return

            removed = await ctx.db.remove_platform_cookie(selected_platform)
            if removed > 0:
                ctx.downloader.remove_platform_cookie(Platform(selected_platform))
            admin_states.pop(user_id, None)
            pending_cookie_platforms.pop(user_id, None)
            await message.answer(
                _admin_cookie_remove_success_text(lang, selected_platform),
                reply_markup=build_admin_cookie_keyboard(lang),
            )
            return

        text_value = (message.text or '').strip()

        if state == ADMIN_STATE_WAIT_FSUB_ADD:
            channel_ref = _extract_forwarded_channel_reference(message)
            if channel_ref is None:
                invalid_forward_text = (
                    '⛔️ پیام را از یک کانال فوروارد کنید.'
                    if lang == 'fa'
                    else '⛔️ Please forward a message from a channel.'
                )
                await message.answer(
                    invalid_forward_text,
                    reply_markup=build_force_sub_action_keyboard(lang),
                )
                return
            await _add_required_channel(
                message=message,
                db=ctx.db,
                lang=lang,
                channel_ref=channel_ref,
            )
            admin_states.pop(user_id, None)
            return

        if state == ADMIN_STATE_WAIT_FSUB_REMOVE:
            channel_ref = _parse_channel_reference(text_value)
            if channel_ref is None:
                channels = await ctx.db.list_required_channels()
                if not channels:
                    force_sub_enabled = await ctx.db.is_force_sub_enabled()
                    admin_states.pop(user_id, None)
                    await message.answer(
                        tr(lang, 'force_sub_list_empty'),
                        reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=force_sub_enabled),
                    )
                    return
                await message.answer(
                    _force_sub_remove_select_text(lang),
                    reply_markup=build_force_sub_remove_keyboard(lang, channels),
                )
                return
            if isinstance(channel_ref, int) and channel_ref > 0:
                channels = await ctx.db.list_required_channels()
                resolved_chat_id = next(
                    (
                        int(channel['chat_id'])
                        for channel in channels
                        if abs(int(channel['chat_id'])) == channel_ref
                    ),
                    None,
                )
                if resolved_chat_id is not None:
                    channel_ref = resolved_chat_id
            removed = await ctx.db.remove_required_channel(str(channel_ref))
            text = tr(lang, 'force_sub_removed') if removed > 0 else tr(lang, 'force_sub_not_found')
        force_sub_enabled = await ctx.db.is_force_sub_enabled()
        await message.answer(
            text,
            reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=force_sub_enabled),
        )
        admin_states.pop(user_id, None)
        return

    @router.callback_query(F.data == 'force:unavailable')
    async def force_sub_unavailable_callback(callback: CallbackQuery) -> None:
        if not callback.from_user:
            return
        lang = await ctx.db.get_user_language(callback.from_user.id)
        await callback.answer(tr(lang, 'force_sub_private_unavailable'), show_alert=True)

    @router.callback_query(F.data == 'force:check')
    async def force_sub_check_callback(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            return
        user_id = callback.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            if await _is_request_blocked(db=ctx.db, user_id=user_id, chat_id=callback.message.chat.id):
                await callback.answer(_blocked_access_text(lang), show_alert=True)
                return

        if user_id in ctx.admin_ids:
            await callback.answer(tr(lang, 'force_sub_ok'), show_alert=True)
            return

        missing_channels = await _missing_required_channels(
            bot=callback.message.bot,
            db=ctx.db,
            user_id=user_id,
        )
        if not missing_channels:
            await _edit_message_content(callback.message, tr(lang, 'force_sub_ok'))
            await callback.answer(tr(lang, 'force_sub_ok'), show_alert=True)
            return

        try:
            await callback.message.edit_text(
                tr(lang, 'force_sub_required'),
                reply_markup=build_force_sub_keyboard(
                    channels=missing_channels,
                    check_label=tr(lang, 'force_sub_check'),
                ),
            )
        except TelegramBadRequest:
            pass
        await callback.answer(tr(lang, 'force_sub_still_missing'), show_alert=True)

    @router.message(F.text)
    async def url_handler(message: Message) -> None:
        if not message.from_user or not message.text:
            return

        await _track_group_chat_from_message(db=ctx.db, message=message)
        user_id = message.from_user.id
        await ctx.db.upsert_user(
            user_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            if await _is_request_blocked(db=ctx.db, user_id=user_id, chat_id=message.chat.id):
                if not _is_group_chat(message.chat.type):
                    await message.answer(_blocked_access_text(lang))
                return
        if user_id not in ctx.admin_ids:
            is_allowed = await _ensure_membership_for_message(
                message=message,
                ctx=ctx,
                user_id=user_id,
                lang=lang,
            )
            if not is_allowed:
                return

        url = extract_first_url(message.text)
        if not url:
            if not _is_group_chat(message.chat.type):
                await message.answer(tr(lang, 'invalid_message_or_link'))
            return

        if not is_supported_url(url):
            if not _is_group_chat(message.chat.type):
                await message.answer(tr(lang, 'invalid_message_or_link'))
            return

        detected_platform = detect_platform(url)
        auto_download_platforms = {
            Platform.INSTAGRAM,
            Platform.TIKTOK,
            Platform.SOUNDCLOUD,
            Platform.TWITTER,
        }
        allowed, reason_code, wait_seconds = await ctx.limiter.try_start_probe(user_id)
        if not allowed:
            if not _is_group_chat(message.chat.type):
                await message.answer(
                    _rate_limited_text(lang, reason_code, wait_seconds),
                )
            return

        try:
            media = await ctx.downloader.probe(url)
        except DownloaderError as exc:
            await ctx.limiter.mark_probe_finished(user_id)
            classification = classify_download_error(str(exc))
            await ctx.db.log_download(
                user_id=user_id,
                url=url,
                platform=detected_platform.value,
                mode='probe',
                status='failed',
                file_size=None,
                error=str(exc),
                error_category=classification.category,
                error_hint=classification.hint_en,
            )
            if classification.category == 'cookie_required':
                await maybe_send_cookie_expiry_alert(
                    bot=message.bot,
                    db=ctx.db,
                    platform=detected_platform,
                    enabled=ctx.cookie_alert_enabled,
                    threshold=ctx.cookie_alert_threshold,
                    window_minutes=ctx.cookie_alert_window_minutes,
                    cooldown_minutes=ctx.cookie_alert_cooldown_minutes,
                    default_recipient_ids=ctx.admin_ids,
                )
            if should_hide_technical_reason(classification.category):
                user_message = tr(
                    lang,
                    'cannot_process_url_simple_hint',
                    hint=classification.hint(lang),
                )
            else:
                user_message = tr(
                    lang,
                    'cannot_process_url_with_hint',
                    reason=compact_error_reason(str(exc)),
                    hint=classification.hint(lang),
                )
            if not _is_group_chat(message.chat.type):
                await message.answer(
                    user_message,
                )
            return
        except Exception as exc:
            await ctx.limiter.mark_probe_finished(user_id)
            logger.exception('Probe failed for %s: %s', url, exc)
            classification = classify_download_error(str(exc))
            await ctx.db.log_download(
                user_id=user_id,
                url=url,
                platform=detected_platform.value,
                mode='probe',
                status='failed',
                file_size=None,
                error=str(exc),
                error_category=classification.category,
                error_hint=classification.hint_en,
            )
            if classification.category == 'cookie_required':
                await maybe_send_cookie_expiry_alert(
                    bot=message.bot,
                    db=ctx.db,
                    platform=detected_platform,
                    enabled=ctx.cookie_alert_enabled,
                    threshold=ctx.cookie_alert_threshold,
                    window_minutes=ctx.cookie_alert_window_minutes,
                    cooldown_minutes=ctx.cookie_alert_cooldown_minutes,
                    default_recipient_ids=ctx.admin_ids,
                )
            if should_hide_technical_reason(classification.category):
                user_message = tr(
                    lang,
                    'cannot_process_url_simple_hint',
                    hint=classification.hint(lang),
                )
            else:
                user_message = tr(
                    lang,
                    'cannot_process_url_with_hint',
                    reason=compact_error_reason(str(exc) or tr(lang, 'unexpected_error_processing')),
                    hint=classification.hint(lang),
                )
            if not _is_group_chat(message.chat.type):
                await message.answer(
                    user_message,
                )
            return
        await ctx.limiter.mark_probe_finished(user_id)

        youtube_limit_minutes = int(ctx.youtube_max_duration_minutes)
        if (
            media.platform == Platform.YOUTUBE
            and youtube_limit_minutes > 0
            and media.duration is not None
            and media.duration > youtube_limit_minutes * 60
        ):
            if not _is_group_chat(message.chat.type):
                await message.answer(
                    _youtube_duration_limit_text(
                        lang=lang,
                        limit_minutes=youtube_limit_minutes,
                    ),
                )
            return

        max_file_size_bytes = ctx.manager.max_file_size_bytes
        default_modes = default_modes_for_platform(media.platform)
        available_modes = _available_modes_within_limit(
            mode_candidates=default_modes,
            mode_size_bytes=media.mode_size_bytes,
            max_file_size_bytes=max_file_size_bytes,
        )

        if media.platform in {Platform.INSTAGRAM, Platform.TIKTOK, Platform.SOUNDCLOUD, Platform.TWITTER}:
            selected_mode = (
                DownloadMode.AUDIO_MP3 if media.platform == Platform.SOUNDCLOUD else DownloadMode.BEST
            )
            if selected_mode not in available_modes:
                if not _is_group_chat(message.chat.type):
                    await message.answer(
                        _mode_exceeds_limit_text(
                            lang=lang,
                            mode=selected_mode,
                            max_file_size_bytes=max_file_size_bytes,
                        ),
                    )
                return

            in_group = _is_group_chat(message.chat.type)
            allowed, reason_code, wait_seconds = await ctx.limiter.try_start(user_id)
            if not allowed:
                if not _is_group_chat(message.chat.type):
                    await message.answer(
                        _rate_limited_text(lang, reason_code, wait_seconds),
                    )
                return

            daily_ok, daily_reason = await _enforce_daily_limits_for_start(
                ctx=ctx,
                user_id=user_id,
                lang=lang,
            )
            if not daily_ok:
                await ctx.limiter.mark_finished(user_id, apply_cooldown=False)
                if not _is_group_chat(message.chat.type):
                    await message.answer(daily_reason)
                return

            try:
                request_id = build_request_id()
                pending = PendingRequest(
                    request_id=request_id,
                    user_id=user_id,
                    url=url,
                    title=media.title,
                    platform=media.platform,
                    available_modes=available_modes,
                )
                await ctx.request_store.put(pending)

                request = DownloadRequest(
                    request_id=request_id,
                    user_id=user_id,
                    chat_id=message.chat.id,
                    url=url,
                    mode=selected_mode,
                    platform=pending.platform,
                    title=pending.title,
                    lang=lang,
                )
                cached_exists = await ctx.manager.has_cached_content(request)
                status_message_id = 0
                if not in_group and not cached_exists:
                    loading_text = 'در حال بارگیری...' if lang == 'fa' else 'Loading...'
                    loading_message = await message.answer(
                        loading_text,
                    )
                    status_message_id = loading_message.message_id

                ctx.manager.enqueue(request, status_message_id)
            except Exception:
                await ctx.limiter.mark_finished(user_id, apply_cooldown=False)
                raise
            return

        if not available_modes:
            if not _is_group_chat(message.chat.type):
                await message.answer(
                    _no_modes_under_limit_text(
                        lang=lang,
                        max_file_size_bytes=max_file_size_bytes,
                    ),
                )
            return

        request_id = build_request_id()
        pending = PendingRequest(
            request_id=request_id,
            user_id=user_id,
            url=url,
            title=media.title,
            platform=media.platform,
            available_modes=available_modes,
        )
        await ctx.request_store.put(pending)

        info_text = tr(
            lang,
            'media_info',
            title=media.title,
            uploader=media.uploader or '-',
            duration=human_duration(media.duration),
            platform=media.platform.value,
        )
        reply_markup = build_download_options(
            request_id=request_id,
            platform=media.platform,
            lang=lang,
            modes=available_modes,
        )

        if media.platform == Platform.YOUTUBE and media.thumbnail_url:
            preview = safe_caption(media.title, limit=980)
            preview = f'🍿 {preview}' if preview else '🍿'
            try:
                await message.answer_photo(
                    photo=media.thumbnail_url,
                    caption=preview,
                    reply_markup=reply_markup,
                )
            except TelegramBadRequest:
                await message.answer(
                    info_text,
                    reply_markup=reply_markup,
                )
        else:
            await message.answer(
                info_text,
                reply_markup=reply_markup,
            )

    @router.callback_query(F.data.startswith('dl:'))
    async def download_callback(callback: CallbackQuery) -> None:
        if not callback.from_user or not callback.message:
            return

        await _track_group_chat_from_message(db=ctx.db, message=callback.message)
        user_id = callback.from_user.id
        lang = await ctx.db.get_user_language(user_id)
        if user_id not in ctx.admin_ids:
            if await _is_request_blocked(db=ctx.db, user_id=user_id, chat_id=callback.message.chat.id):
                await callback.answer(_blocked_access_text(lang), show_alert=True)
                return
        if user_id not in ctx.admin_ids:
            is_allowed = await _ensure_membership_for_callback(
                callback=callback,
                ctx=ctx,
                user_id=user_id,
                lang=lang,
            )
            if not is_allowed:
                return

        parts = callback.data.split(':')
        if len(parts) != 3:
            await callback.answer(tr(lang, 'invalid_action'), show_alert=True)
            return

        _, request_id, mode_value = parts

        pending = await ctx.request_store.get(request_id)
        if pending is None:
            await callback.answer(tr(lang, 'request_expired'), show_alert=True)
            return

        if pending.user_id != user_id:
            await callback.answer(tr(lang, 'button_other_user'), show_alert=True)
            return

        try:
            mode = DownloadMode(mode_value)
        except ValueError:
            await callback.answer(tr(lang, 'unsupported_mode'), show_alert=True)
            return

        if pending.available_modes and mode not in set(pending.available_modes):
            await callback.answer(
                _mode_exceeds_limit_text(
                    lang=lang,
                    mode=mode,
                    max_file_size_bytes=ctx.manager.max_file_size_bytes,
                ),
                show_alert=True,
            )
            return
        if is_video_resolution_mode(mode):
            unavailable_mode_key_by_platform = {
                Platform.INSTAGRAM: 'mode_unavailable_instagram',
                Platform.TIKTOK: 'mode_unavailable_tiktok',
                Platform.TWITTER: 'mode_unavailable_twitter',
                Platform.SOUNDCLOUD: 'mode_unavailable_soundcloud',
            }
            unavailable_key = unavailable_mode_key_by_platform.get(pending.platform)
            if unavailable_key is not None:
                await callback.answer(tr(lang, unavailable_key), show_alert=True)
                return

        allowed, reason_code, wait_seconds = await ctx.limiter.try_start(user_id)
        if not allowed:
            await callback.answer(
                _rate_limited_text(lang, reason_code, wait_seconds),
                show_alert=True,
            )
            return

        daily_ok, daily_reason = await _enforce_daily_limits_for_start(ctx=ctx, user_id=user_id, lang=lang)
        if not daily_ok:
            await ctx.limiter.mark_finished(user_id, apply_cooldown=False)
            await callback.answer(daily_reason, show_alert=True)
            return

        try:
            request = DownloadRequest(
                request_id=request_id,
                user_id=user_id,
                chat_id=callback.message.chat.id,
                url=pending.url,
                mode=mode,
                platform=pending.platform,
                title=pending.title,
                lang=lang,
            )
            cached_exists = await ctx.manager.has_cached_content(request)
            if mode == DownloadMode.AUDIO_MP3:
                await _clear_inline_keyboard(callback.message)
            if pending.platform in {Platform.INSTAGRAM, Platform.TIKTOK}:
                in_group = _is_group_chat(callback.message.chat.type)
                status_message_id = 0
                if not in_group and not cached_exists:
                    loading_text = 'در حال بارگیری...' if lang == 'fa' else 'Loading...'
                    loading_message = await callback.message.answer(
                        loading_text,
                    )
                    status_message_id = loading_message.message_id
                ctx.manager.enqueue(request, status_message_id)
            else:
                if not cached_exists:
                    if not _is_group_chat(callback.message.chat.type):
                        waiting_text = (
                            _build_loading_caption(pending.title, lang)
                            if pending.platform == Platform.YOUTUBE
                            else tr(lang, 'queued_preparing', mode=mode_label(mode, lang))
                        )
                        await _edit_message_content(
                            callback.message,
                            waiting_text,
                        )
                ctx.manager.enqueue(request, callback.message.message_id)
        except Exception:
            await ctx.limiter.mark_finished(user_id, apply_cooldown=False)
            raise

        await callback.answer()

    return router


async def _send_admin_stats(message: Message, ctx: AppContext) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id
    lang = await ctx.db.get_user_language(user_id)

    if user_id not in ctx.admin_ids:
        await message.answer(tr(lang, 'admins_only'), reply_markup=ReplyKeyboardRemove())
        return

    stats = await ctx.db.get_detailed_stats()
    platform_counts_raw = stats.get('platform_counts') or {}
    error_category_counts_raw = stats.get('error_category_counts') or {}
    error_category_counts_24h_raw = stats.get('error_category_counts_24h') or {}
    supported_platform_keys = ['youtube', 'instagram', 'tiktok', 'twitter', 'soundcloud']
    platform_labels_fa = {
        'youtube': 'YouTube',
        'instagram': 'Instagram',
        'tiktok': 'TikTok',
        'twitter': 'X/Twitter',
        'soundcloud': 'SoundCloud',
    }
    platform_labels_en = {
        'youtube': 'YouTube',
        'instagram': 'Instagram',
        'tiktok': 'TikTok',
        'twitter': 'X/Twitter',
        'soundcloud': 'SoundCloud',
    }

    normalized_platform_counts = {
        str(key).strip().lower(): int(value or 0)
        for key, value in platform_counts_raw.items()
    }
    normalized_error_counts = {
        str(key).strip().lower(): int(value or 0)
        for key, value in error_category_counts_raw.items()
    }
    normalized_error_counts_24h = {
        str(key).strip().lower(): int(value or 0)
        for key, value in error_category_counts_24h_raw.items()
    }

    platform_lines: list[str] = []
    for key in supported_platform_keys:
        count = int(normalized_platform_counts.get(key, 0) or 0)
        if count <= 0:
            continue
        label = platform_labels_fa[key] if lang == 'fa' else platform_labels_en[key]
        platform_lines.append(f'• {label}: {count}')
    if not platform_lines:
        platform_lines.append('• هنوز دانلودی ثبت نشده.' if lang == 'fa' else '• No downloads yet.')

    error_lines: list[str] = []
    error_lines_24h: list[str] = []
    for category in ERROR_CATEGORIES:
        count_total = int(normalized_error_counts.get(category, 0) or 0)
        count_24h = int(normalized_error_counts_24h.get(category, 0) or 0)
        if count_total <= 0 and count_24h <= 0:
            continue
        label = error_category_label(category, lang)
        error_lines.append(f'• {label}: {count_total}')
        error_lines_24h.append(f'• {label}: {count_24h}')
    if not error_lines:
        error_lines.append('• خطایی ثبت نشده.' if lang == 'fa' else '• No error recorded.')
    if not error_lines_24h:
        error_lines_24h.append('• خطایی ثبت نشده.' if lang == 'fa' else '• No error recorded.')

    total_uploaded = human_bytes(int(stats.get('uploaded_bytes') or 0))
    avg_uploaded = human_bytes(int(stats.get('avg_upload_bytes') or 0))
    daily_window = resolve_daily_window(ctx.daily_limit_reset_time, ctx.daily_limit_reset_tz)
    daily_success_used = await ctx.db.count_success_between(daily_window.start_utc, daily_window.end_utc)
    daily_global_limit_text = _format_daily_limit_value(lang, int(ctx.daily_global_success_limit))
    daily_success_used_text = _format_number(int(daily_success_used), lang) if lang == 'fa' else str(int(daily_success_used))

    if lang == 'fa':
        text = (
            f'👤 کاربران: {stats["users"]} نفر\n'
            '\n'
            '📥 دانلودها\n'
            f'• کل دانلودها: {stats["downloads"]}\n'
            f'• موفق: {stats["success"]}\n'
            f'• ناموفق: {stats["failed"]}\n'
            f'• ۲۴ ساعت اخیر: {stats["downloads_24h"]}\n'
            '\n'
            '💾 ترافیک\n'
            f'• حجم کل فایل‌های ارسال‌شده: {total_uploaded}\n'
            f'• میانگین حجم هر دانلود موفق: {avg_uploaded}\n\n'
            '🌐 تفکیک پلتفرم\n'
            f'{"\n".join(platform_lines)}\n\n'
            '🎯 محدودیت روزانه کل ربات\n'
            f'• دانلود موفق امروز: {daily_success_used_text}/{daily_global_limit_text}\n'
            f'• ریست: {daily_window.next_reset_local} ({daily_window.reset_tz})\n\n'
            '🚨 خطاها بر اساس دسته (کل)\n'
            f'{"\n".join(error_lines)}\n\n'
            '🕒 خطاها بر اساس دسته (۲۴ ساعت اخیر)\n'
            f'{"\n".join(error_lines_24h)}\n\n'
            '✅ نکته: این آمار بر اساس داده‌های ثبت‌شده در دیتابیس نمایش داده می‌شود و برای پایش روزانه کاربرد دارد.'
        )
    else:
        text = (
            f'👤 Users: {stats["users"]}\n'
            '\n'
            '📥 Downloads\n'
            f'• Total downloads: {stats["downloads"]}\n'
            f'• Success: {stats["success"]}\n'
            f'• Failed: {stats["failed"]}\n'
            f'• Last 24h: {stats["downloads_24h"]}\n'
            '\n'
            '💾 Traffic\n'
            f'• Total uploaded size: {total_uploaded}\n'
            f'• Average successful download size: {avg_uploaded}\n\n'
            '🌐 Platform breakdown\n'
            f'{"\n".join(platform_lines)}\n\n'
            '🎯 Bot daily quota\n'
            f'• Success today: {daily_success_used_text}/{daily_global_limit_text}\n'
            f'• Reset: {daily_window.next_reset_local} ({daily_window.reset_tz})\n\n'
            '🚨 Error categories (all)\n'
            f'{"\n".join(error_lines)}\n\n'
            '🕒 Error categories (last 24h)\n'
            f'{"\n".join(error_lines_24h)}\n\n'
            '✅ Note: These stats are based on stored database records and are useful for daily monitoring.'
        )

    await message.answer(
        text,
        reply_markup=build_admin_panel_keyboard(lang),
    )


async def _send_user_stats(
    message: Message,
    ctx: AppContext,
    lang: str,
    target_user_id: int,
) -> None:
    stats = await ctx.db.get_user_detailed_stats(target_user_id)
    platform_counts_raw = stats.get('platform_counts') or {}
    error_category_counts_raw = stats.get('error_category_counts') or {}
    supported_platform_keys = ['youtube', 'instagram', 'tiktok', 'twitter', 'soundcloud']
    platform_labels_fa = {
        'youtube': 'YouTube',
        'instagram': 'Instagram',
        'tiktok': 'TikTok',
        'twitter': 'X/Twitter',
        'soundcloud': 'SoundCloud',
    }
    platform_labels_en = {
        'youtube': 'YouTube',
        'instagram': 'Instagram',
        'tiktok': 'TikTok',
        'twitter': 'X/Twitter',
        'soundcloud': 'SoundCloud',
    }

    normalized_platform_counts = {
        str(key).strip().lower(): int(value or 0)
        for key, value in platform_counts_raw.items()
    }
    normalized_error_counts = {
        str(key).strip().lower(): int(value or 0)
        for key, value in error_category_counts_raw.items()
    }

    platform_lines: list[str] = []
    for key in supported_platform_keys:
        count = int(normalized_platform_counts.get(key, 0) or 0)
        if count <= 0:
            continue
        label = platform_labels_fa[key] if lang == 'fa' else platform_labels_en[key]
        platform_lines.append(f'• {label}: {count}')
    if not platform_lines:
        platform_lines.append('• هنوز دانلودی ثبت نشده.' if lang == 'fa' else '• No downloads yet.')

    error_lines: list[str] = []
    for category in ERROR_CATEGORIES:
        count_total = int(normalized_error_counts.get(category, 0) or 0)
        if count_total <= 0:
            continue
        label = error_category_label(category, lang)
        error_lines.append(f'• {label}: {count_total}')
    if not error_lines:
        error_lines.append('• خطایی ثبت نشده.' if lang == 'fa' else '• No error recorded.')

    user_id_text = _format_number(int(stats.get('user_id') or target_user_id), lang)
    username = str(stats.get('username') or '-')
    if username != '-' and not username.startswith('@'):
        username = f'@{username}'
    first_name = str(stats.get('first_name') or '-')
    joined_at = str(stats.get('created_at') or '-')
    total_uploaded = human_bytes(int(stats.get('uploaded_bytes') or 0))
    avg_uploaded = human_bytes(int(stats.get('avg_upload_bytes') or 0))
    target_role = await ctx.db.get_bot_admin_role(target_user_id)
    target_limit = _resolve_per_user_daily_limit(ctx=ctx, role=target_role)
    daily_window = resolve_daily_window(ctx.daily_limit_reset_time, ctx.daily_limit_reset_tz)
    target_daily_used = await ctx.db.count_user_success_between(
        user_id=target_user_id,
        start_utc=daily_window.start_utc,
        end_utc=daily_window.end_utc,
    )
    target_daily_used_text = _format_number(int(target_daily_used), lang) if lang == 'fa' else str(int(target_daily_used))
    target_limit_text = _format_daily_limit_value(lang, target_limit)

    if lang == 'fa':
        text = (
            f'👤 گزارش کاربر {user_id_text}\n'
            f'• یوزرنیم: {username}\n'
            f'• نام: {first_name}\n'
            f'• زمان ثبت: {joined_at}\n'
            '\n'
            '📥 دانلودها\n'
            f'• کل دانلودها: {_format_number(int(stats.get("downloads") or 0), lang)}\n'
            f'• موفق: {_format_number(int(stats.get("success") or 0), lang)}\n'
            f'• ناموفق: {_format_number(int(stats.get("failed") or 0), lang)}\n'
            f'• ۲۴ ساعت اخیر: {_format_number(int(stats.get("downloads_24h") or 0), lang)}\n'
            '\n'
            '💾 ترافیک\n'
            f'• حجم کل فایل‌های ارسال‌شده: {total_uploaded}\n'
            f'• میانگین حجم هر دانلود موفق: {avg_uploaded}\n\n'
            '🎯 سهمیه روزانه\n'
            f'• امروز: {target_daily_used_text}/{target_limit_text}\n'
            f'• ریست: {daily_window.next_reset_local} ({daily_window.reset_tz})\n\n'
            '🌐 تفکیک پلتفرم\n'
            f'{"\n".join(platform_lines)}\n\n'
            '🚨 دسته‌بندی خطاهای کاربر\n'
            f'{"\n".join(error_lines)}'
        )
    else:
        text = (
            f'👤 User report {user_id_text}\n'
            f'• Username: {username}\n'
            f'• Name: {first_name}\n'
            f'• Joined at: {joined_at}\n'
            '\n'
            '📥 Downloads\n'
            f'• Total downloads: {int(stats.get("downloads") or 0)}\n'
            f'• Success: {int(stats.get("success") or 0)}\n'
            f'• Failed: {int(stats.get("failed") or 0)}\n'
            f'• Last 24h: {int(stats.get("downloads_24h") or 0)}\n'
            '\n'
            '💾 Traffic\n'
            f'• Total uploaded size: {total_uploaded}\n'
            f'• Average successful download size: {avg_uploaded}\n\n'
            '🎯 Daily quota\n'
            f'• Today: {target_daily_used_text}/{target_limit_text}\n'
            f'• Reset: {daily_window.next_reset_local} ({daily_window.reset_tz})\n\n'
            '🌐 Platform breakdown\n'
            f'{"\n".join(platform_lines)}\n\n'
            '🚨 User error categories\n'
            f'{"\n".join(error_lines)}'
        )

    await message.answer(
        text,
        reply_markup=build_admin_panel_keyboard(lang),
    )


async def _edit_message_content(
    message: Message,
    text: str,
    reply_markup=None,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return
    except TelegramBadRequest:
        pass

    try:
        await message.edit_caption(caption=text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=reply_markup)


async def _clear_inline_keyboard(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        return


def _is_group_chat(chat_type) -> bool:
    value = str(getattr(chat_type, 'value', chat_type)).lower()
    return value in {'group', 'supergroup'}


def _is_private_chat(chat_type) -> bool:
    value = str(getattr(chat_type, 'value', chat_type)).lower()
    return value == 'private'


def _chat_member_status_value(member) -> str:
    status = getattr(member, 'status', '')
    return str(getattr(status, 'value', status)).lower()


async def _track_group_chat_from_message(db, message: Message) -> None:
    if not _is_group_chat(message.chat.type):
        return
    chat_id = int(message.chat.id)
    now = time.time()
    next_allowed_at = _GROUP_TRACK_NEXT_ALLOWED_AT.get(chat_id, 0.0)
    if now < next_allowed_at:
        return

    await db.upsert_group_chat(
        chat_id=chat_id,
        username=message.chat.username,
        title=message.chat.title,
    )
    _GROUP_TRACK_NEXT_ALLOWED_AT[chat_id] = now + GROUP_TRACK_REFRESH_SECONDS

    # Keep memory bounded for long-lived processes with many one-off groups.
    if len(_GROUP_TRACK_NEXT_ALLOWED_AT) > 5000:
        expired_chat_ids = [
            tracked_chat_id
            for tracked_chat_id, allowed_at in _GROUP_TRACK_NEXT_ALLOWED_AT.items()
            if allowed_at <= now
        ]
        for tracked_chat_id in expired_chat_ids:
            _GROUP_TRACK_NEXT_ALLOWED_AT.pop(tracked_chat_id, None)


async def _send_group_welcome(*, event: ChatMemberUpdated, ctx: AppContext) -> None:
    text = _group_welcome_text()
    photo_path = ctx.group_welcome_photo_path
    if photo_path is not None and photo_path.exists() and photo_path.is_file():
        try:
            await event.bot.send_photo(
                chat_id=event.chat.id,
                photo=FSInputFile(str(photo_path), filename=photo_path.name),
                caption=text,
            )
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.exception('Failed to send group welcome photo to %s', event.chat.id)

    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await event.bot.send_message(chat_id=event.chat.id, text=text)


async def _is_request_blocked(db, user_id: int, chat_id: int) -> bool:
    if await db.is_user_blocked(user_id):
        return True
    if chat_id < 0 and await db.is_group_blocked(chat_id):
        return True
    return False


async def _notify_block_status_to_target(
    *,
    bot: Bot,
    db,
    target_id: int,
    target_type: str,
    blocked: bool,
    fallback_lang: str,
) -> bool:
    normalized_type = 'group' if str(target_type).strip().lower() == 'group' else 'user'
    target_lang = fallback_lang
    if normalized_type == 'user':
        target_lang = await db.get_user_language(target_id)

    try:
        await bot.send_message(
            chat_id=target_id,
            text=_target_block_status_text(
                lang=target_lang,
                target_type=normalized_type,
                blocked=blocked,
            ),
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.exception('Failed to notify blocked target %s', target_id)
        return False


def _build_loading_caption(title: str, lang: str) -> str:
    title_text = safe_caption(title, limit=980)
    loading = 'در حال بارگیری...' if lang == 'fa' else 'Loading...'
    if title_text:
        return f'🍿 {title_text}\n\n{loading}'
    return loading


def _start_welcome_text(lang: str) -> str:
    if lang == 'fa':
        return 'سلام 👋\n\nبه روبو دانلود خوش اومدی\nلینک بفرست تا برات دانلود کنم\n\n@RoboDownload'
    return 'Hello 👋\n\nWelcome to RoboDownload\nSend a link and I will download it for you.\n\n@RoboDownload'


def _group_welcome_text() -> str:
    return (
        'سلام، من RoboDownload هستم 👋\n\n'
        'از این لحظه داخل گروه فعالم و لینک‌های پشتیبانی‌شده را دانلود می‌کنم.\n'
        '\n'
        'پلتفرم‌های پشتیبانی‌شده:\n'
        '🎬 YouTube\n'
        '📸 Instagram\n'
        '🎵 SoundCloud\n'
        '🎥 TikTok\n'
        '✖️ X / Twitter\n\n'
        'کافی است لینک را در گروه بفرستید.'
    )


class _AdminStateFilter(Filter):
    def __init__(self, states: dict[int, str]) -> None:
        self._states = states

    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        if not _is_private_chat(message.chat.type):
            return False
        if message.text and message.text.startswith('/'):
            return False
        return message.from_user.id in self._states


def _admin_action_prompt(lang: str, state: str) -> str:
    if lang == 'fa':
        if state == ADMIN_STATE_WAIT_FSUB_ADD:
            return (
                '👨‍💻 یک پیام از کانال موردنظر به ربات فوروارد کنید:\n\n'
                '⚠️ ربات باید در کانالی که پیام را از آن فوروارد می کنید مدیر باشد.'
            )
        if state == ADMIN_STATE_WAIT_FSUB_REMOVE:
            return (
                'یوزرنیم یا chat_id کانال را بفرست تا حذف شود.\n'
                'مثال: @channel_username یا -1001234567890'
            )
    else:
        if state == ADMIN_STATE_WAIT_FSUB_ADD:
            return (
                'Forward a message from the target channel to the bot.\n\n'
                'The bot must be admin in that channel.'
            )
        if state == ADMIN_STATE_WAIT_FSUB_REMOVE:
            return (
                'Send channel username or chat_id to remove.\n'
                'Example: @channel_username or -1001234567890'
            )
    return ''


def _broadcast_target_select_text(lang: str) -> str:
    if lang == 'fa':
        return (
            '🪧 مقصد ارسال پیام همگانی را انتخاب کنید:\n\n'
            '👤 کاربران: ارسال به کاربران خصوصی ربات.\n\n'
            '👥 گروه‌ها: ارسال به گروه‌هایی که ربات در آن‌ها عضو است.'
        )
    return (
        '🪧 Choose broadcast target:\n\n'
        '👤 Users: send to private bot users.\n\n'
        '👥 Groups: send to groups where the bot is a member.'
    )


def _broadcast_mode_select_text(lang: str, target: str) -> str:
    target_label_fa = 'کاربران' if target == 'users' else 'گروه‌ها'
    target_label_en = 'Users' if target == 'users' else 'Groups'
    if lang == 'fa':
        return (
            f'🪧 مقصد انتخاب‌شده: {target_label_fa}\n'
            'حالا نوع ارسال را انتخاب کنید:\n\n'
            '📩 عادی: ارسال پیام بدون فوروارد، فقط ارسال متن امکان پذیر است.\n\n'
            '↗️ فوروارد: ارسال پیام با فوروارد کردن، ارسال هرگونه پیامی امکان پذیر است.'
        )
    return (
        f'🪧 Selected target: {target_label_en}\n'
        'Now choose broadcast type:\n\n'
        '📩 Normal: send non-forwarded message, text only.\n\n'
        '↗️ Forward: forward a message, any message type is supported.'
    )


def _broadcast_normal_prompt_text(lang: str) -> str:
    if lang == 'fa':
        return '👨‍💻 پیام موردنظر را ارسال کنید:'
    return 'Send the broadcast text now (text only).'


def _broadcast_forward_prompt_text(lang: str) -> str:
    if lang == 'fa':
        return '👨‍💻 پیام موردنظر را از یک کانال فوروارد کنید:'
    return 'Send the message to forward now.'


def _broadcast_forward_required_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ پیام را از یک کانال فوروارد کنید.'
    return '⛔️ Please forward a message from a channel.'


def _broadcast_invalid_mode_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ فقط یکی از دو گزینه «📩 عادی» یا «↗️ فوروارد» را انتخاب کنید.'
    return '⛔️ Please choose either "📩 Normal" or "↗️ Forward".'


def _broadcast_invalid_target_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ فقط یکی از دو گزینه «👤 کاربران» یا «👥 گروه‌ها» را انتخاب کنید.'
    return '⛔️ Please choose either "👤 Users" or "👥 Groups".'


def _resolve_broadcast_target(text: str) -> str | None:
    value = text.strip()
    if value in broadcast_target_button_variants('broadcast_target_users'):
        return 'users'
    if value in broadcast_target_button_variants('broadcast_target_groups'):
        return 'groups'
    return None


def _resolve_broadcast_mode(text: str) -> str | None:
    value = text.strip()
    if value in broadcast_mode_button_variants('broadcast_mode_normal'):
        return 'normal'
    if value in broadcast_mode_button_variants('broadcast_mode_forward'):
        return 'forward'
    return None


def _rate_limited_text(lang: str, reason_code: str | None, wait_seconds: int | None) -> str:
    if reason_code == 'active_job':
        return tr(lang, 'rate_limited_active_job')
    if reason_code == 'cooldown':
        seconds = max(1, int(wait_seconds or 1))
        return tr(lang, 'rate_limited_wait', seconds=seconds)
    return tr(lang, 'rate_limited')


async def _enforce_daily_limits_for_start(ctx: AppContext, user_id: int, lang: str) -> tuple[bool, str]:
    daily_window = resolve_daily_window(ctx.daily_limit_reset_time, ctx.daily_limit_reset_tz)
    global_used = await ctx.db.count_success_between(daily_window.start_utc, daily_window.end_utc)
    global_limit = int(ctx.daily_global_success_limit)
    if global_limit > 0 and global_used >= global_limit:
        used_text = _format_number(global_used, lang) if lang == 'fa' else str(global_used)
        limit_text = _format_number(global_limit, lang) if lang == 'fa' else str(global_limit)
        return (
            False,
            tr(
                lang,
                'daily_limit_global_reached',
                used=used_text,
                limit=limit_text,
                reset_at=daily_window.next_reset_local,
                tz=daily_window.reset_tz,
            ),
        )

    role = await _get_user_admin_role(ctx=ctx, user_id=user_id)
    per_user_limit = _resolve_per_user_daily_limit(ctx=ctx, role=role)
    if per_user_limit is None or per_user_limit <= 0:
        return True, ''

    user_used = await ctx.db.count_user_success_between(
        user_id=user_id,
        start_utc=daily_window.start_utc,
        end_utc=daily_window.end_utc,
    )
    if user_used >= per_user_limit:
        used_text = _format_number(user_used, lang) if lang == 'fa' else str(user_used)
        limit_text = _format_number(per_user_limit, lang) if lang == 'fa' else str(per_user_limit)
        message_key = 'daily_limit_admin_reached' if role == 'admin' else 'daily_limit_user_reached'
        return (
            False,
            tr(
                lang,
                message_key,
                used=used_text,
                limit=limit_text,
                reset_at=daily_window.next_reset_local,
                tz=daily_window.reset_tz,
            ),
        )
    return True, ''


async def _get_user_admin_role(ctx: AppContext, user_id: int) -> str | None:
    if user_id not in ctx.admin_ids:
        return None
    return await ctx.db.get_bot_admin_role(user_id)


def _resolve_per_user_daily_limit(ctx: AppContext, role: str | None) -> int | None:
    normalized_role = str(role or '').strip().lower()
    if normalized_role == 'owner':
        return None if ctx.daily_owner_unlimited else int(ctx.daily_admin_success_limit)
    if normalized_role == 'admin':
        return int(ctx.daily_admin_success_limit)
    return int(ctx.daily_user_success_limit)


def _format_daily_limit_value(lang: str, limit: int | None) -> str:
    if limit is None or int(limit) <= 0:
        return tr(lang, 'daily_limit_unlimited')
    if lang == 'fa':
        return _format_number(int(limit), lang)
    return str(int(limit))


def _resolve_cookie_platform(text: str) -> str | None:
    value = str(text or '').strip()
    if not value:
        return None
    lower_value = value.lower()
    if lower_value in COOKIE_PLATFORM_KEYS:
        return lower_value
    for platform in COOKIE_PLATFORM_KEYS:
        if value in cookie_platform_button_variants(platform):
            return platform
    return None


def _admin_cookie_menu_text(lang: str) -> str:
    if lang == 'fa':
        return (
            '🍪 مدیریت کوکی\n\n'
            'در این بخش می‌توانید برای هر پلتفرم کوکی جدا ثبت یا حذف کنید.\n'
            'این تنظیمات در دیتابیس ذخیره می‌شود و بعد از ری‌استارت ربات هم می‌ماند.'
        )
    return (
        '🍪 Cookie management\n\n'
        'Here you can set/remove a separate cookie for each platform.\n'
        'Cookies are saved in database and persist after restart.'
    )


def _admin_cookie_list_text(lang: str, cookies: list[dict[str, str]]) -> str:
    cookie_map = {
        str(item.get('platform') or '').strip().lower(): str(item.get('updated_at') or '-')
        for item in cookies
        if item.get('platform')
    }
    lines: list[str] = []
    if lang == 'fa':
        lines.extend(['📜 وضعیت کوکی پلتفرم‌ها', ''])
        for platform in COOKIE_PLATFORM_KEYS:
            label = cookie_platform_label(platform, lang)
            updated_at = cookie_map.get(platform)
            if updated_at:
                lines.append(f'• {label}: ✅ تنظیم شده ({updated_at})')
            else:
                lines.append(f'• {label}: ⛔️ تنظیم نشده')
    else:
        lines.extend(['📜 Platform cookie status', ''])
        for platform in COOKIE_PLATFORM_KEYS:
            label = cookie_platform_label(platform, lang)
            updated_at = cookie_map.get(platform)
            if updated_at:
                lines.append(f'• {label}: ✅ Set ({updated_at})')
            else:
                lines.append(f'• {label}: ⛔️ Not set')
    return '\n'.join(lines)


def _admin_cookie_pick_platform_text(lang: str, action: str) -> str:
    if lang == 'fa':
        if action == 'remove':
            return '⚠️ پلتفرم موردنظر برای حذف کوکی را انتخاب کنید:'
        return '⚠️ پلتفرم موردنظر برای ثبت/ویرایش کوکی را انتخاب کنید:'
    if action == 'remove':
        return '⚠️ Select a platform to remove cookie:'
    return '⚠️ Select a platform to set/update cookie:'


def _admin_cookie_set_payload_prompt_text(lang: str, platform: str) -> str:
    platform_label = cookie_platform_label(platform, lang)
    if lang == 'fa':
        return (
            f'📝 کوکی پلتفرم {platform_label} را ارسال کنید.\n\n'
            'می‌توانید به صورت متن یا فایل `.txt` بفرستید.'
        )
    return (
        f'📝 Send cookie for {platform_label}.\n\n'
        'You can send it as plain text or `.txt` document.'
    )


def _admin_cookie_invalid_platform_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ پلتفرم نامعتبر است. یکی از گزینه‌های کیبورد را انتخاب کنید.'
    return '⛔️ Invalid platform. Please select one of the keyboard options.'


def _admin_cookie_payload_invalid_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ محتوای کوکی نامعتبر است. متن یا فایل کوکی معتبر ارسال کنید.'
    return '⛔️ Invalid cookie payload. Send a valid cookie text/file.'


def _admin_cookie_store_error_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ ذخیره کوکی با خطا مواجه شد. دوباره تلاش کنید.'
    return '⛔️ Failed to save cookie. Please try again.'


def _admin_cookie_set_success_text(lang: str, platform: str) -> str:
    platform_label = cookie_platform_label(platform, lang)
    if lang == 'fa':
        return f'✅ کوکی {platform_label} با موفقیت ذخیره شد.'
    return f'✅ Cookie for {platform_label} saved successfully.'


def _admin_cookie_remove_empty_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ هنوز هیچ کوکی‌ای ثبت نشده است.'
    return '⛔️ No cookies are configured yet.'


def _admin_cookie_remove_success_text(lang: str, platform: str) -> str:
    platform_label = cookie_platform_label(platform, lang)
    if lang == 'fa':
        return f'✅ کوکی {platform_label} حذف شد.'
    return f'✅ Cookie for {platform_label} removed.'


async def _read_cookie_document_text(message: Message, max_bytes: int) -> str:
    if message.document is None:
        return ''
    doc = message.document
    if doc.file_size is not None and int(doc.file_size) > max_bytes:
        return ''
    file = await message.bot.get_file(doc.file_id)
    file_path = str(file.file_path or '').strip()
    if not file_path:
        return ''
    payload = io.BytesIO()
    await message.bot.download_file(file_path, payload)
    raw = payload.getvalue()
    if len(raw) > max_bytes:
        return ''
    return raw.decode('utf-8-sig', errors='replace').strip()


def _looks_like_cookie_payload(payload: str | None) -> bool:
    text = str(payload or '').strip()
    if len(text) < 12:
        return False
    lowered = text.lower()
    if 'netscape http cookie file' in lowered:
        return True
    if '\t' in text and '=' in text:
        return True
    if '=' in text and (';' in text or '\n' in text):
        return True
    return False


def _format_number(value: int, lang: str) -> str:
    if lang != 'fa':
        return str(value)
    return str(value).translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))


def _available_modes_within_limit(
    mode_candidates: list[DownloadMode],
    mode_size_bytes: dict[DownloadMode, int | None],
    max_file_size_bytes: int,
) -> list[DownloadMode]:
    available: list[DownloadMode] = []
    for mode in mode_candidates:
        estimated_size = mode_size_bytes.get(mode)
        if is_video_resolution_mode(mode) and estimated_size is None:
            continue
        if estimated_size is not None and int(estimated_size) > int(max_file_size_bytes):
            continue
        available.append(mode)
    return available


def _mode_exceeds_limit_text(lang: str, mode: DownloadMode, max_file_size_bytes: int) -> str:
    limit_text = human_bytes(max_file_size_bytes)
    mode_text = mode_label(mode, lang)
    if lang == 'fa':
        return f'⛔️ حالت {mode_text} از محدودیت {limit_text} بیشتر است و قابل ارسال نیست.'
    return f'⛔️ {mode_text} is larger than the {limit_text} limit and cannot be sent.'


def _no_modes_under_limit_text(lang: str, max_file_size_bytes: int) -> str:
    limit_text = human_bytes(max_file_size_bytes)
    if lang == 'fa':
        return '⛔️ حجم ویدیو بالا است'
    return f'⛔️ No downloadable mode was found under the {limit_text} limit.'


def _youtube_duration_limit_text(lang: str, limit_minutes: int) -> str:
    if lang == 'fa':
        return f'⛔️ ویدیوهای بالای {limit_minutes} دقیقه پشتیبانی نمی‌شوند.'
    return f'⛔️ Videos longer than {limit_minutes} minutes are not supported.'


def _broadcast_queued_text(lang: str) -> str:
    if lang == 'fa':
        return '🚚 پیام همگانی در صف ارسال قرار گرفت و به زودی شروع می شود.'
    return '🚚 Broadcast is queued and will start soon.'


def _broadcast_queued_text_for_target(lang: str, target: str) -> str:
    if target == 'groups':
        if lang == 'fa':
            return '🚚 پیام همگانی برای گروه‌ها در صف ارسال قرار گرفت و به زودی شروع می شود.'
        return '🚚 Group broadcast is queued and will start soon.'
    if lang == 'fa':
        return _broadcast_queued_text(lang)
    return _broadcast_queued_text(lang)


def _broadcast_done_text_for_target(
    lang: str,
    target: str,
    total: int,
    sent: int,
    failed: int,
) -> str:
    total_text = _format_number(total, lang)
    sent_text = _format_number(sent, lang)
    failed_text = _format_number(failed, lang)
    target_fa = 'گروه‌ها' if target == 'groups' else 'کاربران'
    target_en = 'groups' if target == 'groups' else 'users'
    if lang == 'fa':
        return (
            f'✅ پیام همگانی برای {target_fa} ارسال شد.\n\n'
            f'👤 کل مقصدها: {total_text}\n'
            f'🟢 ارسال‌های موفق: {sent_text}\n'
            f'🔴 ارسال‌های ناموفق: {failed_text}'
        )
    return (
        f'✅ Broadcast sent to {target_en}.\n\n'
        f'👤 Total targets: {total_text}\n'
        f'🟢 Successful sends: {sent_text}\n'
        f'🔴 Failed sends: {failed_text}'
    )


def _force_sub_remove_select_text(lang: str) -> str:
    if lang == 'fa':
        return '⚠️ کانال مورد نظر رو برای حذف انتخاب کنید:'
    return '⚠️ Select a channel to remove:'


def _admin_cancel_text(lang: str) -> str:
    if lang == 'fa':
        return 'عملیات لغو شد.'
    return 'Canceled.'


def _admin_force_sub_menu_text(lang: str) -> str:
    if lang == 'fa':
        return (
            '📍 در این بخش میتوانید فعال بودن یا غیرفعال بودن عضویت اجباری و کانال های آن را کنترل کنید.\n\n'
            '⁉️ در صورتی که عضویت اجباری را فعال کرده باشید، کاربر برای ادامه کار با ربات باید در کانال هایی که در این بخش تنظیم شده باشد عضو شود.'
        )
    return 'Force-sub section: check status, add/remove channels, or disable it.'


def _admin_managers_menu_text(lang: str) -> str:
    if lang == 'fa':
        return (
            '📍 در این بخش میتوانید مديران ربات را کنترل کرده و لیست مدیران کنونی را مشاهده کنید.\n\n'
            '⁉️ مدیران اضافه شده به ربات، به پنل مدیریت ربات دسترسی دارند.'
        )
    return (
        '📍 In this section you can manage bot admins and view the current admin list.\n\n'
        '⁉️ Added admins have access to the bot management panel.'
    )


def _admin_managers_list_text(lang: str, admins: list[dict[str, int | str]]) -> str:
    if lang == 'fa':
        lines = ['✅ مدیران کنونی ربات', '']
        if not admins:
            lines.append('• مدیری ثبت نشده است.')
        else:
            for admin in admins:
                role = str(admin.get('role') or 'admin').strip().lower()
                role_label = 'مدیرکل' if role == 'owner' else 'مدیر'
                admin_id = _format_number(int(admin.get('user_id') or 0), lang)
                lines.append(f'• {role_label} - {admin_id}')
        lines.extend(
            [
                '',
                '👨‍💻 برای افزودن و یا حذف یک مدیر می توانید از منوی زیر استفاده کنید.',
            ]
        )
        return '\n'.join(lines)

    lines = ['✅ Current bot admins', '']
    if not admins:
        lines.append('• No admins found.')
    else:
        for admin in admins:
            role = str(admin.get('role') or 'admin').strip().lower()
            role_label = 'Owner' if role == 'owner' else 'Admin'
            admin_id = _format_number(int(admin.get('user_id') or 0), lang)
            lines.append(f'• {role_label} - {admin_id}')
    lines.extend(['', '👨‍💻 Use the menu below to add or remove an admin.'])
    return '\n'.join(lines)


def _admin_add_owner_only_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ فقط مدیرکل می تواند مدیر جدید اضافه کند.'
    return '⛔️ Only the owner can add a new admin.'


def _admin_remove_owner_only_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ فقط مدیرکل می تواند یک مدیر را حذف کند.'
    return '⛔️ Only the owner can remove an admin.'


def _admin_user_inspect_prompt_text(lang: str) -> str:
    if lang == 'fa':
        return '🔎 شناسه عددی کاربر را وارد کنید:'
    return '🔎 Enter target numeric user ID:'


def _admin_user_inspect_invalid_input_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ ورودی نامعتبر است. فقط شناسه عددی کاربر مجاز است.'
    return '⛔️ Invalid input. Only numeric user ID is allowed.'


def _admin_block_owner_only_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ فقط مدیرکل می تواند کاربر یا گروه را مسدود کند.'
    return '⛔️ Only the owner can block a user or group.'


def _admin_backup_owner_only_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ فقط مدیرکل می‌تواند دیتابیس و لاگ را دریافت کند.'
    return '⛔️ Only the owner can get the database and logs.'


def _admin_backup_preparing_text(lang: str) -> str:
    if lang == 'fa':
        return '⏳ در حال آماده‌سازی فایل دیتابیس و لاگ...'
    return '⏳ Preparing database and log backup...'


def _admin_backup_failed_text(lang: str) -> str:
    if lang == 'fa':
        return '⚠️ خطا در ساخت یا ارسال فایل دیتابیس و لاگ.'
    return '⚠️ Failed to create or send database and log backup.'


def _admin_block_prompt_text(lang: str) -> str:
    if lang == 'fa':
        return (
            '🔒 شناسه کاربر یا گروه را وارد کنید:\n'
            '• کاربر: عدد مثبت (مثال: 123456789)\n'
            '• گروه: chat_id منفی (مثال: -1001234567890)'
        )
    return (
        '🔒 Enter target user/group ID:\n'
        '• User: positive integer (example: 123456789)\n'
        '• Group: negative chat_id (example: -1001234567890)'
    )


def _admin_block_invalid_input_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ ورودی نامعتبر است. شناسه معتبر کاربر یا گروه را ارسال کنید.'
    return '⛔️ Invalid input. Send a valid user/group ID.'


def _admin_block_admin_forbidden_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ امکان مسدود کردن مدیران ربات وجود ندارد.'
    return '⛔️ Bot admins cannot be blocked.'


def _admin_block_success_text(lang: str, target_id: int, target_type: str) -> str:
    rendered_id = _format_number(abs(int(target_id)), lang)
    if lang == 'fa':
        if target_type == 'group':
            return f'✅ گروه {rendered_id} با موفقیت مسدود شد.'
        return f'✅ کاربر {rendered_id} با موفقیت مسدود شد.'
    if target_type == 'group':
        return f'✅ Group {rendered_id} was blocked successfully.'
    return f'✅ User {rendered_id} was blocked successfully.'


def _admin_unblock_success_text(lang: str, target_id: int) -> str:
    rendered_id = _format_number(abs(int(target_id)), lang)
    if lang == 'fa':
        return f'✅ مسدودی شناسه {rendered_id} با موفقیت حذف شد.'
    return f'✅ Block removed for ID {rendered_id}.'


def _target_block_status_text(*, lang: str, target_type: str, blocked: bool) -> str:
    normalized_lang = str(lang).strip().lower()
    normalized_type = str(target_type).strip().lower()
    if normalized_lang == 'en':
        if blocked:
            if normalized_type == 'group':
                return (
                    '⛔️ This group has been blocked by the bot owner.\n'
                    'Links from this group will no longer be processed.'
                )
            return '⛔️ Your access to this bot has been blocked.'
        if normalized_type == 'group':
            return (
                '✅ This group has been unblocked.\n'
                'Links from this group will be processed again.'
            )
        return '✅ Your access to this bot has been restored.'

    if blocked:
        if normalized_type == 'group':
            return (
                '⛔️ این گروه توسط مدیرکل ربات مسدود شد.\n'
                'از این به بعد لینک‌های این گروه پردازش نمی‌شود.'
            )
        return '⛔️ دسترسی شما به ربات مسدود شد.'
    if normalized_type == 'group':
        return (
            '✅ مسدودی این گروه برداشته شد.\n'
            'از این به بعد لینک‌های این گروه دوباره پردازش می‌شود.'
        )
    return '✅ دسترسی شما به ربات دوباره فعال شد.'


def _with_target_notification_status(text: str, *, lang: str, notified: bool) -> str:
    if notified:
        return text
    if lang == 'fa':
        return f'{text}\n\n⚠️ پیام اطلاع‌رسانی به مقصد ارسال نشد.'
    return f'{text}\n\n⚠️ The notification message could not be delivered.'


def _admin_blocks_menu_text(lang: str) -> str:
    if lang == 'fa':
        return (
            '📍 مدیریت مسدودی‌ها\n\n'
            'در این بخش می‌توانید لیست مسدودی‌ها را ببینید، مسدودی جدید اضافه کنید یا حذف کنید.'
        )
    return (
        '📍 Block management\n\n'
        'Here you can view blocked targets, add a new block, or remove a block.'
    )


def _admin_blocks_list_text(lang: str, blocked: list[dict[str, int | str]]) -> str:
    if lang == 'fa':
        lines = ['✅ لیست مسدودی‌ها', '']
        if not blocked:
            lines.append('• موردی ثبت نشده است.')
        else:
            for item in blocked:
                target_id = int(item.get('target_id') or 0)
                target_type = str(item.get('target_type') or '').strip().lower()
                label = 'گروه' if target_type == 'group' else 'کاربر'
                rendered_id = str(target_id)
                lines.append(f'• {label} - {rendered_id}')
        return '\n'.join(lines)

    lines = ['✅ Blocked targets', '']
    if not blocked:
        lines.append('• No blocked targets found.')
    else:
        for item in blocked:
            target_id = int(item.get('target_id') or 0)
            target_type = str(item.get('target_type') or '').strip().lower()
            label = 'Group' if target_type == 'group' else 'User'
            lines.append(f'• {label} - {target_id}')
    return '\n'.join(lines)


def _admin_block_list_empty_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ لیست مسدودی‌ها خالی است.'
    return '⛔️ Blocked list is empty.'


def _admin_block_remove_select_text(lang: str) -> str:
    if lang == 'fa':
        return '⚠️ شناسه موردنظر را برای حذف مسدودی انتخاب کنید:'
    return '⚠️ Select target ID to remove block:'


def _admin_add_prompt_text(lang: str) -> str:
    if lang == 'fa':
        return '🔢 شناسه کاربر را وارد کنید:'
    return '🔢 Enter the user ID:'


def _admin_add_invalid_input_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ ورودی نامعتبر، فقط عدد صحیح مجاز است.'
    return '⛔️ Invalid input. Only a valid integer is allowed.'


def _admin_add_success_text(lang: str, user_id: int) -> str:
    rendered_id = _format_number(user_id, lang)
    if lang == 'fa':
        return f'✅ کاربر {rendered_id} با موفقیت مدیر ربات شد.'
    return f'✅ User {rendered_id} is now a bot admin.'


def _admin_remove_select_text(lang: str) -> str:
    if lang == 'fa':
        return '⚠️ مدیر مورد نظر رو برای حذف انتخاب کنید:'
    return '⚠️ Select an admin to remove:'


def _admin_remove_empty_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ لیست مدیران خالی است.'
    return 'ℹ️ Admin list is empty.'


def _admin_remove_success_text(lang: str, user_id: int) -> str:
    rendered_id = _format_number(user_id, lang)
    if lang == 'fa':
        return f'✅ کاربر {rendered_id} با موفقیت از مدیران ربات حذف شد.'
    return f'✅ User {rendered_id} was removed from bot admins.'


def _admin_home_text(lang: str) -> str:
    if lang == 'fa':
        return 'به پنل ادمین خوش اومدی 👋'
    return 'Welcome to the admin panel 👋'


def _blocked_access_text(lang: str) -> str:
    if lang == 'fa':
        return '⛔️ دسترسی شما به ربات مسدود شده است.'
    return '⛔️ Your access to this bot is blocked.'


async def _send_force_sub_status(
    message: Message,
    ctx: AppContext,
    lang: str,
    notice: str | None = None,
) -> None:
    enabled = await ctx.db.is_force_sub_enabled()
    channels = await ctx.db.list_required_channels()

    if lang == 'fa':
        status_text = 'فعال' if enabled else 'غیرفعال'
        channel_lines: list[str] = []
        for channel in channels:
            channel_id = abs(int(channel['chat_id']))
            channel_lines.append(f'• کانال - {channel_id}')
        if not channel_lines:
            channel_lines.append('• کانالی ثبت نشده است.')

        body = (
            '✅ وضعیت کنونی عضویت اجباری\n\n'
            f'🥷 عضویت اجباری: {status_text}\n\n'
            '📮 کانال ها:\n\n'
            f'{"\n".join(channel_lines)}\n\n'
            '👨‍💻 برای تنظیم کانال و کنترل عضویت اجباری از منوی زیر استفاده کنید.'
        )
    else:
        status_text = 'Enabled' if enabled else 'Disabled'
        channel_lines = [f'• Channel - {abs(int(ch["chat_id"]))}' for ch in channels]
        if not channel_lines:
            channel_lines.append('• No channels configured.')
        body = (
            '✅ Current force-sub status\n\n'
            f'🥷 Force-sub: {status_text}\n\n'
            '📮 Channels:\n\n'
            f'{"\n".join(channel_lines)}\n\n'
            '👨‍💻 Use the menu below to manage channels and force-sub settings.'
        )

    text = f'{notice}\n\n{body}' if notice else body
    await message.answer(
        text,
        reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=enabled),
    )


async def _add_required_channel(
    message: Message,
    db,
    lang: str,
    channel_ref: str | int,
) -> None:
    force_sub_enabled = await db.is_force_sub_enabled()
    try:
        chat = await message.bot.get_chat(channel_ref)
        me = await message.bot.get_me()
        await message.bot.get_chat_member(chat_id=chat.id, user_id=me.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer(
            tr(lang, 'force_sub_cannot_read'),
            reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=force_sub_enabled),
        )
        return

    if str(chat.type) not in {'channel', 'supergroup'}:
        await message.answer(
            tr(lang, 'force_sub_public_only'),
            reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=force_sub_enabled),
        )
        return

    username = (chat.username or '').strip()
    if not username:
        await message.answer(
            tr(lang, 'force_sub_public_only'),
            reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=force_sub_enabled),
        )
        return

    await db.add_required_channel(chat_id=chat.id, username=username, title=chat.title)
    await message.answer(
        tr(lang, 'force_sub_added', channel=f'@{username}'),
        reply_markup=build_force_sub_admin_keyboard(lang, force_sub_enabled=force_sub_enabled),
    )


async def _run_broadcast(
    message: Message,
    db,
    lang: str,
    target: str,
    mode: str,
    source_message: Message | None,
    text_payload: str,
) -> None:
    if target not in {'users', 'groups'}:
        await message.answer(
            _broadcast_invalid_target_text(lang),
            reply_markup=build_broadcast_target_keyboard(lang),
        )
        return

    if mode not in {'normal', 'forward'}:
        await message.answer(
            _broadcast_invalid_mode_text(lang),
            reply_markup=build_broadcast_mode_keyboard(lang),
        )
        return

    if mode == 'normal' and not text_payload.strip():
        await message.answer(
            _broadcast_normal_prompt_text(lang),
            reply_markup=build_force_sub_action_keyboard(lang),
        )
        return

    if mode == 'forward' and source_message is None:
        await message.answer(
            _broadcast_forward_prompt_text(lang),
            reply_markup=build_force_sub_action_keyboard(lang),
        )
        return

    target_chat_ids: list[int]
    if target == 'groups':
        target_chat_ids = [chat_id for chat_id in await db.list_broadcast_group_chat_ids() if int(chat_id) < 0]
    else:
        target_chat_ids = [user_id for user_id in await db.list_user_ids() if int(user_id) > 0]

    if not target_chat_ids:
        no_targets_text = (
            '⛔️ هیچ گروهی برای ارسال پیدا نشد.' if target == 'groups'
            else tr(lang, 'broadcast_no_users')
        ) if lang == 'fa' else (
            '⛔️ No groups found for broadcast.' if target == 'groups'
            else tr(lang, 'broadcast_no_users')
        )
        await message.answer(
            no_targets_text,
            reply_markup=build_admin_panel_keyboard(lang),
        )
        return

    await message.answer(
        _broadcast_queued_text_for_target(lang, target),
        reply_markup=build_admin_panel_keyboard(lang),
    )

    sent = 0
    blocked = 0
    failed = 0
    for target_chat_id in target_chat_ids:
        ok, state = await _send_broadcast_to_chat(
            bot=message.bot,
            target_chat_id=target_chat_id,
            source_message=source_message if mode == 'forward' else None,
            text_payload=text_payload if mode == 'normal' else '',
        )
        if ok:
            sent += 1
        elif state == 'blocked':
            blocked += 1
            if target == 'groups':
                await db.remove_group_chat(target_chat_id)
        else:
            failed += 1
        await asyncio.sleep(0.04)

    failed_total = blocked + failed
    await message.answer(
        _broadcast_done_text_for_target(
            lang=lang,
            target=target,
            total=len(target_chat_ids),
            sent=sent,
            failed=failed_total,
        ),
    )


def _parse_channel_reference(raw: str) -> str | int | None:
    value = raw.strip()
    if not value:
        return None

    if value.lstrip('-').isdigit():
        return int(value)

    lowered = value.lower()
    if lowered.startswith('http://') or lowered.startswith('https://'):
        try:
            parsed = urlparse(value)
        except ValueError:
            return None

        host = parsed.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        if host not in {'t.me', 'telegram.me'}:
            return None

        path = parsed.path.strip('/')
        if not path:
            return None
        username = path.split('/')[0].strip()
        if not username or username.startswith('+') or username in {'joinchat', 's'}:
            return None
        if _is_valid_telegram_username(username):
            return f'@{username}'
        return None

    if value.startswith('@'):
        username = value[1:].strip()
        if _is_valid_telegram_username(username):
            return f'@{username}'
        return None

    if _is_valid_telegram_username(value):
        return f'@{value}'
    return None


def _extract_forwarded_channel_reference(message: Message) -> str | int | None:
    origin = getattr(message, 'forward_origin', None)
    origin_chat = getattr(origin, 'chat', None)
    legacy_chat = getattr(message, 'forward_from_chat', None)
    chat = origin_chat or legacy_chat
    if chat is None:
        return None

    chat_type = str(getattr(chat, 'type', '')).lower()
    if chat_type not in {'channel', 'supergroup'}:
        return None

    username = str(getattr(chat, 'username', '') or '').strip()
    if username:
        return f'@{username}'

    chat_id = getattr(chat, 'id', None)
    if isinstance(chat_id, int):
        return chat_id
    try:
        return int(str(chat_id))
    except Exception:
        return None


def _is_valid_telegram_username(value: str) -> bool:
    return bool(re.fullmatch(r'[A-Za-z0-9_]{5,}', value))


async def _send_broadcast_to_chat(
    bot,
    target_chat_id: int,
    source_message: Message | None,
    text_payload: str,
) -> tuple[bool, str]:
    for attempt in range(2):
        try:
            if source_message:
                await bot.copy_message(
                    chat_id=target_chat_id,
                    from_chat_id=source_message.chat.id,
                    message_id=source_message.message_id,
                )
            else:
                await bot.send_message(target_chat_id, text_payload)
            return True, 'sent'
        except TelegramRetryAfter as exc:
            if attempt == 0:
                wait_for = float(getattr(exc, 'retry_after', 1.0)) + 0.2
                await asyncio.sleep(wait_for)
                continue
            return False, 'failed'
        except TelegramForbiddenError:
            return False, 'blocked'
        except TelegramBadRequest:
            return False, 'failed'
        except Exception:
            logger.exception('Broadcast failed for chat_id=%s', target_chat_id)
            return False, 'failed'
    return False, 'failed'


async def _ensure_membership_for_message(
    message: Message,
    ctx: AppContext,
    user_id: int,
    lang: str,
) -> bool:
    if not _is_private_chat(message.chat.type):
        return True

    missing_channels = await _missing_required_channels(
        bot=message.bot,
        db=ctx.db,
        user_id=user_id,
    )
    if not missing_channels:
        return True

    await message.answer(
        tr(lang, 'force_sub_required'),
        reply_markup=build_force_sub_keyboard(
            channels=missing_channels,
            check_label=tr(lang, 'force_sub_check'),
        ),
    )
    return False


async def _ensure_membership_for_callback(
    callback: CallbackQuery,
    ctx: AppContext,
    user_id: int,
    lang: str,
) -> bool:
    if not callback.message:
        return False
    if not _is_private_chat(callback.message.chat.type):
        return True

    missing_channels = await _missing_required_channels(
        bot=callback.message.bot,
        db=ctx.db,
        user_id=user_id,
    )
    if not missing_channels:
        return True

    await callback.answer(tr(lang, 'force_sub_still_missing'), show_alert=True)
    return False


async def _missing_required_channels(
    bot,
    db,
    user_id: int,
) -> list[dict[str, str]]:
    if not await db.is_force_sub_enabled():
        return []

    channels = await db.list_required_channels()
    missing: list[dict[str, str]] = []
    for channel in channels:
        chat_id = int(channel['chat_id'])
        is_member = await _is_user_channel_member(bot=bot, chat_id=chat_id, user_id=user_id)
        if is_member:
            continue

        username = str(channel.get('username') or '').strip()
        title = str(channel.get('title') or '').strip()
        label = title or (f'@{username}' if username else str(chat_id))
        url = f'https://t.me/{username}' if username else ''
        missing.append({'title': label, 'url': url})
    return missing


async def _is_user_channel_member(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramBadRequest as exc:
        logger.warning('Membership check failed for chat_id=%s user_id=%s: %s', chat_id, user_id, exc)
        return False
    except Exception as exc:
        logger.warning(
            'Membership check unexpected error for chat_id=%s user_id=%s: %s',
            chat_id,
            user_id,
            exc,
        )
        return False

    status = str(getattr(member, 'status', ''))
    if status in {'creator', 'administrator', 'member'}:
        return True
    if status == 'restricted' and bool(getattr(member, 'is_member', False)):
        return True
    return False
