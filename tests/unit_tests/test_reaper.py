import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from docker.errors import APIError, NotFound

from daiv_sandbox.activity import NoopSessionActivityTracker
from daiv_sandbox.locks import NoopSessionLockManager, SessionBusyError
from daiv_sandbox.reaper import (
    _list_running_sandbox_containers,
    _list_stopped_sandbox_containers,
    _parse_docker_timestamp,
    _reap_idle_running_sessions,
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
    c.remove.assert_called_once_with(force=True, v=True)


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

    await _reap_once(
        client,
        NoopSessionLockManager(),
        now=NOW,
        grace_seconds=43200,
        max_stopped=50,
        activity=NoopSessionActivityTracker(),
        idle_seconds=0,
    )

    old.remove.assert_called_once_with(force=True, v=True)
    fresh.remove.assert_not_called()


async def test_reap_once_lru_evicts_oldest_beyond_cap():
    # All within grace, but cap is 1 -> evict the two oldest, keep the newest.
    c1 = _stopped_container("c1", "2026-06-01T11:00:00Z")
    c2 = _stopped_container("c2", "2026-06-01T11:30:00Z")
    c3 = _stopped_container("c3", "2026-06-01T11:50:00Z")
    client = Mock()
    client.containers.list.return_value = [c3, c1, c2]  # unsorted on purpose
    client.networks.list.return_value = []  # orphan-triad sweep runs every tick now (see _reap_once)

    await _reap_once(
        client,
        NoopSessionLockManager(),
        now=NOW,
        grace_seconds=43200,
        max_stopped=1,
        activity=NoopSessionActivityTracker(),
        idle_seconds=0,
    )

    c1.remove.assert_called_once_with(force=True, v=True)
    c2.remove.assert_called_once_with(force=True, v=True)
    c3.remove.assert_not_called()


async def test_reap_once_max_stopped_zero_evicts_all():
    """max_stopped=0 means retain none: every within-grace survivor is LRU-evicted."""
    c1 = _stopped_container("c1", "2026-06-01T11:00:00Z")  # within grace
    c2 = _stopped_container("c2", "2026-06-01T11:30:00Z")  # within grace
    client = Mock()
    client.containers.list.return_value = [c1, c2]
    client.networks.list.return_value = []  # orphan-triad sweep runs every tick now (see _reap_once)

    await _reap_once(
        client,
        NoopSessionLockManager(),
        now=NOW,
        grace_seconds=43200,
        max_stopped=0,
        activity=NoopSessionActivityTracker(),
        idle_seconds=0,
    )

    c1.remove.assert_called_once_with(force=True, v=True)
    c2.remove.assert_called_once_with(force=True, v=True)


async def test_reap_once_always_runs_orphan_sweep():
    """The orphan-triad sweep is wired into every _reap_once tick (no longer gated on egress being
    configured), so a triad stranded after an operator disables egress is still reclaimed. The sweep's own
    teardown behavior is covered by the test_reap_orphan_triads_* tests below."""
    client = Mock()
    client.containers.list.return_value = []
    with patch("daiv_sandbox.reaper._reap_orphan_triads", new=AsyncMock()) as sweep:
        await _reap_once(
            client,
            NoopSessionLockManager(),
            now=NOW,
            grace_seconds=43200,
            max_stopped=50,
            activity=NoopSessionActivityTracker(),
            idle_seconds=0,
        )
    sweep.assert_awaited_once_with(client, now=NOW, grace_seconds=43200)


async def test_maybe_reap_runs_directly_without_redis():
    client = Mock()
    client.containers.list.return_value = []
    client.networks.list.return_value = []
    # redis=None -> no leader lock, sweep runs inline (no exception, list consulted).
    from daiv_sandbox.reaper import _maybe_reap

    await _maybe_reap(
        client,
        None,
        NoopSessionLockManager(),
        grace_seconds=43200,
        max_stopped=50,
        activity=NoopSessionActivityTracker(),
        idle_seconds=0,
    )
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

    await _maybe_reap(
        client,
        redis,
        NoopSessionLockManager(),
        grace_seconds=43200,
        max_stopped=50,
        activity=NoopSessionActivityTracker(),
        idle_seconds=0,
    )

    client.containers.list.assert_not_called()  # sweep skipped


def test_start_reaper_returns_none_when_disabled(monkeypatch):
    from daiv_sandbox import reaper
    from daiv_sandbox.config import settings as cfg

    monkeypatch.setattr(cfg, "REAPER_ENABLED", False)
    app = Mock()
    assert reaper.start_reaper(app) is None


async def test_start_reaper_forwards_activity_and_idle_window(monkeypatch):
    """Guards the whole feature: dropping either kwarg on the way from settings/app.state into the
    sweep silently restores the unbounded leak, since _reap_idle_running_sessions just returns."""
    from daiv_sandbox import reaper
    from daiv_sandbox.config import settings as cfg

    monkeypatch.setattr(cfg, "REAPER_ENABLED", True)
    monkeypatch.setattr(cfg, "RUNNING_SESSION_MAX_IDLE_SECONDS", 14400)
    monkeypatch.setattr(reaper.SandboxDockerSession, "_get_shared_client", staticmethod(lambda: Mock()))

    tracker = NoopSessionActivityTracker()
    app = Mock()
    app.state.session_activity = tracker

    captured = {}

    async def fake_loop(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(reaper, "_reaper_loop", fake_loop)
    task = reaper.start_reaper(app)
    assert task is not None
    await task

    assert captured["activity"] is tracker
    assert captured["idle_seconds"] == 14400


async def test_maybe_reap_forwards_activity_and_idle_window_to_the_sweep(monkeypatch):
    """_maybe_reap builds sweep_kwargs by hand, so a dropped key here disables idle reaping silently."""
    from daiv_sandbox import reaper

    tracker = NoopSessionActivityTracker()
    captured = {}

    async def fake_reap_once(client, lock_manager, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(reaper, "_reap_once", fake_reap_once)
    await reaper._maybe_reap(
        Mock(),
        None,
        NoopSessionLockManager(),
        grace_seconds=43200,
        max_stopped=50,
        activity=tracker,
        idle_seconds=14400,
    )

    assert captured["activity"] is tracker
    assert captured["idle_seconds"] == 14400


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
        container.remove.assert_called_once_with(force=True, v=True)


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


# --- idle running-session reaping -------------------------------------------------------------


class _FakeActivity:
    """In-memory stand-in for RedisSessionActivityTracker."""

    enabled = True

    def __init__(self, records=None):
        self.records = dict(records or {})
        self.touched: list[str] = []
        self.forgotten: list[str] = []

    async def last_seen(self, session_id):
        return self.records.get(session_id)

    async def touch(self, session_id):
        self.touched.append(session_id)
        self.records[session_id] = datetime.now(UTC)

    async def forget(self, session_id):
        self.forgotten.append(session_id)
        self.records.pop(session_id, None)


def _running_container(cid: str):
    return Mock(id=cid, status="running", labels={}, attrs={"State": {"FinishedAt": ""}}, remove=Mock())


def _running_client(containers):
    client = Mock()
    client.containers.list.return_value = containers
    return client


async def test_list_running_filters_out_stopped():
    running, exited = Mock(status="running"), Mock(status="exited")
    client = Mock()
    client.containers.list.return_value = [running, exited]

    result = _list_running_sandbox_containers(client)

    client.containers.list.assert_called_once_with(filters={"label": f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_CMD_EXECUTOR}"})
    assert result == [running]


async def test_idle_sweep_removes_session_idle_past_window():
    c = _running_container("idle")
    activity = _FakeActivity({"idle": NOW - timedelta(hours=5)})

    await _reap_idle_running_sessions(
        _running_client([c]), NoopSessionLockManager(), activity, now=NOW, idle_seconds=14400
    )

    c.remove.assert_called_once_with(force=True, v=True)
    assert activity.forgotten == ["idle"]


async def test_idle_sweep_keeps_recently_touched_session():
    c = _running_container("busy")
    activity = _FakeActivity({"busy": NOW - timedelta(minutes=5)})

    await _reap_idle_running_sessions(
        _running_client([c]), NoopSessionLockManager(), activity, now=NOW, idle_seconds=14400
    )

    c.remove.assert_not_called()


async def test_idle_sweep_seeds_unknown_session_instead_of_reaping():
    """A running session with no activity record (pre-upgrade, or record lost to a Redis flush) gets a
    full idle window rather than being removed on first sight."""
    c = _running_container("unknown")
    activity = _FakeActivity()

    await _reap_idle_running_sessions(
        _running_client([c]), NoopSessionLockManager(), activity, now=NOW, idle_seconds=14400
    )

    c.remove.assert_not_called()
    assert activity.touched == ["unknown"]


async def test_idle_sweep_skips_busy_session():
    """An in-flight request holds the per-session lock, so a long command is never reaped mid-run
    however stale its activity record looks."""
    c = _running_container("inflight")
    activity = _FakeActivity({"inflight": NOW - timedelta(hours=9)})

    await _reap_idle_running_sessions(_running_client([c]), _BusyLockManager(), activity, now=NOW, idle_seconds=14400)

    c.remove.assert_not_called()
    assert activity.forgotten == []


async def test_idle_sweep_skips_session_touched_while_waiting_for_lock():
    """TOCTOU: a request that refreshed the record between the listing and the lock must save the
    session — the precondition re-reads activity under the lock."""
    c = _running_container("revived")
    activity = _FakeActivity({"revived": NOW - timedelta(hours=9)})

    def _revive():
        activity.records["revived"] = datetime.now(UTC)

    c.reload.side_effect = _revive

    await _reap_idle_running_sessions(
        _running_client([c]), NoopSessionLockManager(), activity, now=NOW, idle_seconds=14400
    )

    c.remove.assert_not_called()


async def test_idle_sweep_tears_down_egress_triad():
    c = _running_container("idle")
    c.labels = {EGRESS_SESSION_LABEL: "tok-idle"}
    activity = _FakeActivity({"idle": NOW - timedelta(hours=5)})

    with patch("daiv_sandbox.reaper.EgressProxyManager") as mgr_cls:
        await _reap_idle_running_sessions(
            _running_client([c]), NoopSessionLockManager(), activity, now=NOW, idle_seconds=14400
        )

    mgr_cls.return_value.teardown.assert_called_once_with("tok-idle")


async def test_idle_sweep_does_not_remove_when_activity_read_fails_under_lock():
    """An unknown activity read on the destructive branch must fail CLOSED.

    last_seen() returns None for a transient Redis fault as well as for "no record", and the outer
    sweep treats None as seed-and-wait. If the precondition read it as "still idle", one failed GET
    between the listing and the lock would force-remove a live container and its writable layer."""
    c = _running_container("live")
    activity = _FakeActivity({"live": NOW - timedelta(hours=9)})

    calls = {"n": 0}
    original = activity.last_seen

    async def flaky(session_id):
        calls["n"] += 1
        return await original(session_id) if calls["n"] == 1 else None

    activity.last_seen = flaky

    await _reap_idle_running_sessions(
        _running_client([c]), NoopSessionLockManager(), activity, now=NOW, idle_seconds=14400
    )

    c.remove.assert_not_called()
    assert activity.forgotten == []


async def test_idle_sweep_does_not_remove_a_container_that_stopped_during_the_lock_wait():
    """A container that stops between the listing and the lock belongs to the stopped-container sweep.

    Force-removing it on the idle path would bypass SESSION_GRACE_SECONDS and the MAX_STOPPED_SESSIONS
    LRU, destroying a writable layer that is still meant to be warm-reusable. The record is left intact
    here on purpose, so this isolates the run-state re-check rather than the fail-closed activity
    guard: a crashed/OOM-killed sandbox stops without anything clearing its activity record."""
    c = _running_container("warm")
    activity = _FakeActivity({"warm": NOW - timedelta(hours=5)})

    def _stops_under_the_lock():
        c.status = "exited"

    c.reload.side_effect = _stops_under_the_lock

    await _reap_idle_running_sessions(
        _running_client([c]), NoopSessionLockManager(), activity, now=NOW, idle_seconds=14400
    )

    c.remove.assert_not_called()
    assert activity.forgotten == []


async def test_idle_sweep_isolates_a_failing_activity_read_per_container():
    """One unreadable record must not starve the sessions listed after it, mirroring the per-token
    isolation in _reap_orphan_triads. Otherwise a single poisoned key stalls every sweep."""
    bad = _running_container("bad")
    good = _running_container("good")
    activity = _FakeActivity({"good": NOW - timedelta(hours=9)})

    original = activity.last_seen

    async def raising(session_id):
        if session_id == "bad":
            raise OverflowError("timestamp out of range for platform time_t")
        return await original(session_id)

    activity.last_seen = raising

    await _reap_idle_running_sessions(
        _running_client([bad, good]), NoopSessionLockManager(), activity, now=NOW, idle_seconds=14400
    )

    bad.remove.assert_not_called()
    good.remove.assert_called_once_with(force=True, v=True)


async def test_remove_guarded_reports_success_when_only_egress_teardown_fails():
    """The container is gone and its token is now unreferenced, so the orphan-triad sweep finishes the
    job. Reporting False would both misdescribe the failure and skip the activity-record cleanup."""
    c = _stopped_container("gone", "2026-06-01T00:00:00Z")
    c.labels = {EGRESS_SESSION_LABEL: "tok-gone"}

    with patch("daiv_sandbox.reaper.EgressProxyManager") as mgr_cls:
        mgr_cls.return_value.teardown.side_effect = APIError("daemon hiccup")
        removed = await _remove_guarded(c, NoopSessionLockManager())

    assert removed is True
    c.remove.assert_called_once_with(force=True, v=True)


async def test_idle_sweep_disabled_when_window_is_zero():
    c = _running_container("idle")
    activity = _FakeActivity({"idle": NOW - timedelta(days=30)})
    client = _running_client([c])

    await _reap_idle_running_sessions(client, NoopSessionLockManager(), activity, now=NOW, idle_seconds=0)

    c.remove.assert_not_called()
    client.containers.list.assert_not_called()


async def test_idle_sweep_disabled_without_activity_tracking():
    """No Redis means no activity records AND no session lock, so removing a running container could
    kill live work — the sweep must not run at all."""
    c = _running_container("idle")
    client = _running_client([c])

    await _reap_idle_running_sessions(
        client, NoopSessionLockManager(), NoopSessionActivityTracker(), now=NOW, idle_seconds=14400
    )

    c.remove.assert_not_called()
    client.containers.list.assert_not_called()


async def test_reap_once_runs_idle_sweep():
    """_reap_once wires the idle sweep, so a stopped-container-only mock is not enough coverage."""
    idle = _running_container("idle")
    client = Mock()

    def _list(filters=None, **kwargs):
        # A full sweep lists containers four times (stopped, running, orphan proxies, orphan
        # executors); dispatch on the filter so the mock can't be exhausted by call order.
        label = (filters or {}).get("label", "")
        if label.endswith(TYPE_CMD_EXECUTOR):
            return [] if kwargs.get("all") else [idle]
        return []

    client.containers.list.side_effect = _list
    client.networks.list.return_value = []
    activity = _FakeActivity({"idle": NOW - timedelta(hours=5)})

    await _reap_once(
        client,
        NoopSessionLockManager(),
        now=NOW,
        grace_seconds=43200,
        max_stopped=50,
        activity=activity,
        idle_seconds=14400,
    )

    idle.remove.assert_called_once_with(force=True, v=True)
