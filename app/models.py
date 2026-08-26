from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Platform(str, Enum):
    YOUTUBE = 'youtube'
    INSTAGRAM = 'instagram'
    SOUNDCLOUD = 'soundcloud'
    TIKTOK = 'tiktok'
    TWITTER = 'twitter'
    OTHER = 'other'


class DownloadMode(str, Enum):
    VIDEO_1080 = 'video_1080'
    VIDEO_720 = 'video_720'
    VIDEO_480 = 'video_480'
    VIDEO_360 = 'video_360'
    VIDEO_240 = 'video_240'
    AUDIO_MP3 = 'audio_mp3'
    BEST = 'best'


@dataclass(slots=True)
class MediaInfo:
    url: str
    title: str
    duration: int | None
    platform: Platform
    uploader: str | None
    thumbnail_url: str | None = None
    caption: str | None = None
    mode_size_bytes: dict[DownloadMode, int | None] = field(default_factory=dict)


@dataclass(slots=True)
class PendingRequest:
    request_id: str
    user_id: int
    url: str
    title: str
    platform: Platform
    available_modes: list[DownloadMode] = field(default_factory=list)


@dataclass(slots=True)
class DownloadRequest:
    request_id: str
    user_id: int
    chat_id: int
    url: str
    mode: DownloadMode
    platform: Platform
    title: str
    lang: str = 'fa'


@dataclass(slots=True)
class DownloadArtifact:
    path: Path
    mime: str
    size_bytes: int


@dataclass(slots=True)
class DownloadResult:
    title: str
    platform: Platform
    artifacts: list[DownloadArtifact]
    quality_label: str | None = None
    thumbnail_url: str | None = None
