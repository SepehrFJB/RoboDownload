from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(slots=True)
class UserThrottle:
    next_allowed_at: float
    active_jobs: int
    active_probes: int = 0


class RateLimiter:
    def __init__(self, cooldown_seconds: int) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._users: dict[int, UserThrottle] = {}
        self._lock = asyncio.Lock()

    async def try_start(self, user_id: int) -> tuple[bool, str | None, int | None]:
        now = time.time()
        async with self._lock:
            state = self._users.get(user_id)
            if state is None:
                self._users[user_id] = UserThrottle(next_allowed_at=now, active_jobs=1)
                return True, None, None

            if state.active_jobs >= 1 or state.active_probes >= 1:
                return False, 'active_job', None
            if state.next_allowed_at > now:
                wait_seconds = max(1, int(state.next_allowed_at - now))
                return False, 'cooldown', wait_seconds
            state.active_jobs += 1
            state.next_allowed_at = now
            return True, None, None

    async def try_start_probe(self, user_id: int) -> tuple[bool, str | None, int | None]:
        now = time.time()
        async with self._lock:
            state = self._users.get(user_id)
            if state is None:
                self._users[user_id] = UserThrottle(next_allowed_at=now, active_jobs=0, active_probes=1)
                return True, None, None

            if state.active_jobs >= 1 or state.active_probes >= 1:
                return False, 'active_job', None
            if state.next_allowed_at > now:
                wait_seconds = max(1, int(state.next_allowed_at - now))
                return False, 'cooldown', wait_seconds
            state.active_probes += 1
            return True, None, None

    async def mark_probe_finished(self, user_id: int) -> None:
        async with self._lock:
            state = self._users.get(user_id)
            if state is None:
                return
            state.active_probes = max(0, state.active_probes - 1)

    async def mark_finished(self, user_id: int, apply_cooldown: bool = True) -> None:
        now = time.time()
        async with self._lock:
            state = self._users.get(user_id)
            if state is None:
                self._users[user_id] = UserThrottle(
                    next_allowed_at=now + self._cooldown_seconds if apply_cooldown else now,
                    active_jobs=0,
                    active_probes=0,
                )
                return

            state.active_jobs = max(0, state.active_jobs - 1)
            if apply_cooldown:
                state.next_allowed_at = now + self._cooldown_seconds
