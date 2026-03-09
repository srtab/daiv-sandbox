from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from redis.exceptions import LockError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from redis.asyncio import Redis
    from redis.asyncio.lock import Lock


class SessionBusyError(RuntimeError):
    def __init__(self, session_id: str):
        super().__init__(f"Session '{session_id}' is busy")
        self.session_id = session_id


class NoopSessionLockManager:
    @asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncIterator[None]:
        del session_id
        yield


class RedisSessionLockManager:
    def __init__(
        self,
        redis_client: Redis,
        *,
        key_prefix: str = "daiv-sandbox:session-lock",
        ttl_seconds: int = 900,
        wait_seconds: float = 1.0,
        refresh_interval_seconds: float = 30.0,
        acquire_sleep_seconds: float = 0.1,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if wait_seconds < 0:
            raise ValueError("wait_seconds must be greater than or equal to zero")
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be greater than zero")
        if acquire_sleep_seconds <= 0:
            raise ValueError("acquire_sleep_seconds must be greater than zero")

        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_seconds
        self.wait_seconds = wait_seconds
        self.refresh_interval_seconds = refresh_interval_seconds
        self.acquire_sleep_seconds = acquire_sleep_seconds

    def _key(self, session_id: str) -> str:
        return f"{self.key_prefix}:{session_id}"

    async def _refresh_loop(self, lock: Lock) -> None:
        try:
            while True:
                await asyncio.sleep(self.refresh_interval_seconds)
                refreshed = await lock.reacquire()
                if not refreshed:
                    return
        except asyncio.CancelledError:
            raise
        except LockError:
            return

    @asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncIterator[None]:
        lock = self.redis_client.lock(
            self._key(session_id),
            timeout=self.ttl_seconds,
            sleep=self.acquire_sleep_seconds,
            blocking_timeout=self.wait_seconds,
            thread_local=False,
            raise_on_release_error=False,
        )
        acquired = await lock.acquire()
        if not acquired:
            raise SessionBusyError(session_id)

        refresh_task = asyncio.create_task(self._refresh_loop(lock))

        try:
            yield
        finally:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task

            with contextlib.suppress(LockError):
                await lock.release()
