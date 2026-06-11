from __future__ import annotations

import re


def human_bytes(num: int) -> str:
    step = 1024.0
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    value = float(num)
    for unit in units:
        if value < step:
            return f'{value:.1f}{unit}'
        value /= step
    return f'{value:.1f}PB'



def human_duration(seconds: int | None) -> str:
    if seconds is None:
        return '-'
    mins, sec = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f'{hrs:d}:{mins:02d}:{sec:02d}'
    return f'{mins:d}:{sec:02d}'



def safe_caption(text: str, limit: int = 900) -> str:
    stripped = ' '.join(text.split())
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3].rstrip() + '...'


def safe_filename(text: str, ext: str = '', limit: int = 128) -> str:
    normalized = ' '.join(str(text).split())
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip(' .')
    if not normalized:
        normalized = 'media'

    normalized_ext = ext.strip()
    if normalized_ext and not normalized_ext.startswith('.'):
        normalized_ext = f'.{normalized_ext}'

    max_base_len = max(1, limit - len(normalized_ext))
    if len(normalized) > max_base_len:
        normalized = normalized[:max_base_len].rstrip(' .')
    if not normalized:
        normalized = 'media'

    return f'{normalized}{normalized_ext}'
