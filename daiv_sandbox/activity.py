from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class NoopSessionActivityTracker:
    """Activity tracker used when Redis is not configured: records nothing, reports nothing.

    ``enabled = False`` is what makes the reaper skip idle-reaping running sessions altogether.
    Without Redis there is also no per-session lock (``NoopSessionLockManager``), so nothing would
    stop the reaper removing a container from under an in-flight request.
    """

    enabled = False

    async def touch(self, session_id: str) -> None:
        del session_id

    async def last_seen(self, session_id: str) -> datetime | None:
        del session_id
        return None

    async def forget(self, session_id: str) -> None:
        del session_id


class RedisSessionActivityTracker:
    """Records the last time each session was touched by a request, for idle-session reaping.

    Every method is best-effort: a Redis fault is logged and swallowed, never raised at the caller.
    A request must not fail because bookkeeping did, and a sweep must not abort because one read did
    — an unreadable record is reported as "unknown" (None), which the reaper treats as
    seed-and-wait rather than as "idle".
    """

    enabled = True

    def __init__(
        self, redis_client: Redis, *, key_prefix: str = "daiv-sandbox:session-activity", ttl_seconds: int
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_seconds

    def _key(self, session_id: str) -> str:
        return f"{self.key_prefix}:{session_id}"

    async def touch(self, session_id: str) -> None:
        try:
            await self.redis_client.set(self._key(session_id), str(datetime.now(UTC).timestamp()), ex=self.ttl_seconds)
        except Exception:
            logger.exception("activity: failed to record activity for session %s", session_id)

    async def last_seen(self, session_id: str) -> datetime | None:
        """Return when the session was last touched, or None if unknown/unreadable."""
        try:
            raw = await self.redis_client.get(self._key(session_id))
        except Exception:
            logger.exception("activity: failed to read activity for session %s", session_id)
            return None
        if raw is None:
            return None
        try:
            return datetime.fromtimestamp(float(raw), UTC)
        except TypeError, ValueError:
            logger.warning("activity: unparseable activity record for session %s: %r", session_id, raw)
            return None

    async def forget(self, session_id: str) -> None:
        try:
            await self.redis_client.delete(self._key(session_id))
        except Exception:
            logger.exception("activity: failed to clear activity for session %s", session_id)
