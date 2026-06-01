from datetime import UTC, datetime
from unittest.mock import Mock

from daiv_sandbox.locks import NoopSessionLockManager, SessionBusyError
from daiv_sandbox.reaper import _list_stopped_sandbox_containers, _parse_docker_timestamp, _reap_once, _remove_guarded
from daiv_sandbox.sessions import DAIV_SANDBOX_TYPE_LABEL, TYPE_CMD_EXECUTOR


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
    return Mock(id=cid, status="exited", attrs={"State": {"FinishedAt": finished_at}}, remove=Mock())


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


async def test_reap_once_removes_only_aged_out():
    old = _stopped_container("old", "2026-05-31T00:00:00Z")  # >12h before NOW
    fresh = _stopped_container("fresh", "2026-06-01T11:59:00Z")  # 1m before NOW
    client = Mock()
    client.containers.list.return_value = [old, fresh]

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

    await _reap_once(client, NoopSessionLockManager(), now=NOW, grace_seconds=43200, max_stopped=1)

    c1.remove.assert_called_once_with(force=True)
    c2.remove.assert_called_once_with(force=True)
    c3.remove.assert_not_called()
