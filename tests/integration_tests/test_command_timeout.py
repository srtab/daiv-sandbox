import time
from typing import TYPE_CHECKING

import docker
import pytest

from daiv_sandbox.config import settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

# One line per process: its /proc stat fields from the state onwards, then "|" and its command line.
_PROCESS_SCAN = (
    'for d in /proc/[0-9]*; do read -r s < "$d/stat" && echo "${s##*) }|$(tr \'\\000\' \' \' < "$d/cmdline")"; done'
    " 2>/dev/null"
)


def _available_runtimes() -> list:
    runtimes = docker.from_env().info().get("Runtimes", {})
    return [
        pytest.param(name, marks=pytest.mark.skipif(name not in runtimes, reason=f"{name} runtime not installed"))
        for name in ("runc", "runsc")
    ]


@pytest.fixture(params=_available_runtimes())
def runtime(request, monkeypatch) -> str:
    monkeypatch.setattr(settings, "RUNTIME", request.param)
    return request.param


def _run(client: TestClient, session_id: str, command: str, timeout: int) -> dict:
    resp = client.post(f"/session/{session_id}/", json={"commands": [command], "timeout": timeout})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    return results[0]


def _live_processes_matching(client: TestClient, session_id: str, needle: str) -> list[str]:
    scan = _run(client, session_id, _PROCESS_SCAN, timeout=0)
    assert scan["exit_code"] == 0, scan
    return [line for line in scan["output"].splitlines() if needle in line and not line.startswith("Z ")]


def test_timeout_returns_partial_output_and_kills_the_command(
    client: TestClient, sandbox_session: Callable[..., str], runtime: str
):
    session_id = sandbox_session(base_image="alpine:latest")

    started = time.monotonic()
    result = _run(client, session_id, "echo before; sleep 3017", timeout=2)

    assert time.monotonic() - started < 2 + 10
    assert result["exit_code"] == 124
    assert result["timed_out"] is True
    assert result["output"] == "before\nCommand timed out after 2s and was killed."
    assert _live_processes_matching(client, session_id, "sleep 3017") == []


@pytest.mark.parametrize("base_image", ["alpine:latest", "debian:bookworm-slim"])
def test_timeout_kills_child_processes(
    client: TestClient, sandbox_session: Callable[..., str], runtime: str, base_image: str
):
    session_id = sandbox_session(base_image=base_image)

    result = _run(client, session_id, "sleep 3017 & sleep 3017", timeout=2)

    assert result["timed_out"] is True
    assert _live_processes_matching(client, session_id, "sleep 3017") == []


def test_timeout_kills_children_that_left_the_session_or_cleared_their_environment(
    client: TestClient, sandbox_session: Callable[..., str], runtime: str
):
    session_id = sandbox_session(base_image="alpine:latest")

    result = _run(client, session_id, "setsid sleep 3017 & env -i sleep 3017 & sleep 3017", timeout=2)

    assert result["timed_out"] is True
    assert _live_processes_matching(client, session_id, "sleep 3017") == []


def test_next_request_does_not_overlap_the_killed_command(client: TestClient, sandbox_session: Callable[..., str]):
    """The killed command ignores SIGTERM, so only the SIGKILL after the grace period stops it; the next
    request must still see it fully stopped rather than running alongside it."""
    session_id = sandbox_session(base_image="alpine:latest")
    ticker = "trap '' TERM; while :; do echo tick >> /workspace/tmp/ticks; sleep 0.1; done"

    started = time.monotonic()
    result = _run(client, session_id, ticker, timeout=1)

    assert result["timed_out"] is True
    assert time.monotonic() - started < 1 + 10
    check = _run(
        client,
        session_id,
        "a=$(wc -l < /workspace/tmp/ticks); sleep 1; b=$(wc -l < /workspace/tmp/ticks); echo $a $b",
        0,
    )
    before, after = check["output"].split()
    assert int(before) > 0
    assert before == after


@pytest.mark.parametrize("base_image", ["alpine:latest", "debian:bookworm-slim"])
def test_command_within_timeout_matches_untimed_run(
    client: TestClient, sandbox_session: Callable[..., str], base_image: str
):
    session_id = sandbox_session(base_image=base_image)
    command = "printf 'a\\377b\\n'; pwd; id -u; echo \"$HOME\"; false | true"

    timed = _run(client, session_id, command, timeout=30)
    untimed = _run(client, session_id, command, timeout=0)

    assert timed == untimed
    assert timed["exit_code"] == 1
    assert timed["timed_out"] is False
    assert timed["output"].startswith("a�b\n/workspace/repo\n")
