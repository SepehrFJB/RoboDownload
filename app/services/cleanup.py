from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class CleanupService:
    def __init__(self, download_dir: Path, keep_seconds: int = 3600) -> None:
        self._download_dir = download_dir
        self._cookie_dir = self._download_dir / '_cookies'
        self._keep_seconds = keep_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._cleanup_once)
            except Exception:
                logger.exception('Cleanup pass failed')
            await asyncio.sleep(600)

    def _cleanup_once(self) -> None:
        if not self._download_dir.exists():
            return
        cutoff = time.time() - self._keep_seconds
        for path in self._download_dir.iterdir():
            # Cookie storage is persistent config state and must never be deleted
            # by temporary file cleanup.
            if path.resolve() == self._cookie_dir.resolve():
                continue
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime >= cutoff:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
