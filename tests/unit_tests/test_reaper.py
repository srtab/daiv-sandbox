from datetime import UTC, datetime
from unittest.mock import Mock

from daiv_sandbox.reaper import _list_stopped_sandbox_containers, _parse_docker_timestamp
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
