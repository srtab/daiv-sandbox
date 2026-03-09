import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from daiv_sandbox.locks import RedisSessionLockManager, SessionBusyError


def test_redis_session_lock_manager_releases_owned_lock():
    redis_client = Mock()
    redis_lock = AsyncMock()
    redis_lock.acquire.return_value = True
    redis_lock.release.return_value = None
    redis_client.lock.return_value = redis_lock
    lock_manager = RedisSessionLockManager(
        redis_client, ttl_seconds=10, wait_seconds=0.0, refresh_interval_seconds=60.0
    )

    async def run() -> None:
        async with lock_manager.acquire("session-123"):
            return None

    asyncio.run(run())

    redis_client.lock.assert_called_once_with(
        "daiv-sandbox:session-lock:session-123",
        timeout=10,
        sleep=0.1,
        blocking_timeout=0.0,
        thread_local=False,
        raise_on_release_error=False,
    )
    redis_lock.acquire.assert_awaited_once_with()
    redis_lock.release.assert_awaited_once_with()

    redis_lock.reacquire.assert_not_called()


def test_redis_session_lock_manager_raises_when_session_is_busy():
    redis_client = Mock()
    redis_lock = AsyncMock()
    redis_lock.acquire.return_value = False
    redis_client.lock.return_value = redis_lock
    lock_manager = RedisSessionLockManager(
        redis_client, ttl_seconds=10, wait_seconds=0.0, refresh_interval_seconds=60.0
    )

    async def run() -> None:
        async with lock_manager.acquire("session-123"):
            raise AssertionError("lock should not be acquired")

    with pytest.raises(SessionBusyError, match="session-123"):
        asyncio.run(run())

    redis_client.lock.assert_called_once()
    redis_lock.acquire.assert_awaited_once_with()
    redis_lock.release.assert_not_called()
