from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db import Database
from app.i18n import tr
from app.models import DownloadMode, DownloadRequest, DownloadResult, Platform
from app.services.cookie_alert_notifier import maybe_send_cookie_expiry_alert
from app.services.error_classifier import (
    classify_download_error,
    compact_error_reason,
    should_hide_technical_reason,
)
from app.services.downloader import DownloaderError, DownloaderService, DownloadTimeoutError
from app.utils.formatting import human_bytes, safe_caption, safe_filename
from app.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SendOutcome:
    sent_files: int
    sent_bytes: int
    skipped_files: int
    cache_artifacts: list[dict] = field(default_factory=list)


class DownloadManager:
    def __init__(
        self,
        bot: Bot,
        db: Database,
        downloader: DownloaderService,
        limiter: RateLimiter,
        max_concurrent_downloads: int,
        max_file_size_mb: int,
        ffmpeg_binary: str = 'ffmpeg',
        download_timeout_seconds: int = 600,
        alert_enabled: bool = True,
        alert_threshold: int = 3,
        alert_window_minutes: int = 30,
        alert_cooldown_minutes: int = 300,
        alert_recipient_ids: set[int] | None = None,
    ) -> None:
        self._bot = bot
        self._db = db
        self._downloader = downloader
        self._limiter = limiter
        self._semaphore = asyncio.Semaphore(max_concurrent_downloads)
        self._max_size_bytes = max_file_size_mb * 1024 * 1024
        self._ffmpeg_binary = str(ffmpeg_binary or 'ffmpeg').strip() or 'ffmpeg'
        self._download_timeout_seconds = max(0, int(download_timeout_seconds))
        self._alert_enabled = bool(alert_enabled)
        self._alert_threshold = max(1, int(alert_threshold))
        self._alert_window_minutes = max(1, int(alert_window_minutes))
        self._alert_cooldown_minutes = max(0, int(alert_cooldown_minutes))
        self._alert_recipient_ids = set(alert_recipient_ids or set())
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def max_file_size_bytes(self) -> int:
        return self._max_size_bytes

    def enqueue(self, request: DownloadRequest, status_message_id: int) -> None:
        task = asyncio.create_task(self._run_job(request, status_message_id))
        self._tasks.add(task)

        def _cleanup(finished: asyncio.Task[None]) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc:
                logger.exception('Background download task failed: %s', exc)

        task.add_done_callback(_cleanup)

    async def shutdown(self) -> None:
        if not self._tasks:
            return
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_job(self, request: DownloadRequest, status_message_id: int) -> None:
        active_status_message_id = int(status_message_id or 0)

        is_group_chat = int(request.chat_id) < 0

        try:
            async with self._semaphore:
                cache_key = self._build_cache_key(request)
                if cache_key:
                    cached_payload = await self._db.get_download_cache(cache_key)
                    if cached_payload:
                        cached_outcome = await self._try_send_cached(
                            request=request,
                            cached_payload=cached_payload,
                            cache_key=cache_key,
                        )
                        if cached_outcome is not None:
                            await self._db.log_download(
                                user_id=request.user_id,
                                url=request.url,
                                platform=request.platform.value,
                                mode=request.mode.value,
                                status='success',
                                file_size=cached_outcome.sent_bytes,
                                error=None,
                                error_category=None,
                                error_hint=None,
                            )
                            summary = tr(
                                request.lang,
                                'finished_summary',
                                sent_files=cached_outcome.sent_files,
                                skipped_files=cached_outcome.skipped_files,
                                sent_bytes=human_bytes(cached_outcome.sent_bytes),
                            )
                            if request.platform in {
                                Platform.YOUTUBE,
                                Platform.INSTAGRAM,
                                Platform.TIKTOK,
                                Platform.SOUNDCLOUD,
                                Platform.TWITTER,
                            }:
                                if active_status_message_id > 0:
                                    await self._safe_delete(request.chat_id, active_status_message_id)
                            elif active_status_message_id > 0:
                                await self._safe_edit(request.chat_id, active_status_message_id, summary)
                            return
                if (
                    active_status_message_id <= 0
                    and request.chat_id > 0
                    and request.platform in {
                        Platform.INSTAGRAM,
                        Platform.TIKTOK,
                        Platform.SOUNDCLOUD,
                        Platform.TWITTER,
                    }
                ):
                    status_msg = await self._bot.send_message(
                        request.chat_id,
                        self._build_initial_status_text(request),
                    )
                    active_status_message_id = int(status_msg.message_id)
                if status_message_id > 0:
                    await self._safe_edit(
                        request.chat_id,
                        active_status_message_id,
                        self._build_initial_status_text(request),
                    )
                result = await self._downloader.download(
                    request,
                    timeout_seconds=self._download_timeout_seconds,
                )

            outcome = await self._send_download(request, result)
            if cache_key and outcome.cache_artifacts:
                try:
                    await self._db.upsert_download_cache(
                        cache_key=cache_key,
                        platform=request.platform.value,
                        mode=request.mode.value,
                        title=result.title,
                        quality_label=result.quality_label,
                        artifacts=outcome.cache_artifacts,
                    )
                except Exception:
                    logger.warning('Failed to update download cache for key: %s', cache_key, exc_info=True)
            await self._db.log_download(
                user_id=request.user_id,
                url=request.url,
                platform=request.platform.value,
                mode=request.mode.value,
                status='success',
                file_size=outcome.sent_bytes,
                error=None,
                error_category=None,
                error_hint=None,
            )

            summary = (
                tr(
                    request.lang,
                    'finished_summary',
                    sent_files=outcome.sent_files,
                    skipped_files=outcome.skipped_files,
                    sent_bytes=human_bytes(outcome.sent_bytes),
                )
            )
            if (
                request.platform == Platform.YOUTUBE
                or request.platform == Platform.INSTAGRAM
                or request.platform == Platform.TIKTOK
                or request.platform == Platform.SOUNDCLOUD
                or request.platform == Platform.TWITTER
            ):
                if active_status_message_id > 0:
                    await self._safe_delete(request.chat_id, active_status_message_id)
            else:
                if active_status_message_id > 0:
                    await self._safe_edit(request.chat_id, active_status_message_id, summary)
        except DownloadTimeoutError:
            timeout_seconds = max(1, int(self._download_timeout_seconds))
            timeout_minutes = max(1, (timeout_seconds + 59) // 60)
            classification = classify_download_error('download timed out', platform=request.platform)
            message = tr(
                request.lang,
                'download_timeout_reached',
                minutes=timeout_minutes,
            )
            await self._db.log_download(
                user_id=request.user_id,
                url=request.url,
                platform=request.platform.value,
                mode=request.mode.value,
                status='failed',
                file_size=None,
                error=f'Download timed out after {timeout_seconds}s',
                error_category=classification.category,
                error_hint=classification.hint_en,
            )
            if not is_group_chat:
                if active_status_message_id > 0:
                    await self._safe_edit(request.chat_id, active_status_message_id, message)
                else:
                    await self._bot.send_message(request.chat_id, message)
        except DownloaderError as exc:
            classification = classify_download_error(str(exc), platform=request.platform)
            if should_hide_technical_reason(classification.category):
                message = tr(
                    request.lang,
                    'failed_download_simple_hint',
                    hint=classification.hint(request.lang),
                )
            else:
                message = tr(
                    request.lang,
                    'failed_download_reason_with_hint',
                    reason=compact_error_reason(str(exc)),
                    hint=classification.hint(request.lang),
                )
            await self._db.log_download(
                user_id=request.user_id,
                url=request.url,
                platform=request.platform.value,
                mode=request.mode.value,
                status='failed',
                file_size=None,
                error=str(exc),
                error_category=classification.category,
                error_hint=classification.hint_en,
            )
            if classification.category == 'cookie_required':
                await maybe_send_cookie_expiry_alert(
                    bot=self._bot,
                    db=self._db,
                    platform=request.platform,
                    enabled=self._alert_enabled,
                    threshold=self._alert_threshold,
                    window_minutes=self._alert_window_minutes,
                    cooldown_minutes=self._alert_cooldown_minutes,
                    default_recipient_ids=self._alert_recipient_ids,
                )
            if not is_group_chat:
                if active_status_message_id > 0:
                    await self._safe_edit(request.chat_id, active_status_message_id, message)
                else:
                    await self._bot.send_message(request.chat_id, message)
        except Exception as exc:
            logger.exception('Unexpected job error: %s', exc)
            classification = classify_download_error(str(exc), platform=request.platform)
            await self._db.log_download(
                user_id=request.user_id,
                url=request.url,
                platform=request.platform.value,
                mode=request.mode.value,
                status='failed',
                file_size=None,
                error=str(exc) or 'Unexpected error',
                error_category=classification.category,
                error_hint=classification.hint_en,
            )
            if classification.category == 'cookie_required':
                await maybe_send_cookie_expiry_alert(
                    bot=self._bot,
                    db=self._db,
                    platform=request.platform,
                    enabled=self._alert_enabled,
                    threshold=self._alert_threshold,
                    window_minutes=self._alert_window_minutes,
                    cooldown_minutes=self._alert_cooldown_minutes,
                    default_recipient_ids=self._alert_recipient_ids,
                )
            user_error_text = tr(
                request.lang,
                'unexpected_error_processing_with_hint',
                hint=classification.hint(request.lang),
            )
            if not is_group_chat:
                if active_status_message_id > 0:
                    await self._safe_edit(
                        request.chat_id,
                        active_status_message_id,
                        user_error_text,
                    )
                else:
                    await self._bot.send_message(
                        request.chat_id,
                        user_error_text,
                    )
        finally:
            await self._limiter.mark_finished(request.user_id)

    async def has_cached_content(self, request: DownloadRequest) -> bool:
        cache_key = self._build_cache_key(request)
        if not cache_key:
            return False
        payload = await self._db.get_download_cache(cache_key)
        if payload is None:
            return False

        raw_artifacts = payload.get('artifacts')
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            return False
        for item in raw_artifacts:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get('file_id') or '').strip()
            mime = str(item.get('mime') or '').strip().lower()
            if file_id and mime in {'photo', 'audio', 'video', 'document'}:
                return True
        return False

    async def _send_download(self, request: DownloadRequest, result: DownloadResult) -> SendOutcome:
        sent_files = 0
        skipped_files = 0
        sent_bytes = 0
        cached_artifacts: list[dict] = []

        caption = self._build_media_caption(request, result)
        caption_parse_mode = self._caption_parse_mode(request.platform)
        sendable_artifacts = []
        sent_as_instagram_group = False

        for artifact in result.artifacts:
            if artifact.size_bytes > self._max_size_bytes:
                skipped_files += 1
                await self._bot.send_message(
                    request.chat_id,
                    tr(
                        request.lang,
                        'skipped_file_size',
                        name=artifact.path.name,
                        size=human_bytes(artifact.size_bytes),
                        limit=human_bytes(self._max_size_bytes),
                    ),
                )
                continue
            sendable_artifacts.append(artifact)

        has_video_artifact = any(artifact.mime == 'video' for artifact in sendable_artifacts)
        audio_markup = self._build_audio_download_markup(request, has_video=has_video_artifact)

        if (
            request.platform == Platform.INSTAGRAM
            and len(sendable_artifacts) > 1
            and all(a.mime in {'photo', 'video'} for a in sendable_artifacts)
        ):
            try:
                sent_messages = await self._send_instagram_media_group(
                    chat_id=request.chat_id,
                    artifacts=sendable_artifacts,
                    caption=caption,
                    title=result.title,
                )
                sent_files += len(sendable_artifacts)
                sent_bytes += sum(a.size_bytes for a in sendable_artifacts)
                for artifact, sent_message in zip(sendable_artifacts, sent_messages):
                    cache_item = self._build_cache_artifact(artifact, sent_message)
                    if cache_item is not None:
                        cached_artifacts.append(cache_item)
                sent_as_instagram_group = True
                if audio_markup:
                    prompt = '🎵 دانلود صوت' if request.lang == 'fa' else '🎵 Download audio'
                    await self._bot.send_message(
                        request.chat_id,
                        prompt,
                        reply_markup=audio_markup,
                    )
            except TelegramBadRequest:
                logger.warning('Instagram media group failed, fallback to single sends', exc_info=True)

        if not sent_as_instagram_group:
            total_artifacts = len(sendable_artifacts)
            for idx, artifact in enumerate(sendable_artifacts, start=1):
                output_filename = self._build_output_filename(
                    title=result.title,
                    fallback=artifact.path.stem,
                    suffix=artifact.path.suffix,
                    index=idx,
                    total=total_artifacts,
                )
                input_file = FSInputFile(str(artifact.path), filename=output_filename)
                maybe_caption = caption if idx == 1 else None
                maybe_markup = audio_markup if idx == 1 else None

                if artifact.mime == 'photo':
                    await self._bot.send_chat_action(request.chat_id, ChatAction.UPLOAD_PHOTO)
                    sent_message = await self._bot.send_photo(
                        request.chat_id,
                        input_file,
                        caption=maybe_caption,
                        parse_mode=caption_parse_mode if maybe_caption else None,
                        reply_markup=maybe_markup,
                    )
                elif artifact.mime == 'audio':
                    await self._bot.send_chat_action(request.chat_id, ChatAction.UPLOAD_DOCUMENT)
                    sent_message = await self._bot.send_audio(
                        request.chat_id,
                        input_file,
                        caption=maybe_caption,
                        parse_mode=caption_parse_mode if maybe_caption else None,
                        reply_markup=maybe_markup,
                    )
                elif artifact.mime == 'video':
                    await self._bot.send_chat_action(request.chat_id, ChatAction.UPLOAD_VIDEO)
                    cover_input = None
                    cover_url = None
                    if request.platform == Platform.YOUTUBE and idx == 1:
                        cover_path = await asyncio.to_thread(self._extract_video_cover_image, artifact.path)
                        if cover_path is not None:
                            cover_input = FSInputFile(str(cover_path), filename=cover_path.name)
                        else:
                            cover_url = result.thumbnail_url
                    sent_message = await self._send_video_with_optional_cover(
                        chat_id=request.chat_id,
                        video=input_file,
                        caption=maybe_caption,
                        parse_mode=caption_parse_mode if maybe_caption else None,
                        reply_markup=maybe_markup,
                        cover=cover_input,
                        cover_url=cover_url,
                    )
                else:
                    await self._bot.send_chat_action(request.chat_id, ChatAction.UPLOAD_DOCUMENT)
                    sent_message = await self._bot.send_document(
                        request.chat_id,
                        input_file,
                        caption=maybe_caption,
                        parse_mode=caption_parse_mode if maybe_caption else None,
                        reply_markup=maybe_markup,
                    )

                sent_files += 1
                sent_bytes += artifact.size_bytes
                cache_item = self._build_cache_artifact(artifact, sent_message)
                if cache_item is not None:
                    cached_artifacts.append(cache_item)

        if result.artifacts:
            try:
                root_dir = result.artifacts[0].path.parent
                shutil.rmtree(root_dir, ignore_errors=True)
            except Exception:
                logger.warning('Failed to cleanup job directory', exc_info=True)

        return SendOutcome(
            sent_files=sent_files,
            sent_bytes=sent_bytes,
            skipped_files=skipped_files,
            cache_artifacts=cached_artifacts,
        )

    async def _try_send_cached(
        self,
        request: DownloadRequest,
        cached_payload: dict,
        cache_key: str,
    ) -> SendOutcome | None:
        cached_platform = str(cached_payload.get('platform') or '').strip().lower()
        cached_mode = str(cached_payload.get('mode') or '').strip().lower()
        if cached_platform and cached_platform != request.platform.value:
            return None
        if cached_mode and cached_mode != request.mode.value:
            return None
        if not self._youtube_cache_quality_matches_request(request, cached_payload):
            await self._db.remove_download_cache(cache_key)
            logger.info('Cache entry invalidated due quality mismatch for key: %s', cache_key)
            return None

        raw_artifacts = cached_payload.get('artifacts')
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            return None

        cached_artifacts: list[dict] = []
        for item in raw_artifacts:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get('file_id') or '').strip()
            mime = str(item.get('mime') or '').strip().lower()
            if not file_id or mime not in {'photo', 'audio', 'video', 'document'}:
                continue
            size_value = item.get('size_bytes')
            size_bytes = int(size_value) if isinstance(size_value, (int, float)) and size_value > 0 else 0
            cached_artifacts.append(
                {
                    'file_id': file_id,
                    'mime': mime,
                    'size_bytes': size_bytes,
                }
            )
        if not cached_artifacts:
            return None

        pseudo_result = DownloadResult(
            title=str(cached_payload.get('title') or request.title or 'Untitled'),
            platform=request.platform,
            artifacts=[],
            quality_label=(
                str(cached_payload.get('quality_label'))
                if cached_payload.get('quality_label') is not None
                else None
            ),
        )
        caption = self._build_media_caption(request, pseudo_result)
        caption_parse_mode = self._caption_parse_mode(request.platform)
        has_video_artifact = any(item.get('mime') == 'video' for item in cached_artifacts)
        audio_markup = self._build_audio_download_markup(request, has_video=has_video_artifact)

        try:
            if (
                request.platform == Platform.INSTAGRAM
                and len(cached_artifacts) > 1
                and all(item['mime'] in {'photo', 'video'} for item in cached_artifacts)
            ):
                await self._send_cached_media_group(
                    chat_id=request.chat_id,
                    cached_artifacts=cached_artifacts,
                    caption=caption,
                )
                if audio_markup:
                    prompt = '🎵 دانلود صوت' if request.lang == 'fa' else '🎵 Download audio'
                    await self._bot.send_message(
                        request.chat_id,
                        prompt,
                        reply_markup=audio_markup,
                    )
            else:
                for idx, item in enumerate(cached_artifacts, start=1):
                    maybe_caption = caption if idx == 1 else None
                    maybe_markup = audio_markup if idx == 1 else None
                    file_id = item['file_id']
                    mime = item['mime']
                    if mime == 'photo':
                        await self._bot.send_photo(
                            request.chat_id,
                            file_id,
                            caption=maybe_caption,
                            parse_mode=caption_parse_mode if maybe_caption else None,
                            reply_markup=maybe_markup,
                        )
                    elif mime == 'audio':
                        await self._bot.send_audio(
                            request.chat_id,
                            file_id,
                            caption=maybe_caption,
                            parse_mode=caption_parse_mode if maybe_caption else None,
                            reply_markup=maybe_markup,
                        )
                    elif mime == 'video':
                        cover_url = (
                            self._youtube_cover_url_from_request(request.url)
                            if request.platform == Platform.YOUTUBE and idx == 1
                            else None
                        )
                        await self._send_video_with_optional_cover(
                            chat_id=request.chat_id,
                            video=file_id,
                            caption=maybe_caption,
                            parse_mode=caption_parse_mode if maybe_caption else None,
                            reply_markup=maybe_markup,
                            cover=None,
                            cover_url=cover_url,
                        )
                    else:
                        await self._bot.send_document(
                            request.chat_id,
                            file_id,
                            caption=maybe_caption,
                            parse_mode=caption_parse_mode if maybe_caption else None,
                            reply_markup=maybe_markup,
                        )
        except TelegramBadRequest:
            await self._db.remove_download_cache(cache_key)
            logger.info('Cache entry invalidated for key: %s', cache_key)
            return None

        return SendOutcome(
            sent_files=len(cached_artifacts),
            sent_bytes=sum(int(item.get('size_bytes') or 0) for item in cached_artifacts),
            skipped_files=0,
            cache_artifacts=[],
        )

    @staticmethod
    def _youtube_cache_quality_matches_request(request: DownloadRequest, cached_payload: dict) -> bool:
        if request.platform != Platform.YOUTUBE:
            return True
        mode_limit_map: dict[DownloadMode, int] = {
            DownloadMode.VIDEO_1080: 1080,
            DownloadMode.VIDEO_240: 240,
            DownloadMode.VIDEO_360: 360,
            DownloadMode.VIDEO_480: 480,
            DownloadMode.VIDEO_720: 720,
        }
        max_res = mode_limit_map.get(request.mode)
        if max_res is None:
            return True
        quality_label = str(cached_payload.get('quality_label') or '').strip().lower()
        if not quality_label:
            return True
        digits = ''.join(ch for ch in quality_label if ch.isdigit())
        if not digits:
            return True
        try:
            cached_res = int(digits)
        except ValueError:
            return True
        return cached_res <= max_res

    async def _send_cached_media_group(
        self,
        chat_id: int,
        cached_artifacts: list[dict],
        caption: str,
    ) -> None:
        for chunk_index in range(0, len(cached_artifacts), 10):
            chunk = cached_artifacts[chunk_index : chunk_index + 10]
            media_items = []
            for idx, item in enumerate(chunk, start=1):
                item_caption = caption if chunk_index == 0 and idx == 1 else None
                if item['mime'] == 'photo':
                    media_items.append(
                        InputMediaPhoto(
                            media=item['file_id'],
                            caption=item_caption,
                        )
                    )
                else:
                    media_items.append(
                        InputMediaVideo(
                            media=item['file_id'],
                            caption=item_caption,
                            supports_streaming=True,
                        )
                    )
            await self._bot.send_media_group(chat_id=chat_id, media=media_items)

    @staticmethod
    def _build_cache_artifact(artifact, sent_message) -> dict | None:
        file_id: str | None = None
        file_unique_id: str | None = None
        mime = str(getattr(artifact, 'mime', '') or '').strip().lower()
        if mime == 'photo':
            photo_sizes = getattr(sent_message, 'photo', None) or []
            if photo_sizes:
                largest_photo = photo_sizes[-1]
                file_id = str(getattr(largest_photo, 'file_id', '') or '').strip()
                file_unique_id = str(getattr(largest_photo, 'file_unique_id', '') or '').strip()
        elif mime == 'audio':
            audio = getattr(sent_message, 'audio', None)
            if audio is not None:
                file_id = str(getattr(audio, 'file_id', '') or '').strip()
                file_unique_id = str(getattr(audio, 'file_unique_id', '') or '').strip()
        elif mime == 'video':
            video = getattr(sent_message, 'video', None)
            if video is not None:
                file_id = str(getattr(video, 'file_id', '') or '').strip()
                file_unique_id = str(getattr(video, 'file_unique_id', '') or '').strip()
        else:
            document = getattr(sent_message, 'document', None)
            if document is not None:
                file_id = str(getattr(document, 'file_id', '') or '').strip()
                file_unique_id = str(getattr(document, 'file_unique_id', '') or '').strip()
                mime = 'document'
        if not file_id:
            return None

        size_raw = getattr(artifact, 'size_bytes', 0)
        size_bytes = int(size_raw) if isinstance(size_raw, (int, float)) and size_raw > 0 else 0
        payload = {
            'mime': mime,
            'file_id': file_id,
            'size_bytes': size_bytes,
        }
        if file_unique_id:
            payload['file_unique_id'] = file_unique_id
        return payload

    async def _send_instagram_media_group(
        self,
        chat_id: int,
        artifacts: list,
        caption: str,
        title: str,
    ) -> list:
        sent_messages = []
        total_artifacts = len(artifacts)
        for chunk_index in range(0, len(artifacts), 10):
            chunk = artifacts[chunk_index : chunk_index + 10]
            media_items = []
            for idx, artifact in enumerate(chunk, start=1):
                global_index = chunk_index + idx
                output_filename = self._build_output_filename(
                    title=title,
                    fallback=artifact.path.stem,
                    suffix=artifact.path.suffix,
                    index=global_index,
                    total=total_artifacts,
                )
                input_file = FSInputFile(str(artifact.path), filename=output_filename)
                item_caption = caption if chunk_index == 0 and idx == 1 else None
                if artifact.mime == 'photo':
                    media_items.append(InputMediaPhoto(media=input_file, caption=item_caption))
                else:
                    media_items.append(
                        InputMediaVideo(
                            media=input_file,
                            caption=item_caption,
                            supports_streaming=True,
                        )
                    )
            await self._bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            sent_chunk = await self._bot.send_media_group(chat_id=chat_id, media=media_items)
            sent_messages.extend(sent_chunk)
        return sent_messages

    @staticmethod
    def _build_media_caption(request: DownloadRequest, result: DownloadResult) -> str:
        title_text = safe_caption(result.title)

        if request.platform in {Platform.INSTAGRAM, Platform.TIKTOK, Platform.SOUNDCLOUD}:
            return '❤️ @RoboDownloadBot'

        if request.platform == Platform.TWITTER:
            username = DownloadManager._extract_twitter_username(request.url)
            title_text = DownloadManager._clean_twitter_title_text(title_text, username)
            middle_text = f'𝕏 @{username}' if username else '𝕏 Open post'
            post_url = html.escape(safe_caption(request.url, limit=500), quote=True)
            return (
                f'{html.escape(title_text)}\n\n'
                f'<a href="{post_url}">{html.escape(middle_text)}</a>\n\n'
                '🤖 @RoboDownloadBot'
            )

        if request.platform == Platform.YOUTUBE:
            short_url = DownloadManager._short_youtube_url(request.url)
            if request.mode == DownloadMode.AUDIO_MP3:
                return '❤️ @RoboDownloadBot'

            quality = result.quality_label or DownloadManager._quality_label(request.mode)
            return f'🍿 {title_text}\n\n🔗 {short_url}\n\n🎬 {quality}\n\n❤️ @RoboDownloadBot'

        source = safe_caption(request.url, limit=350)
        return f'{title_text}\nSource: {source}'

    @staticmethod
    def _build_output_filename(
        title: str,
        fallback: str,
        suffix: str,
        index: int,
        total: int,
    ) -> str:
        base_name = (title or fallback or 'media').strip()
        if total > 1:
            base_name = f'{base_name} - {index}'
        return safe_filename(base_name, ext=suffix, limit=128)

    @staticmethod
    def _build_audio_download_markup(request: DownloadRequest, has_video: bool = True):
        if request.platform not in {Platform.INSTAGRAM, Platform.TIKTOK}:
            return None
        if not has_video:
            return None
        # Group/supergroup chat IDs are negative in Telegram; no audio button there.
        if request.chat_id < 0:
            return None
        if request.mode == DownloadMode.AUDIO_MP3:
            return None

        label = '🎵 Audio'
        kb = InlineKeyboardBuilder()
        kb.button(
            text=label,
            callback_data=f'dl:{request.request_id}:{DownloadMode.AUDIO_MP3.value}',
        )
        kb.adjust(1)
        return kb.as_markup()

    @staticmethod
    def _build_cache_key(request: DownloadRequest) -> str | None:
        platform_key = request.platform.value
        mode_key = request.mode.value
        source_key = DownloadManager._extract_content_cache_id(request.platform, request.url)
        if source_key is None:
            source_key = DownloadManager._normalized_url_for_cache(request.url)
        if not source_key:
            return None
        return f'{platform_key}:{mode_key}:{source_key}'

    @staticmethod
    def _extract_content_cache_id(platform: Platform, url: str) -> str | None:
        if platform == Platform.YOUTUBE:
            video_id = DownloadManager._extract_youtube_video_id(url)
            if video_id:
                return f'yt:{video_id}'
            return None
        if platform == Platform.TWITTER:
            status_id = DownloadManager._extract_twitter_status_id(url)
            if status_id:
                return f'tw:{status_id}'
            return None
        if platform == Platform.INSTAGRAM:
            media_id = DownloadManager._extract_instagram_media_id(url)
            if media_id:
                return f'ig:{media_id}'
            return None
        if platform == Platform.TIKTOK:
            video_id = DownloadManager._extract_tiktok_video_id(url)
            if video_id:
                return f'tt:{video_id}'
            return None
        if platform == Platform.SOUNDCLOUD:
            return DownloadManager._normalized_url_for_cache(url)
        return None

    @staticmethod
    def _normalized_url_for_cache(url: str) -> str | None:
        raw = str(url or '').strip()
        if not raw:
            return None
        try:
            parsed = urlparse(raw)
        except Exception:
            return raw.lower()
        host = parsed.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        path = parsed.path.rstrip('/')
        query = parsed.query or ''
        if query:
            return f'{host}{path}?{query}'.lower()
        return f'{host}{path}'.lower()

    @staticmethod
    def _short_youtube_url(url: str) -> str:
        video_id = DownloadManager._extract_youtube_video_id(url)
        if video_id:
            return f'https://youtu.be/{video_id}'
        return safe_caption(url, limit=350)

    @staticmethod
    def _extract_twitter_username(url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except Exception:
            return None

        host = parsed.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        if host not in {'x.com', 'twitter.com', 'm.twitter.com', 'mobile.twitter.com'}:
            return None

        parts = [part.strip() for part in parsed.path.split('/') if part.strip()]
        if not parts:
            return None

        username = parts[0].lstrip('@')
        if not username or username in {'i', 'home', 'explore', 'search'}:
            return None
        return username

    @staticmethod
    def _extract_twitter_status_id(url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except Exception:
            return None

        host = parsed.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        if host not in {'x.com', 'twitter.com', 'm.twitter.com', 'mobile.twitter.com'}:
            return None

        parts = [part.strip() for part in parsed.path.split('/') if part.strip()]
        if len(parts) < 3:
            return None
        if parts[1].lower() != 'status':
            return None
        status_id = parts[2]
        return status_id if status_id.isdigit() else None

    @staticmethod
    def _extract_instagram_media_id(url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except Exception:
            return None

        host = parsed.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        if host not in {'instagram.com', 'm.instagram.com'}:
            return None

        parts = [part.strip() for part in parsed.path.split('/') if part.strip()]
        if len(parts) < 2:
            return None
        media_type = parts[0].lower()
        if media_type not in {'p', 'reel', 'tv'}:
            return None
        media_id = parts[1]
        return media_id if media_id else None

    @staticmethod
    def _extract_tiktok_video_id(url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except Exception:
            return None

        host = parsed.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        if host not in {'tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'}:
            return None

        parts = [part.strip() for part in parsed.path.split('/') if part.strip()]
        if not parts:
            return None

        if 'video' in [part.lower() for part in parts]:
            try:
                idx = [part.lower() for part in parts].index('video')
            except ValueError:
                idx = -1
            if idx >= 0 and idx + 1 < len(parts):
                candidate = parts[idx + 1]
                if candidate.isdigit():
                    return candidate

        if host in {'vm.tiktok.com', 'vt.tiktok.com'}:
            # Short redirect links: keep path as stable fallback key.
            return '/'.join(parts).lower()

        return None

    @staticmethod
    def _caption_parse_mode(platform: Platform) -> str | None:
        if platform == Platform.TWITTER:
            return 'HTML'
        return None

    @staticmethod
    def _clean_twitter_title_text(title_text: str, username: str | None) -> str:
        cleaned = str(title_text or '').strip()
        if not cleaned or not username:
            return cleaned

        patterns = (
            f' (@{username})',
            f' - @{username}',
            f' — @{username}',
            f' – @{username}',
            f' | @{username}',
        )
        for pattern in patterns:
            if cleaned.endswith(pattern):
                cleaned = cleaned[: -len(pattern)].rstrip(' -–—|')
                break
        return cleaned or str(title_text or '').strip()

    @staticmethod
    def _extract_youtube_video_id(url: str) -> str | None:
        try:
            parsed = urlparse(url)
        except Exception:
            return None

        host = parsed.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]
        path = parsed.path.strip('/')
        if not path:
            return None

        if host == 'youtu.be':
            return path.split('/')[0]

        if host in {'youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtube-nocookie.com'}:
            if path == 'watch':
                query_video = parse_qs(parsed.query).get('v', [None])[0]
                if query_video:
                    return query_video

            for prefix in ('shorts/', 'embed/', 'live/'):
                if path.startswith(prefix):
                    parts = path.split('/')
                    if len(parts) >= 2 and parts[1]:
                        return parts[1]

        return None

    @staticmethod
    def _quality_label(mode: DownloadMode) -> str:
        if mode == DownloadMode.VIDEO_1080:
            return '1080p'
        if mode == DownloadMode.VIDEO_720:
            return '720p'
        if mode == DownloadMode.VIDEO_480:
            return '480p'
        if mode == DownloadMode.VIDEO_360:
            return '360p'
        if mode == DownloadMode.VIDEO_240:
            return '240p'
        if mode == DownloadMode.BEST:
            return 'Best'
        return 'Audio'

    @staticmethod
    def _youtube_cover_url_from_request(url: str) -> str | None:
        video_id = DownloadManager._extract_youtube_video_id(url)
        if not video_id:
            return None
        return f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'

    @staticmethod
    def _build_initial_status_text(request: DownloadRequest) -> str:
        if request.platform.value == 'youtube':
            title_text = safe_caption(request.title, limit=980)
            loading = 'در حال بارگیری...' if request.lang == 'fa' else 'Loading...'
            if title_text:
                return f'🍿 {title_text}\n\n{loading}'
            return loading
        if request.platform in {Platform.INSTAGRAM, Platform.TIKTOK, Platform.SOUNDCLOUD, Platform.TWITTER}:
            return 'در حال بارگیری...' if request.lang == 'fa' else 'Loading...'
        return tr(request.lang, 'download_started_fetching')

    async def _safe_edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self._bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
            return
        except TelegramBadRequest:
            try:
                await self._bot.edit_message_caption(
                    caption=text,
                    chat_id=chat_id,
                    message_id=message_id,
                )
            except TelegramBadRequest:
                return
        except Exception:
            logger.debug('Failed to edit progress message', exc_info=True)
            return

    async def _safe_delete(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest:
            return
        except Exception:
            logger.debug('Failed to delete status message', exc_info=True)
            return

    async def _send_video_with_optional_cover(
        self,
        chat_id: int,
        video,
        caption: str | None,
        parse_mode: str | None,
        reply_markup,
        cover,
        cover_url: str | None,
    ):
        if cover is not None:
            try:
                return await self._bot.send_video(
                    chat_id,
                    video,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    supports_streaming=True,
                    cover=cover,
                )
            except TelegramBadRequest:
                logger.debug('YouTube local cover rejected by Telegram, retrying with URL/no cover')

        if cover_url:
            try:
                return await self._bot.send_video(
                    chat_id,
                    video,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    supports_streaming=True,
                    cover=cover_url,
                )
            except TelegramBadRequest:
                logger.debug('YouTube cover URL rejected by Telegram, retrying without cover')

        return await self._bot.send_video(
            chat_id,
            video,
            caption=caption,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            supports_streaming=True,
        )

    def _extract_video_cover_image(self, video_path: Path) -> Path | None:
        source = Path(video_path)
        if not source.exists():
            return None
        cover_path = source.with_suffix('.cover.jpg')
        command = [
            self._ffmpeg_binary,
            '-y',
            '-i',
            str(source),
            '-vf',
            'thumbnail',
            '-frames:v',
            '1',
            str(cover_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            logger.debug('ffmpeg thumbnail extraction failed: %s', completed.stderr or completed.stdout)
            return None
        if not cover_path.exists():
            return None
        with contextlib.suppress(OSError):
            if cover_path.stat().st_size <= 0:
                return None
        return cover_path

