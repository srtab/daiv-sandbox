import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from docker.errors import APIError, NotFound

from daiv_sandbox.locks import NoopSessionLockManager, SessionBusyError
from daiv_sandbox.reaper import (
    _list_stopped_sandbox_containers,
    _parse_docker_timestamp,
    _reap_once,
    _reap_orphan_triads,
    _remove_guarded,
)
from daiv_sandbox.sessions import DAIV_SANDBOX_TYPE_LABEL, EGRESS_SESSION_LABEL, TYPE_CMD_EXECUTOR, TYPE_EGRESS_PROXY


def test_parse_nanosecond_timestamp_truncates_to_micros():
    dt = _parse_docker_timestamp("2026-06-01T12:34:56.123456789Z")
    assert dt == datetime(2026, 6, 1, 12, 34, 56, 123456, tzinfo=UTC)


def test_parse_timestamp_without_fraction():
    dt = _parse_docker_timestamp("2026-06-01T12:34:56Z")
    assert dt == datetime(2026, 6, 1, 12, 34, 56, tzinfo=UTC)


def test_parse_zero_value_is_none():
    assert _parse_docker_timestamp("0001-01-01T00:00:00Z") is None


def test_parse_empty_is_none():
    assert _parse_docker_timestamp("") is None


def test_parse_garbage_is_none():
    assert _parse_docker_timestamp("not-a-timestamp") is None


def test_list_stopped_filters_out_running():
    running = Mock(status="running")
    exited = Mock(status="exited")
    dead = Mock(status="dead")
    client = Mock()
    client.containers.list.return_value = [running, exited, dead]

    result = _list_stopped_sandbox_containers(client)

    client.containers.list.assert_called_once_with(
        all=True, filters={"label": f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_CMD_EXECUTOR}"}
    )
    assert result == [exited, dead]


def _stopped_container(cid: str, finished_at: str):
    return Mock(id=cid, status="exited", labels={}, attrs={"State": {"FinishedAt": finished_at}}, remove=Mock())


class _BusyLockManager:
    """Lock manager whose acquire always reports the session busy."""

    def acquire(self, session_id):
        class _Ctx:
            async def __aenter__(self):
                raise SessionBusyError(session_id)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


async def test_remove_guarded_removes_when_lock_free():
    c = _stopped_container("a", "2026-06-01T00:00:00Z")
    removed = await _remove_guarded(c, NoopSessionLockManager())
    assert removed is True
    c.remove.assert_called_once_with(force=True)


async def test_remove_guarded_skips_when_busy():
    c = _stopped_container("a", "2026-06-01T00:00:00Z")
    removed = await _remove_guarded(c, _BusyLockManager())
    assert removed is False
    c.remove.assert_not_called()


async def test_remove_guarded_skips_when_running_again():
    """A container warmed (restarted) between listing and removal must not be reaped: the re-read
    under the lock sees status=running and skips it (closes the list-then-restart TOCTOU)."""
    c = _stopped_container("a", "2026-06-01T00:00:00Z")

    def _warm():
        c.status = "running"

    c.reload.side_effect = _warm
    removed = await _remove_guarded(c, NoopSessionLockManager())
    assert removed is False
    c.remove.assert_not_called()


async def test_remove_guarded_treats_vanished_as_removed():
    """A container that vanished between listing and the under-lock reload counts as reaped."""
    c = _stopped_container("a", "2026-06-01T00:00:00Z")
    c.reload.side_effect = NotFound("gone")
    removed = await _remove_guarded(c, NoopSessionLockManager())
    assert removed is True
    c.remove.assert_not_called()


async def test_remove_guarded_swallows_docker_error():
    """A Docker error during removal is logged and swallowed (returns False) so one bad container
    can't abort the rest of the sweep."""
    c = _stopped_container("a", "2026-06-01T00:00:00Z")
    c.remove.side_effect = APIError("boom")
    removed = await _remove_guarded(c, NoopSessionLockManager())
    assert removed is False


