from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app.models import (
    DownloadArtifact,
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    MediaInfo,
    Platform,
)
from app.utils.mode_policy import YOUTUBE_TARGET_RESOLUTION_BY_MODE
from app.utils.url_tools import detect_platform

logger = logging.getLogger(__name__)


class DownloaderError(RuntimeError):
    """Raised when media cannot be downloaded."""


class DownloadTimeoutError(DownloaderError):
    """Raised when a download exceeds the configured timeout."""


class _QuietYtdlpLogger:
    def __init__(self, wrapped: logging.Logger) -> None:
        self._wrapped = wrapped

    def debug(self, msg: str) -> None:
        self._wrapped.debug('yt-dlp: %s', msg)

    def warning(self, msg: str) -> None:
        self._wrapped.debug('yt-dlp: %s', msg)

    def error(self, msg: str) -> None:
        self._wrapped.debug('yt-dlp: %s', msg)


class DownloaderService:
    _GALLERY_DL_IMAGE_FILTER = (
        "extension in ('jpg','jpeg','png','webp','bmp','tif','tiff','gif','heic','heif','avif')"
    )

    def __init__(
        self,
        download_dir: Path,
        ffmpeg_binary: str,
        platform_cookies: dict[str, str] | None = None,
        ytdlp_js_runtimes: tuple[str, ...] = (),
        ytdlp_remote_components: tuple[str, ...] = (),
        probe_worker_threads: int = 4,
        download_worker_threads: int = 6,
    ) -> None:
        self._download_dir = download_dir
        self._ffmpeg_binary = ffmpeg_binary
        self._platform_cookie_dir = self._download_dir / '_cookies'
        self._platform_cookie_files: dict[Platform, Path] = {}
        self._ytdlp_js_runtimes = tuple(
            item.strip() for item in ytdlp_js_runtimes if str(item).strip()
        )
        self._ytdlp_remote_components = tuple(
            item.strip() for item in ytdlp_remote_components if str(item).strip()
        )
        self._ytdlp_logger = _QuietYtdlpLogger(logger)
        self._probe_executor = ThreadPoolExecutor(
            max_workers=max(1, int(probe_worker_threads)),
            thread_name_prefix='probe-worker',
        )
        self._download_executor = ThreadPoolExecutor(
            max_workers=max(1, int(download_worker_threads)),
            thread_name_prefix='download-worker',
        )
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._platform_cookie_dir.mkdir(parents=True, exist_ok=True)
        self._hydrate_platform_cookies(platform_cookies or {})

    async def shutdown(self) -> None:
        self._probe_executor.shutdown(wait=False, cancel_futures=True)
        self._download_executor.shutdown(wait=False, cancel_futures=True)

    async def probe(self, url: str, user_cookie: str | None = None) -> MediaInfo:
        platform = detect_platform(url)
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            self._probe_executor,
            self._extract_info,
            url,
            platform,
            user_cookie,
        )
        title = self._extract_title(info, fallback='Untitled')
        duration_raw = info.get('duration')
        duration = int(duration_raw) if isinstance(duration_raw, (int, float)) else None
        uploader = info.get('uploader')
        uploader_name = str(uploader) if uploader else None
        thumbnail = info.get('thumbnail')
        thumbnail_url = str(thumbnail).strip() if thumbnail else None
        description = info.get('description')
        caption = str(description).strip() if description else None
        resolved_platform = detect_platform(str(info.get('webpage_url') or url))
        if resolved_platform == Platform.OTHER:
            resolved_platform = platform
        mode_size_bytes = self._estimate_mode_sizes(
            info=info,
            platform=resolved_platform,
            duration=duration,
        )

        return MediaInfo(
            url=url,
            title=title,
            duration=duration,
            platform=resolved_platform,
            uploader=uploader_name,
            thumbnail_url=thumbnail_url,
            caption=caption,
            mode_size_bytes=mode_size_bytes,
        )

    async def download(
        self,
        request: DownloadRequest,
        user_cookie: str | None = None,
        progress_queue: asyncio.Queue[dict[str, Any]] | None = None,
        timeout_seconds: int = 0,
    ) -> DownloadResult:
        job_dir = self._download_dir / request.request_id
        job_dir.mkdir(parents=True, exist_ok=True)
        deadline = self._deadline_from_timeout(timeout_seconds)

        if request.platform == Platform.INSTAGRAM and request.mode != DownloadMode.AUDIO_MP3:
            return await self._download_instagram_hybrid(
                request=request,
                job_dir=job_dir,
                source=request.url,
                user_cookie=user_cookie,
                progress_queue=progress_queue,
                deadline=deadline,
            )

        return await self._download_with_ytdlp(
            request=request,
            job_dir=job_dir,
            source=request.url,
            user_cookie=user_cookie,
            progress_queue=progress_queue,
            deadline=deadline,
        )

    async def _download_instagram_hybrid(
        self,
        request: DownloadRequest,
        job_dir: Path,
        source: str,
        user_cookie: str | None,
        progress_queue: asyncio.Queue[dict[str, Any]] | None,
        deadline: float | None,
    ) -> DownloadResult:
        loop = asyncio.get_running_loop()

        def progress_hook(payload: dict[str, Any]) -> None:
            self._raise_if_deadline_exceeded(deadline)
            if progress_queue is None:
                return
            status = payload.get('status')
            event: dict[str, Any] = {'status': status}

            if status == 'downloading':
                event.update(
                    {
                        'downloaded': payload.get('downloaded_bytes'),
                        'total': payload.get('total_bytes') or payload.get('total_bytes_estimate'),
                        'speed': payload.get('speed'),
                        'eta': payload.get('eta'),
                        'filename': payload.get('filename'),
                    }
                )
            elif status == 'finished':
                event.update({'filename': payload.get('filename')})

            loop.call_soon_threadsafe(progress_queue.put_nowait, event)

        user_cookie_file = self._create_user_cookie_file(user_cookie, f'dl_ig_{request.request_id}')
        bot_cookie_file = self._cookie_file_for_platform(request.platform)

        # Pass 1: yt-dlp video pass with 3-tier cookie hierarchy (User -> Bot -> No Cookie)
        ytdlp_info: dict[str, Any] | None = None
        ytdlp_error: Exception | None = None
        self._raise_if_deadline_exceeded(deadline)
        opts = self._build_download_opts(request, job_dir, progress_hook)
        try:
            ytdlp_info = await loop.run_in_executor(
                self._download_executor,
                self._download_with_cookie_hierarchy,
                source,
                request.platform,
                opts,
                user_cookie_file,
                bot_cookie_file,
            )
        except DownloadTimeoutError:
            raise
        except Exception as exc:
            if self._is_timeout_error(exc):
                raise DownloadTimeoutError('Download timed out') from exc
            ytdlp_error = exc
            logger.info('Instagram yt-dlp video pass failed: %s', exc)

        # Pass 2: gallery-dl photo pass with 3-tier cookie hierarchy (User -> Bot -> No Cookie)
        gallery_error: Exception | None = None
        self._raise_if_deadline_exceeded(deadline)
        try:
            await loop.run_in_executor(
                self._download_executor,
                self._download_instagram_photos_with_gallery_hierarchy,
                source,
                request.platform,
                job_dir,
                deadline,
                user_cookie_file,
                bot_cookie_file,
            )
        except DownloadTimeoutError:
            raise
        except Exception as exc:
            if self._is_timeout_error(exc):
                raise DownloadTimeoutError('Download timed out') from exc
            gallery_error = exc
            logger.info('Instagram gallery-dl photo pass failed: %s', exc)
        finally:
            self._cleanup_user_cookie_file(user_cookie_file)

        files = self._collect_files(job_dir)
        if not files:
            if ytdlp_error is not None and gallery_error is not None:
                raise DownloaderError(
                    'Instagram download failed (video and photo passes failed). '
                    f'yt-dlp: {ytdlp_error} | gallery-dl: {gallery_error}'
                ) from gallery_error
            if ytdlp_error is not None:
                if isinstance(ytdlp_error, DownloaderError):
                    raise ytdlp_error
                if isinstance(ytdlp_error, DownloadError):
                    raise DownloaderError(str(ytdlp_error)) from ytdlp_error
                raise DownloaderError(str(ytdlp_error)) from ytdlp_error
            if gallery_error is not None:
                if isinstance(gallery_error, DownloaderError):
                    raise gallery_error
                raise DownloaderError(str(gallery_error)) from gallery_error
            raise DownloaderError('Download finished but no file was produced.')

        title = self._extract_title(ytdlp_info or {}, fallback=request.title)
        artifacts = [
            DownloadArtifact(
                path=file_path,
                mime=self._guess_mime(file_path.suffix.lower()),
                size_bytes=file_path.stat().st_size,
            )
            for file_path in files
        ]

        if progress_queue is not None:
            await progress_queue.put({'status': 'complete'})

        quality_label = self._extract_quality_label(ytdlp_info or {})
        return DownloadResult(
            title=title,
            platform=request.platform,
            artifacts=artifacts,
            quality_label=quality_label,
            thumbnail_url=(
                str((ytdlp_info or {}).get('thumbnail') or '').strip() or None
            ),
        )

    async def _download_with_ytdlp(
        self,
        request: DownloadRequest,
        job_dir: Path,
        source: str,
        user_cookie: str | None,
        progress_queue: asyncio.Queue[dict[str, Any]] | None,
        deadline: float | None,
    ) -> DownloadResult:
        loop = asyncio.get_running_loop()

        def progress_hook(payload: dict[str, Any]) -> None:
            self._raise_if_deadline_exceeded(deadline)
            if progress_queue is None:
                return
            status = payload.get('status')
            event: dict[str, Any] = {'status': status}

            if status == 'downloading':
                event.update(
                    {
                        'downloaded': payload.get('downloaded_bytes'),
                        'total': payload.get('total_bytes') or payload.get('total_bytes_estimate'),
                        'speed': payload.get('speed'),
                        'eta': payload.get('eta'),
                        'filename': payload.get('filename'),
                    }
                )
            elif status == 'finished':
                event.update({'filename': payload.get('filename')})

            loop.call_soon_threadsafe(progress_queue.put_nowait, event)

        opts = self._build_download_opts(request, job_dir, progress_hook)
        self._raise_if_deadline_exceeded(deadline)

        user_cookie_file = self._create_user_cookie_file(user_cookie, f'dl_{request.request_id}')
        bot_cookie_file = self._cookie_file_for_platform(request.platform)

        try:
            info = await loop.run_in_executor(
                self._download_executor,
                self._download_with_cookie_hierarchy,
                source,
                request.platform,
                opts,
                user_cookie_file,
                bot_cookie_file,
            )
        except DownloadTimeoutError:
            raise
        except DownloaderError:
            raise
        except DownloadError as exc:
            if self._is_timeout_error(exc):
                raise DownloadTimeoutError('Download timed out') from exc
            logger.exception('yt-dlp download failed: %s', exc)
            raise DownloaderError(str(exc)) from exc
        except Exception as exc:
            logger.exception('unexpected download error: %s', exc)
            raise DownloaderError('Unexpected error while downloading media.') from exc
        finally:
            self._cleanup_user_cookie_file(user_cookie_file)

        files = self._collect_files(job_dir)
        if not files:
            raise DownloaderError('Download finished but no file was produced.')

        title = self._extract_title(info, fallback=request.title)
        artifacts = [
            DownloadArtifact(
                path=file_path,
                mime=self._guess_mime(file_path.suffix.lower()),
                size_bytes=file_path.stat().st_size,
            )
            for file_path in files
        ]

        if progress_queue is not None:
            await progress_queue.put({'status': 'complete'})

        quality_label = self._extract_quality_label(info)
        return DownloadResult(
            title=title,
            platform=request.platform,
            artifacts=artifacts,
            quality_label=quality_label,
            thumbnail_url=str(info.get('thumbnail') or '').strip() or None,
        )

    def _create_user_cookie_file(self, user_cookie: str | None, tag: str) -> Path | None:
        payload = str(user_cookie or '').strip()
        if not payload:
            return None
        users_dir = self._platform_cookie_dir / 'users'
        users_dir.mkdir(parents=True, exist_ok=True)
        cookie_path = users_dir / f'{tag}.txt'
        cookie_path.write_text(payload, encoding='utf-8')
        return cookie_path

    @staticmethod
    def _cleanup_user_cookie_file(path: Path | None) -> None:
        if path is not None and path.exists():
            with contextlib.suppress(OSError):
                path.unlink()

    def _extract_info(
        self,
        url: str,
        platform: Platform,
        user_cookie: str | None = None,
    ) -> dict[str, Any]:
        opts: dict[str, Any] = {
            'quiet': True,
            'skip_download': True,
            'noplaylist': platform not in {Platform.INSTAGRAM, Platform.SOUNDCLOUD},
            'extract_flat': False,
            'no_warnings': True,
            'socket_timeout': 30,
            'logger': self._ytdlp_logger,
        }
        self._apply_youtube_ejs_options(opts, platform)

        user_cookie_file = self._create_user_cookie_file(user_cookie, f'probe_{secrets.token_hex(4)}')
        bot_cookie_file = self._cookie_file_for_platform(platform)

        try:
            info = self._extract_info_with_cookie_hierarchy(
                url=url,
                platform=platform,
                options=opts,
                user_cookie_file=user_cookie_file,
                bot_cookie_file=bot_cookie_file,
            )
        except (DownloadError, DownloaderError) as exc:
            if platform == Platform.INSTAGRAM and self._is_instagram_no_video_error(exc):
                if self._probe_instagram_with_gallery_hierarchy(
                    url=url,
                    platform=platform,
                    user_cookie_file=user_cookie_file,
                    bot_cookie_file=bot_cookie_file,
                ):
                    return self._build_instagram_probe_fallback(url)
            raise DownloaderError(str(exc)) from exc
        finally:
            self._cleanup_user_cookie_file(user_cookie_file)

        if not isinstance(info, dict):
            raise DownloaderError('Could not extract metadata from URL.')
        return info

    def _extract_info_with_cookie_hierarchy(
        self,
        url: str,
        platform: Platform,
        options: dict[str, Any],
        user_cookie_file: Path | None,
        bot_cookie_file: Path | None,
    ) -> dict[str, Any]:
        candidates: list[tuple[str, Path | None]] = []
        if user_cookie_file is not None and user_cookie_file.exists():
            candidates.append(('user cookie', user_cookie_file))
        if bot_cookie_file is not None and bot_cookie_file.exists():
            candidates.append(('bot cookie', bot_cookie_file))
        candidates.append(('no cookie', None))

        last_exc: Exception | None = None
        base_opts = dict(options)

        for tag, cookie_path in candidates:
            run_opts = dict(base_opts)
            if cookie_path is not None:
                run_opts['cookiefile'] = str(cookie_path)
            else:
                run_opts.pop('cookiefile', None)

            try:
                return self._extract_info_once(url, platform, run_opts)
            except (DownloadError, DownloaderError) as exc:
                last_exc = exc
                log = logger.debug if platform == Platform.INSTAGRAM else logger.info
                log('Extract metadata failed with %s for %s (falling back): %s', tag, platform.value, exc)

        if last_exc is not None:
            raise last_exc
        raise DownloaderError('Could not extract metadata from URL.')

    def _probe_instagram_with_gallery_hierarchy(
        self,
        url: str,
        platform: Platform,
        user_cookie_file: Path | None,
        bot_cookie_file: Path | None,
    ) -> bool:
        candidates: list[Path | None] = []
        if user_cookie_file is not None and user_cookie_file.exists():
            candidates.append(user_cookie_file)
        if bot_cookie_file is not None and bot_cookie_file.exists():
            candidates.append(bot_cookie_file)
        candidates.append(None)

        for cookie_path in candidates:
            if self._probe_instagram_with_gallery(url=url, cookiefile=cookie_path):
                return True
        return False

    @staticmethod
    def _probe_instagram_with_gallery(url: str, cookiefile: Path | None) -> bool:
        command = [
            sys.executable,
            '-m',
            'gallery_dl',
            '--config-ignore',
            '--no-input',
            '--quiet',
            '--simulate',
            url,
        ]
        if cookiefile is not None:
            command[-1:-1] = ['--cookies', str(cookiefile)]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return False
        return completed.returncode == 0

    @staticmethod
    def _build_instagram_probe_fallback(url: str) -> dict[str, Any]:
        return {
            'title': 'Instagram Post',
            'webpage_url': str(url or ''),
            'duration': None,
            'uploader': None,
            'thumbnail': None,
            'description': None,
            'formats': [],
            'entries': [],
        }

    @staticmethod
    def _is_instagram_no_video_error(exc: Exception) -> bool:
        message = str(exc or '').lower()
        return 'instagram' in message and 'no video' in message

    def _download_instagram_photos_with_gallery_hierarchy(
        self,
        url: str,
        platform: Platform,
        job_dir: Path,
        deadline: float | None,
        user_cookie_file: Path | None,
        bot_cookie_file: Path | None,
    ) -> None:
        candidates: list[tuple[str, Path | None]] = []
        if user_cookie_file is not None and user_cookie_file.exists():
            candidates.append(('user cookie', user_cookie_file))
        if bot_cookie_file is not None and bot_cookie_file.exists():
            candidates.append(('bot cookie', bot_cookie_file))
        candidates.append(('no cookie', None))

        last_error: Exception | None = None
        for tag, cookie_path in candidates:
            try:
                self._download_instagram_photos_with_gallery(
                    url=url,
                    job_dir=job_dir,
                    cookiefile=cookie_path,
                    deadline=deadline,
                )
                return
            except DownloadTimeoutError:
                raise
            except DownloaderError as exc:
                last_error = exc
                logger.info('gallery-dl photo pass failed with %s for %s (falling back): %s', tag, platform.value, exc)

        if last_error is not None:
            raise last_error

    def _download_instagram_photos_with_gallery(
        self,
        url: str,
        job_dir: Path,
        cookiefile: Path | None,
        deadline: float | None,
    ) -> None:
        self._raise_if_deadline_exceeded(deadline)
        module_command = [
            sys.executable,
            '-m',
            'gallery_dl',
            '--config-ignore',
            '--no-input',
            '--quiet',
            '--destination',
            str(job_dir),
            '--filter',
            self._GALLERY_DL_IMAGE_FILTER,
            url,
        ]
        if cookiefile is not None:
            module_command[-1:-1] = ['--cookies', str(cookiefile)]

        completed = self._run_subprocess(
            module_command,
            timeout_seconds=self._remaining_timeout(deadline),
        )

        # Fallback: if module is unavailable for current interpreter, try binary.
        if completed is not None and completed.returncode != 0:
            stderr_text = str(completed.stderr or '').strip()
            stdout_text = str(completed.stdout or '').strip()
            module_missing = (
                'No module named gallery_dl' in stderr_text
                or 'No module named gallery_dl' in stdout_text
            )
            if module_missing:
                binary_command = ['gallery-dl'] + module_command[3:]
                completed = self._run_subprocess(
                    binary_command,
                    timeout_seconds=self._remaining_timeout(deadline),
                )

        if completed is not None and completed.returncode == 0:
            return

        if completed is None:
            raise DownloaderError('gallery-dl executable is not available in current runtime.')

        stderr_text = str(completed.stderr or '').strip()
        stdout_text = str(completed.stdout or '').strip()
        error_text = stderr_text or stdout_text or f'gallery-dl exited with code {completed.returncode}'
        raise DownloaderError(error_text)

    @staticmethod
    def _run_subprocess(command: list[str], timeout_seconds: float | None = None):
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired as exc:
            raise DownloadTimeoutError('Download timed out') from exc

    def _extract_info_once(
        self,
        url: str,
        platform: Platform,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        if platform == Platform.TWITTER:
            return self._extract_twitter_info_with_fallback(url, options)
        return self._extract_info_with_options(url, options)

    def _download_with_cookie_hierarchy(
        self,
        url: str,
        platform: Platform,
        options: dict[str, Any],
        user_cookie_file: Path | None,
        bot_cookie_file: Path | None,
    ) -> dict[str, Any]:
        candidates: list[tuple[str, Path | None]] = []
        if user_cookie_file is not None and user_cookie_file.exists():
            candidates.append(('user cookie', user_cookie_file))
        if bot_cookie_file is not None and bot_cookie_file.exists():
            candidates.append(('bot cookie', bot_cookie_file))
        candidates.append(('no cookie', None))

        last_error: Exception | None = None
        base_opts = dict(options)

        for tag, cookie_path in candidates:
            run_opts = dict(base_opts)
            if cookie_path is not None:
                run_opts['cookiefile'] = str(cookie_path)
            else:
                run_opts.pop('cookiefile', None)

            try:
                return self._download_once(url, platform, run_opts)
            except DownloadTimeoutError:
                raise
            except (DownloadError, DownloaderError) as exc:
                if self._is_timeout_error(exc):
                    raise DownloadTimeoutError('Download timed out') from exc
                last_error = exc
                log = logger.debug if platform == Platform.INSTAGRAM else logger.info
                log('Download pass failed with %s for %s (falling back): %s', tag, platform.value, exc)

        if last_error is not None:
            raise last_error
        raise DownloaderError('Download failed.')

    def _download_once(
        self,
        url: str,
        platform: Platform,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        if platform == Platform.TWITTER:
            return self._download_twitter_with_fallback(url, options)
        return self._download_with_options(url, options)

    @staticmethod
    def _download_with_options(url: str, options: dict[str, Any]) -> dict[str, Any]:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
        if not isinstance(info, dict):
            return {'title': 'Untitled'}
        return info

    @staticmethod
    def _extract_info_with_options(url: str, options: dict[str, Any]) -> dict[str, Any]:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict):
            return {'title': 'Untitled'}
        return info

    def _extract_twitter_info_with_fallback(self, url: str, base_opts: dict[str, Any]) -> dict[str, Any]:
        attempts = ('syndication', 'graphql', 'legacy')
        errors: list[str] = []

        for api in attempts:
            options = self._with_twitter_api(base_opts, api)
            try:
                return self._extract_info_with_options(url, options)
            except DownloadError as exc:
                errors.append(f'{api}: {exc}')
                continue

        raise DownloaderError(self._format_twitter_api_error(errors))

    def _download_twitter_with_fallback(self, url: str, base_opts: dict[str, Any]) -> dict[str, Any]:
        attempts = ('syndication', 'graphql', 'legacy')
        errors: list[str] = []

        for api in attempts:
            options = self._with_twitter_api(base_opts, api)
            try:
                return self._download_with_options(url, options)
            except DownloadError as exc:
                if self._is_timeout_error(exc):
                    raise DownloadTimeoutError('Download timed out') from exc
                errors.append(f'{api}: {exc}')
                continue

        raise DownloaderError(self._format_twitter_api_error(errors))

    def _build_download_opts(
        self,
        request: DownloadRequest,
        job_dir: Path,
        progress_hook,
    ) -> dict[str, Any]:
        opts: dict[str, Any] = {
            'outtmpl': str(job_dir / '%(title).80s-%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'restrictfilenames': True,
            'noplaylist': request.platform not in {Platform.INSTAGRAM, Platform.SOUNDCLOUD},
            'merge_output_format': 'mp4',
            'concurrent_fragment_downloads': 4,
            'retries': 3,
            'fragment_retries': 3,
            'socket_timeout': 30,
            'progress_hooks': [progress_hook],
            'ffmpeg_location': self._ffmpeg_binary,
            'logger': self._ytdlp_logger,
        }
        cookiefile = self._cookie_file_for_platform(request.platform)
        if cookiefile is not None:
            opts['cookiefile'] = str(cookiefile)
        self._apply_youtube_ejs_options(opts, request.platform)

        if request.platform == Platform.SOUNDCLOUD:
            if request.mode == DownloadMode.AUDIO_MP3:
                self._configure_mp3(opts)
            else:
                opts['format'] = 'bestaudio/best'
            return opts

        if request.platform == Platform.INSTAGRAM:
            if request.mode == DownloadMode.AUDIO_MP3:
                self._configure_mp3(opts)
            else:
                self._configure_video_target_resolution(opts, 720)
            return opts

        if request.platform == Platform.TIKTOK and request.mode != DownloadMode.AUDIO_MP3:
            # Keep TikTok quality aligned with Instagram (720p target, portrait-safe).
            self._configure_video_target_resolution(opts, 720)
            return opts

        if request.mode == DownloadMode.VIDEO_1080:
            self._configure_video_target_resolution(opts, 1080)
        elif request.mode == DownloadMode.VIDEO_720:
            self._configure_video_target_resolution(opts, 720)
        elif request.mode == DownloadMode.VIDEO_480:
            self._configure_video_target_resolution(opts, 480)
        elif request.mode == DownloadMode.VIDEO_360:
            self._configure_video_target_resolution(opts, 360)
        elif request.mode == DownloadMode.VIDEO_240:
            self._configure_video_target_resolution(opts, 240)
        elif request.mode == DownloadMode.AUDIO_MP3:
            self._configure_mp3(opts)
        elif request.platform == Platform.TWITTER:
            opts['format'] = 'bestvideo*+bestaudio/best'
        else:
            opts['format'] = 'bestvideo+bestaudio/best'

        return opts

    @staticmethod
    def _deadline_from_timeout(timeout_seconds: int | float | None) -> float | None:
        timeout_value = max(0.0, float(timeout_seconds or 0))
        if timeout_value <= 0:
            return None
        return time.monotonic() + timeout_value

    @staticmethod
    def _remaining_timeout(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadTimeoutError('Download timed out')
        return remaining

    @classmethod
    def _raise_if_deadline_exceeded(cls, deadline: float | None) -> None:
        cls._remaining_timeout(deadline)

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, DownloadTimeoutError):
            return True
        return 'download timed out' in str(exc or '').lower()

    def _apply_youtube_ejs_options(self, opts: dict[str, Any], platform: Platform) -> None:
        if platform != Platform.YOUTUBE:
            return
        if self._ytdlp_js_runtimes:
            opts['js_runtimes'] = {
                runtime_name.strip().lower(): {}
                for runtime_name in self._ytdlp_js_runtimes
                if runtime_name.strip()
            }
        if self._ytdlp_remote_components:
            opts['remote_components'] = list(self._ytdlp_remote_components)

    def set_platform_cookie(self, platform: Platform, cookie_text: str) -> None:
        payload = str(cookie_text or '').strip()
        if not payload:
            raise ValueError('Cookie text cannot be empty')
        path = self._platform_cookie_dir / f'{platform.value}.txt'
        path.write_text(payload, encoding='utf-8')
        self._platform_cookie_files[platform] = path

    def remove_platform_cookie(self, platform: Platform) -> None:
        cookie_path = self._platform_cookie_files.pop(platform, None)
        if cookie_path is None:
            return
        with contextlib.suppress(OSError):
            cookie_path.unlink()

    def _hydrate_platform_cookies(self, platform_cookies: dict[str, str]) -> None:
        for raw_platform, cookie_text in platform_cookies.items():
            platform = self._parse_platform(raw_platform)
            if platform is None:
                continue
            payload = str(cookie_text or '').strip()
            if not payload:
                continue
            try:
                self.set_platform_cookie(platform, payload)
            except OSError:
                logger.exception('Failed to persist cookie file for %s', platform.value)

    def _cookie_file_for_platform(self, platform: Platform) -> Path | None:
        platform_cookie = self._platform_cookie_files.get(platform)
        if platform_cookie is not None and platform_cookie.exists():
            return platform_cookie
        return None

    @staticmethod
    def _parse_platform(raw_platform: str | None) -> Platform | None:
        value = str(raw_platform or '').strip().lower()
        if not value:
            return None
        try:
            platform = Platform(value)
        except ValueError:
            return None
        if platform == Platform.OTHER:
            return None
        return platform

    @staticmethod
    def _configure_video_target_resolution(opts: dict[str, Any], target_res: int) -> None:
        opts['format'] = 'bestvideo+bestaudio/best'
        opts['format_sort'] = [f'res:{target_res}']
        opts['format_sort_force'] = True

    @staticmethod
    def _configure_mp3(opts: dict[str, Any]) -> None:
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }
        ]
        opts['postprocessor_args'] = ['-ar', '44100']

    @staticmethod
    def _extract_quality_label(info: dict[str, Any]) -> str | None:
        width = info.get('width')
        height = info.get('height')
        if isinstance(width, (int, float)) and isinstance(height, (int, float)):
            shortest_edge = int(min(width, height))
            if shortest_edge > 0:
                return f'{shortest_edge}p'

        format_note = info.get('format_note')
        if format_note:
            match = re.search(r'(\d{3,4})p', str(format_note))
            if match:
                return f'{match.group(1)}p'

        requested_formats = info.get('requested_formats')
        if isinstance(requested_formats, list):
            for fmt in requested_formats:
                if not isinstance(fmt, dict):
                    continue
                vcodec = str(fmt.get('vcodec') or '')
                if not vcodec or vcodec == 'none':
                    continue
                w = fmt.get('width')
                h = fmt.get('height')
                if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                    shortest_edge = int(min(w, h))
                    if shortest_edge > 0:
                        return f'{shortest_edge}p'

        return None

    @staticmethod
    def _extract_title(info: dict[str, Any], fallback: str) -> str:
        title = info.get('title')
        if title:
            return str(title)

        entries = info.get('entries')
        if entries is not None:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_title = entry.get('title')
                if entry_title:
                    return str(entry_title)

        return fallback

    @staticmethod
    def _collect_files(job_dir: Path) -> list[Path]:
        files: list[Path] = []
        for file_path in sorted(job_dir.rglob('*')):
            if file_path.is_dir():
                continue
            if file_path.suffix in {'.part', '.ytdl', '.temp', '.lrc', '.skip'}:
                continue
            if not DownloaderService._is_media_file(file_path):
                continue
            files.append(file_path)
        return files

    @staticmethod
    def _is_media_file(file_path: Path) -> bool:
        suffix = file_path.suffix.lower()
        return suffix in {
            '.mp3',
            '.m4a',
            '.aac',
            '.wav',
            '.ogg',
            '.flac',
            '.opus',
            '.mp4',
            '.mkv',
            '.webm',
            '.mov',
            '.avi',
            '.m4v',
            '.jpg',
            '.jpeg',
            '.png',
            '.webp',
            '.bmp',
            '.tiff',
            '.tif',
            '.gif',
        }

    @staticmethod
    def _guess_mime(suffix: str) -> str:
        if suffix in {'.mp3', '.m4a', '.aac', '.wav', '.ogg', '.flac', '.opus'}:
            return 'audio'
        if suffix in {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif'}:
            return 'photo'
        if suffix in {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.gif'}:
            return 'video'
        return 'document'

    @staticmethod
    def _with_twitter_api(base_opts: dict[str, Any], api: str) -> dict[str, Any]:
        options = dict(base_opts)
        extractor_args = dict(options.get('extractor_args') or {})
        twitter_args = dict(extractor_args.get('twitter') or {})
        twitter_args['api'] = [api]
        extractor_args['twitter'] = twitter_args
        options['extractor_args'] = extractor_args
        return options

    def _estimate_mode_sizes(
        self,
        info: dict[str, Any],
        platform: Platform,
        duration: int | None,
    ) -> dict[DownloadMode, int | None]:
        entries = self._iter_info_entries(info)
        result: dict[DownloadMode, int | None] = {}

        def aggregate(mode: DownloadMode, estimator) -> int | None:
            max_size: int | None = None
            for entry in entries:
                value = estimator(entry)
                if value is None:
                    continue
                if max_size is None or value > max_size:
                    max_size = int(value)
            return max_size

        if platform == Platform.YOUTUBE:
            for mode, target_resolution in YOUTUBE_TARGET_RESOLUTION_BY_MODE.items():
                result[mode] = aggregate(
                    mode,
                    lambda entry, target_res=target_resolution: self._estimate_video_mode_size_from_entry(
                        entry,
                        target_res=target_res,
                        duration=duration,
                        strict_target=True,
                    ),
                )
            result[DownloadMode.AUDIO_MP3] = aggregate(
                DownloadMode.AUDIO_MP3,
                lambda entry: self._estimate_mp3_size_from_entry(entry, duration=duration),
            )
            return result

        if platform == Platform.INSTAGRAM:
            result[DownloadMode.BEST] = aggregate(
                DownloadMode.BEST,
                lambda entry: self._estimate_video_mode_size_from_entry(entry, target_res=720, duration=duration),
            )
            result[DownloadMode.AUDIO_MP3] = aggregate(
                DownloadMode.AUDIO_MP3,
                lambda entry: self._estimate_mp3_size_from_entry(entry, duration=duration),
            )
            return result

        if platform == Platform.TIKTOK:
            result[DownloadMode.BEST] = aggregate(
                DownloadMode.BEST,
                lambda entry: self._estimate_video_mode_size_from_entry(entry, target_res=720, duration=duration),
            )
            result[DownloadMode.AUDIO_MP3] = aggregate(
                DownloadMode.AUDIO_MP3,
                lambda entry: self._estimate_mp3_size_from_entry(entry, duration=duration),
            )
            return result

        if platform == Platform.TWITTER:
            result[DownloadMode.BEST] = aggregate(
                DownloadMode.BEST,
                lambda entry: self._estimate_video_mode_size_from_entry(entry, target_res=None, duration=duration),
            )
            result[DownloadMode.AUDIO_MP3] = aggregate(
                DownloadMode.AUDIO_MP3,
                lambda entry: self._estimate_mp3_size_from_entry(entry, duration=duration),
            )
            return result

        if platform == Platform.SOUNDCLOUD:
            result[DownloadMode.AUDIO_MP3] = aggregate(
                DownloadMode.AUDIO_MP3,
                lambda entry: self._estimate_mp3_size_from_entry(entry, duration=duration),
            )
            result[DownloadMode.BEST] = aggregate(
                DownloadMode.BEST,
                lambda entry: self._estimate_audio_mode_size_from_entry(entry, duration=duration),
            )
            return result

        result[DownloadMode.BEST] = aggregate(
            DownloadMode.BEST,
            lambda entry: self._estimate_video_mode_size_from_entry(entry, target_res=None, duration=duration),
        )
        return result

    @staticmethod
    def _iter_info_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
        entries = info.get('entries')
        if isinstance(entries, list):
            normalized = [entry for entry in entries if isinstance(entry, dict)]
            if normalized:
                return normalized
        return [info]

    def _estimate_video_mode_size_from_entry(
        self,
        entry: dict[str, Any],
        target_res: int | None,
        duration: int | None,
        strict_target: bool = False,
    ) -> int | None:
        entry_duration_raw = entry.get('duration')
        effective_duration = (
            int(entry_duration_raw)
            if isinstance(entry_duration_raw, (int, float)) and entry_duration_raw > 0
            else duration
        )
        formats = entry.get('formats')
        if not isinstance(formats, list):
            formats = [entry]
        format_dicts = [fmt for fmt in formats if isinstance(fmt, dict)]
        if not format_dicts:
            return None

        video_fmt = self._pick_video_format(
            format_dicts,
            target_res=target_res,
            strict_target=strict_target,
        )
        if video_fmt is None:
            return None

        video_size = self._estimate_format_size_bytes(video_fmt, duration=effective_duration)
        if self._format_has_audio(video_fmt):
            return video_size

        audio_fmt = self._pick_audio_format(format_dicts)
        if audio_fmt is None:
            return video_size

        audio_size = self._estimate_format_size_bytes(audio_fmt, duration=effective_duration)
        if video_size is None or audio_size is None:
            return None
        return int(video_size + audio_size)

    def _estimate_audio_mode_size_from_entry(
        self,
        entry: dict[str, Any],
        duration: int | None,
    ) -> int | None:
        entry_duration_raw = entry.get('duration')
        effective_duration = (
            int(entry_duration_raw)
            if isinstance(entry_duration_raw, (int, float)) and entry_duration_raw > 0
            else duration
        )
        formats = entry.get('formats')
        if not isinstance(formats, list):
            formats = [entry]
        format_dicts = [fmt for fmt in formats if isinstance(fmt, dict)]
        if not format_dicts:
            return None

        audio_fmt = self._pick_audio_format(format_dicts)
        if audio_fmt is None:
            return None
        return self._estimate_format_size_bytes(audio_fmt, duration=effective_duration)

    def _estimate_mp3_size_from_entry(
        self,
        entry: dict[str, Any],
        duration: int | None,
    ) -> int | None:
        entry_duration_raw = entry.get('duration')
        effective_duration = (
            int(entry_duration_raw)
            if isinstance(entry_duration_raw, (int, float)) and entry_duration_raw > 0
            else duration
        )
        src_audio_size = self._estimate_audio_mode_size_from_entry(entry, duration=effective_duration)
        if effective_duration is None or effective_duration <= 0:
            return src_audio_size

        target_size = int((effective_duration * 192_000 / 8) * 1.05)
        if src_audio_size is None:
            return target_size
        return max(int(src_audio_size), target_size)

    def _pick_video_format(
        self,
        formats: list[dict[str, Any]],
        target_res: int | None,
        strict_target: bool = False,
    ) -> dict[str, Any] | None:
        videos = [fmt for fmt in formats if self._format_has_video(fmt)]
        if not videos:
            return None

        def height_value(fmt: dict[str, Any]) -> int | None:
            w = fmt.get('width')
            h = fmt.get('height')
            if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                return int(min(w, h))
            if isinstance(h, (int, float)):
                return int(h)
            return None

        chosen_pool = videos
        if target_res is not None:
            if strict_target:
                exact_target = [
                    fmt for fmt in videos
                    if (h := height_value(fmt)) is not None and h == target_res
                ]
                if not exact_target:
                    return None
                chosen_pool = exact_target
                target_res = None
            else:
                lower_or_equal = [
                    fmt for fmt in videos
                    if (h := height_value(fmt)) is not None and h <= target_res
                ]
                if lower_or_equal:
                    chosen_pool = lower_or_equal
                else:
                    with_height = [fmt for fmt in videos if height_value(fmt) is not None]
                    if with_height:
                        min_h = min(height_value(fmt) or 0 for fmt in with_height)
                        chosen_pool = [fmt for fmt in with_height if (height_value(fmt) or 0) == min_h]

        def video_score(fmt: dict[str, Any]) -> tuple[int, float, int]:
            h = height_value(fmt) or 0
            tbr_raw = fmt.get('tbr')
            tbr = float(tbr_raw) if isinstance(tbr_raw, (int, float)) else 0.0
            size = self._estimate_format_size_bytes(fmt, duration=None) or 0
            return (h, tbr, size)

        return max(chosen_pool, key=video_score)

    def _pick_audio_format(self, formats: list[dict[str, Any]]) -> dict[str, Any] | None:
        audio_only = [
            fmt for fmt in formats
            if self._format_has_audio(fmt) and not self._format_has_video(fmt)
        ]
        if not audio_only:
            audio_only = [fmt for fmt in formats if self._format_has_audio(fmt)]
        if not audio_only:
            return None

        def audio_score(fmt: dict[str, Any]) -> tuple[float, float, int]:
            abr_raw = fmt.get('abr')
            abr = float(abr_raw) if isinstance(abr_raw, (int, float)) else 0.0
            tbr_raw = fmt.get('tbr')
            tbr = float(tbr_raw) if isinstance(tbr_raw, (int, float)) else 0.0
            size = self._estimate_format_size_bytes(fmt, duration=None) or 0
            return (abr, tbr, size)

        return max(audio_only, key=audio_score)

    @staticmethod
    def _format_has_video(fmt: dict[str, Any]) -> bool:
        vcodec = str(fmt.get('vcodec') or '')
        return bool(vcodec and vcodec != 'none')

    @staticmethod
    def _format_has_audio(fmt: dict[str, Any]) -> bool:
        acodec = str(fmt.get('acodec') or '')
        return bool(acodec and acodec != 'none')

    @staticmethod
    def _estimate_format_size_bytes(fmt: dict[str, Any], duration: int | None) -> int | None:
        size = fmt.get('filesize')
        if isinstance(size, (int, float)) and size > 0:
            return int(size)

        approx = fmt.get('filesize_approx')
        if isinstance(approx, (int, float)) and approx > 0:
            return int(approx)

        if duration is None or duration <= 0:
            return None
        tbr = fmt.get('tbr')
        if isinstance(tbr, (int, float)) and tbr > 0:
            return int((float(tbr) * 1000 / 8) * duration * 1.05)
        return None

    @staticmethod
    def _format_twitter_api_error(errors: list[str]) -> str:
        if not errors:
            return 'Twitter/X API error.'

        short_errors = ' | '.join(errors[-3:])
        return (
            'Twitter/X API failed (tried syndication/graphql/legacy). '
            f'Details: {short_errors}'
        )
