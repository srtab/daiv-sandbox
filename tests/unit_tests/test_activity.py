from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from daiv_sandbox.activity import NoopSessionActivityTracker, RedisSessionActivityTracker


async def test_noop_tracker_is_disabled_and_reports_nothing():
    tracker = NoopSessionActivityTracker()

    assert tracker.enabled is False
    assert await tracker.last_seen("abc") is None
    # touch/forget must be callable no-ops so callers need no branching
    assert await tracker.touch("abc") is None
    assert await tracker.forget("abc") is None


def test_redis_tracker_rejects_non_positive_ttl():
    with pytest.raises(ValueError, match="ttl_seconds must be greater than zero"):
        RedisSessionActivityTracker(AsyncMock(), ttl_seconds=0)


async def test_touch_writes_epoch_with_ttl():
    redis = AsyncMock()
    tracker = RedisSessionActivityTracker(redis, ttl_seconds=120)

    await tracker.touch("abc")

    redis.set.assert_awaited_once()
    key, value = redis.set.await_args.args
    assert key == "daiv-sandbox:session-activity:abc"
    assert float(value) == pytest.approx(datetime.now(UTC).timestamp(), abs=5)
    assert redis.set.await_args.kwargs == {"ex": 120}


async def test_last_seen_parses_stored_epoch():
    moment = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    redis = AsyncMock()
    redis.get.return_value = str(moment.timestamp()).encode()
    tracker = RedisSessionActivityTracker(redis, ttl_seconds=120)

    assert await tracker.last_seen("abc") == moment
    redis.get.assert_awaited_once_with("daiv-sandbox:session-activity:abc")


async def test_last_seen_is_none_when_absent():
    redis = AsyncMock()
    redis.get.return_value = None
    tracker = RedisSessionActivityTracker(redis, ttl_seconds=120)

    assert await tracker.last_seen("abc") is None


@pytest.mark.parametrize(
    "raw",
    [
        b"not-a-timestamp",
        b"",
        b"nan",
        # Out-of-range epochs raise OverflowError/OSError rather than ValueError. These escaped a
        # narrower handler and aborted the whole reaper sweep, including the orphan-triad backstop.
        b"inf",
        b"-inf",
        b"1e30",
        b"1e18",
        b"99999999999999999999",
    ],
)
async def test_last_seen_is_none_on_unparseable_value(raw):
    redis = AsyncMock()
    redis.get.return_value = raw
    tracker = RedisSessionActivityTracker(redis, ttl_seconds=120)

    assert await tracker.last_seen("abc") is None


async def test_last_seen_is_none_when_redis_faults():
    """A Redis fault must not abort the reaper sweep; unknown activity reads as None."""
    redis = AsyncMock()
    redis.get.side_effect = RuntimeError("redis down")
    tracker = RedisSessionActivityTracker(redis, ttl_seconds=120)

    assert await tracker.last_seen("abc") is None


async def test_forget_deletes_key():
    redis = AsyncMock()
    tracker = RedisSessionActivityTracker(redis, ttl_seconds=120)

    await tracker.forget("abc")

    redis.delete.assert_awaited_once_with("daiv-sandbox:session-activity:abc")


async def test_touch_and_forget_swallow_redis_faults():
    redis = AsyncMock()
    redis.set.side_effect = RuntimeError("redis down")
    redis.delete.side_effect = RuntimeError("redis down")
    tracker = RedisSessionActivityTracker(redis, ttl_seconds=120)

    # Activity bookkeeping is best-effort: a Redis fault must never fail the caller's request.
    await tracker.touch("abc")
    await tracker.forget("abc")