async def test_reap_once_removes_only_aged_out():
    old = _stopped_container("old", "2026-05-31T00:00:00Z")  # >12h before NOW
    fresh = _stopped_container("fresh", "2026-06-01T11:59:00Z")  # 1m before NOW
    client = Mock()
    client.containers.list.return_value = [old, fresh]
    client.networks.list.return_value = []  # orphan-triad sweep runs every tick now (see _reap_once)

    await _reap_once(client, NoopSessionLockManager(), now=NOW, grace_seconds=43200, max_stopped=50)

    old.remove.assert_called_once_with(force=True)
    fresh.remove.assert_not_called()


async def test_reap_once_lru_evicts_oldest_beyond_cap():
    # All within grace, but cap is 1 -> evict the two oldest, keep the newest.
    c1 = _stopped_container("c1", "2026-06-01T11:00:00Z")
    c2 = _stopped_container("c2", "2026-06-01T11:30:00Z")
    c3 = _stopped_container("c3", "2026-06-01T11:50:00Z")
    client = Mock()
    client.containers.list.return_value = [c3, c1, c2]  # unsorted on purpose
    client.networks.list.return_value = []  # orphan-triad sweep runs every tick now (see _reap_once)

    await _reap_once(client, NoopSessionLockManager(), now=NOW, grace_seconds=43200, max_stopped=1)

    c1.remove.assert_called_once_with(force=True)
    c2.remove.assert_called_once_with(force=True)
    c3.remove.assert_not_called()


async def test_reap_once_max_stopped_zero_evicts_all():
    """max_stopped=0 means retain none: every within-grace survivor is LRU-evicted."""
    c1 = _stopped_container("c1", "2026-06-01T11:00:00Z")  # within grace
    c2 = _stopped_container("c2", "2026-06-01T11:30:00Z")  # within grace
    client = Mock()
    client.containers.list.return_value = [c1, c2]
    client.networks.list.return_value = []  # orphan-triad sweep runs every tick now (see _reap_once)

    await _reap_once(client, NoopSessionLockManager(), now=NOW, grace_seconds=43200, max_stopped=0)

    c1.remove.assert_called_once_with(force=True)
    c2.remove.assert_called_once_with(force=True)


async def test_reap_once_always_runs_orphan_sweep():
    """The orphan-triad sweep is wired into every _reap_once tick (no longer gated on egress being
    configured), so a triad stranded after an operator disables egress is still reclaimed. The sweep's own
    teardown behavior is covered by the test_reap_orphan_triads_* tests below."""
    client = Mock()
    client.containers.list.return_value = []
    with patch("daiv_sandbox.reaper._reap_orphan_triads", new=AsyncMock()) as sweep:
        await _reap_once(client, NoopSessionLockManager(), now=NOW, grace_seconds=43200, max_stopped=50)
    sweep.assert_awaited_once_with(client, now=NOW, grace_seconds=43200)


async def test_maybe_reap_runs_directly_without_redis():
    client = Mock()
    client.containers.list.return_value = []
    client.networks.list.return_value = []
    # redis=None -> no leader lock, sweep runs inline (no exception, list consulted).
    from daiv_sandbox.reaper import _maybe_reap

    await _maybe_reap(client, None, NoopSessionLockManager(), grace_seconds=43200, max_stopped=50)
    client.containers.list.assert_called()


async def test_maybe_reap_skips_when_not_leader():
    from daiv_sandbox.reaper import _maybe_reap

    client = Mock()
    client.containers.list.return_value = []
    lock = Mock()
    lock.acquire = AsyncMock(return_value=False)  # another replica holds it
    lock.release = AsyncMock(return_value=None)
    redis = Mock()
    redis.lock = Mock(return_value=lock)

    await _maybe_reap(client, redis, NoopSessionLockManager(), grace_seconds=43200, max_stopped=50)

    client.containers.list.assert_not_called()  # sweep skipped


def test_start_reaper_returns_none_when_disabled(monkeypatch):
    from daiv_sandbox import reaper
    from daiv_sandbox.config import settings as cfg

    monkeypatch.setattr(cfg, "REAPER_ENABLED", False)
    app = Mock()
    assert reaper.start_reaper(app) is None


class _Noop:
    def acquire(self, _id):
        class _Ctx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def test_reaper_tears_down_triad_for_egress_sandbox():
    """Reaper tears down the egress triad after force-removing an egress sandbox container."""
    container = MagicMock(id="sbx", status="exited", labels={"daiv.sandbox.egress": "tok123"})
    with patch("daiv_sandbox.reaper.EgressProxyManager") as mock_mgr_class:
        asyncio.run(_remove_guarded(container, _Noop()))
        mock_mgr_class.return_value.teardown.assert_called_once_with("tok123")
        container.remove.assert_called_once_with(force=True)


