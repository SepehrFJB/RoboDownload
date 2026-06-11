from __future__ import annotations

from urllib.parse import urlparse

from app.models import Platform


YOUTUBE_HOSTS = {
    'youtube.com',
    'music.youtube.com',
    'youtu.be',
}

INSTAGRAM_HOSTS = {
    'instagram.com',
}

SOUNDCLOUD_HOSTS = {
    'soundcloud.com',
    'on.soundcloud.com',
}

TIKTOK_HOSTS = {
    'tiktok.com',
    'vm.tiktok.com',
    'vt.tiktok.com',
}

TWITTER_HOSTS = {
    'twitter.com',
    'x.com',
    't.co',
}


def _host_matches(host: str, domains: set[str]) -> bool:
    for domain in domains:
        if host == domain or host.endswith(f'.{domain}'):
            return True
    return False



def extract_first_url(text: str) -> str | None:
    for token in text.split():
        cleaned = token.strip()
        if cleaned.startswith('http://') or cleaned.startswith('https://'):
            return cleaned
    return None



def detect_platform(url: str) -> Platform:
    try:
        host = urlparse(url).netloc.lower().strip()
    except ValueError:
        return Platform.OTHER

    if _host_matches(host, YOUTUBE_HOSTS):
        return Platform.YOUTUBE
    if _host_matches(host, INSTAGRAM_HOSTS):
        return Platform.INSTAGRAM
    if _host_matches(host, SOUNDCLOUD_HOSTS):
        return Platform.SOUNDCLOUD
    if _host_matches(host, TIKTOK_HOSTS):
        return Platform.TIKTOK
    if _host_matches(host, TWITTER_HOSTS):
        return Platform.TWITTER
    return Platform.OTHER



def is_supported_url(url: str) -> bool:
    return detect_platform(url) in {
        Platform.YOUTUBE,
        Platform.INSTAGRAM,
        Platform.SOUNDCLOUD,
        Platform.TIKTOK,
        Platform.TWITTER,
    }
