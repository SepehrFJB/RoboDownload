from __future__ import annotations

from app.models import DownloadMode, Platform

YOUTUBE_VIDEO_MODES: tuple[DownloadMode, ...] = (
    DownloadMode.VIDEO_1080,
    DownloadMode.VIDEO_720,
    DownloadMode.VIDEO_480,
    DownloadMode.VIDEO_360,
    DownloadMode.VIDEO_240,
)

YOUTUBE_DEFAULT_MODES: tuple[DownloadMode, ...] = (
    *YOUTUBE_VIDEO_MODES,
    DownloadMode.AUDIO_MP3,
)

YOUTUBE_MODE_ORDER: dict[DownloadMode, int] = {
    DownloadMode.VIDEO_1080: 0,
    DownloadMode.VIDEO_720: 1,
    DownloadMode.VIDEO_480: 2,
    DownloadMode.VIDEO_360: 3,
    DownloadMode.VIDEO_240: 4,
    DownloadMode.AUDIO_MP3: 5,
    DownloadMode.BEST: 6,
}

YOUTUBE_TARGET_RESOLUTION_BY_MODE: dict[DownloadMode, int] = {
    DownloadMode.VIDEO_1080: 1080,
    DownloadMode.VIDEO_720: 720,
    DownloadMode.VIDEO_480: 480,
    DownloadMode.VIDEO_360: 360,
    DownloadMode.VIDEO_240: 240,
}


def default_modes_for_platform(platform: Platform) -> list[DownloadMode]:
    if platform == Platform.YOUTUBE:
        return list(YOUTUBE_DEFAULT_MODES)
    if platform in {Platform.TWITTER, Platform.INSTAGRAM, Platform.TIKTOK}:
        return [DownloadMode.BEST, DownloadMode.AUDIO_MP3]
    if platform == Platform.SOUNDCLOUD:
        return [DownloadMode.AUDIO_MP3, DownloadMode.BEST]
    return [DownloadMode.BEST]


def is_video_resolution_mode(mode: DownloadMode) -> bool:
    return mode in YOUTUBE_VIDEO_MODES


def youtube_mode_sort_key(mode: DownloadMode) -> int:
    return YOUTUBE_MODE_ORDER.get(mode, 99)