# --- Orphan egress triad sweep ------------------------------------------------

_OLD = "2026-05-31T00:00:00Z"  # >12h before NOW
_FRESH = "2026-06-01T11:59:00Z"  # 1m before NOW


def _egress_client(*, proxies=(), cmd_execs=(), networks=()):
    """A Mock client that routes containers.list by its label filter (egress proxies vs cmd-executors)."""

    def _containers_list(all=True, filters=None):  # noqa: A002 - mirrors docker SDK kwarg name
        label = (filters or {}).get("label")
        if label == f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_EGRESS_PROXY}":
            return list(proxies)
        if label == f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_CMD_EXECUTOR}":
            return list(cmd_execs)
        return []

    client = MagicMock()
    client.containers.list.side_effect = _containers_list
    client.networks.list.return_value = list(networks)
    return client


def _proxy(token, created):
    return MagicMock(labels={EGRESS_SESSION_LABEL: token}, attrs={"Created": created})


def _cmd(token):
    return MagicMock(labels={EGRESS_SESSION_LABEL: token} if token else {})


def _net(token, created):
    return MagicMock(attrs={"Created": created, "Labels": {EGRESS_SESSION_LABEL: token}})


async def test_reap_orphan_triads_tears_down_unreferenced_token():
    """An egress proxy whose token no cmd-executor carries is an orphan and must be torn down."""
    client = _egress_client(proxies=[_proxy("orphan", _OLD)])
    with patch("daiv_sandbox.reaper.EgressProxyManager") as mgr_cls:
        await _reap_orphan_triads(client, now=NOW, grace_seconds=43200)
    mgr_cls.return_value.teardown.assert_called_once_with("orphan")


async def test_reap_orphan_triads_keeps_referenced_token():
    """A triad whose token is carried by a (stopped or running) cmd-executor is in use — keep it."""
    client = _egress_client(proxies=[_proxy("live", _OLD)], cmd_execs=[_cmd("live")])
    with patch("daiv_sandbox.reaper.EgressProxyManager") as mgr_cls:
        await _reap_orphan_triads(client, now=NOW, grace_seconds=43200)
    mgr_cls.return_value.teardown.assert_not_called()


async def test_reap_orphan_triads_skips_recent_token():
    """A triad built during an in-flight start (cmd-executor not yet created/labelled) must not be
    reaped: only resources older than the grace window are swept, closing the mid-start TOCTOU."""
    client = _egress_client(proxies=[_proxy("starting", _FRESH)])
    with patch("daiv_sandbox.reaper.EgressProxyManager") as mgr_cls:
        await _reap_orphan_triads(client, now=NOW, grace_seconds=43200)
    mgr_cls.return_value.teardown.assert_not_called()


async def test_reap_orphan_triads_reaps_network_only_orphan():
    """A lingering internal network whose proxy is already gone is also reclaimed by token."""
    client = _egress_client(networks=[_net("netonly", _OLD)])
    with patch("daiv_sandbox.reaper.EgressProxyManager") as mgr_cls:
        await _reap_orphan_triads(client, now=NOW, grace_seconds=43200)
    mgr_cls.return_value.teardown.assert_called_once_with("netonly")


async def test_reap_orphan_triads_continues_when_one_teardown_fails():
    """A teardown that faults for one orphan must not abort the sweep — the others are still reclaimed
    (the reaper is the resilient backstop; one wedged orphan can't starve the rest every tick)."""
    client = _egress_client(proxies=[_proxy("bad", _OLD), _proxy("good", _OLD)])

    def _teardown(tok):
        if tok == "bad":
            raise APIError("daemon busy")

    with patch("daiv_sandbox.reaper.EgressProxyManager") as mgr_cls:
        mgr_cls.return_value.teardown.side_effect = _teardown
        await _reap_orphan_triads(client, now=NOW, grace_seconds=43200)
    assert mgr_cls.return_value.teardown.call_count == 2  # both attempted despite "bad" raising
