from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass

from app.models import PendingRequest


@dataclass(slots=True)
class StoredRequest:
    expires_at: float
    request: PendingRequest


class RequestStore:
    def __init__(self, ttl_seconds: int, cleanup_interval_seconds: int = 60) -> None:
        self._ttl_seconds = ttl_seconds
        self._cleanup_interval_seconds = max(1, int(cleanup_interval_seconds))
        self._items: dict[str, StoredRequest] = {}
        self._lock = asyncio.Lock()
        self._next_cleanup_at = 0.0

    async def put(self, request: PendingRequest) -> None:
        now = time.time()
        async with self._lock:
            if now >= self._next_cleanup_at:
                self._cleanup_expired_locked(now)
                self._next_cleanup_at = now + self._cleanup_interval_seconds
            self._items[request.request_id] = StoredRequest(
                expires_at=now + self._ttl_seconds,
                request=request,
            )

    async def get(self, request_id: str) -> PendingRequest | None:
        async with self._lock:
            stored = self._items.get(request_id)
            if stored is None:
                return None
            if stored.expires_at < time.time():
                self._items.pop(request_id, None)
                return None
            return stored.request

    async def pop(self, request_id: str) -> PendingRequest | None:
        async with self._lock:
            stored = self._items.pop(request_id, None)
            if stored is None:
                return None
            if stored.expires_at < time.time():
                return None
            return stored.request

    async def cleanup(self) -> None:
        now = time.time()
        async with self._lock:
            self._cleanup_expired_locked(now)
            self._next_cleanup_at = now + self._cleanup_interval_seconds

    def _cleanup_expired_locked(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if value.expires_at < now]
        for key in expired:
            self._items.pop(key, None)



def build_request_id() -> str:
    return secrets.token_urlsafe(8)
