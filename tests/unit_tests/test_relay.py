import asyncio
import logging
import socket
import struct
from unittest.mock import Mock

import pytest

from daiv_sandbox.config import settings
from daiv_sandbox.relay import RelayPump, open_exec_stream


def _docker_frame(stream_type: int, payload: bytes) -> bytes:
    return struct.pack(">BxxxL", stream_type, len(payload)) + payload


class FakeExecs:
    """A Docker client whose execs are socketpairs; ``process[cmd[1]]`` is the exec process's end."""

    def __init__(self):
        self.created: list[dict] = []
        self.process: dict[str, socket.socket] = {}
        self.client = Mock()
        self.client.api.exec_create.side_effect = self._create
        self.client.api.exec_start.side_effect = self._start
        self._pending: dict[str, str] = {}

    def _create(self, container_id, cmd, **kwargs):
        exec_id = f"exec-{len(self.created)}"
        self.created.append({"container": container_id, "cmd": cmd, **kwargs})
        self._pending[exec_id] = cmd[1]
        return {"Id": exec_id}

    def _start(self, exec_id, socket):  # noqa: A002 - mirrors docker SDK kwarg name
        service_end, process_end = _socketpair()
        self.process[self._pending.pop(exec_id)] = process_end
        return service_end


def _socketpair():
    a, b = socket.socketpair()
    b.setblocking(False)
    return a, b


async def _recv(sock: socket.socket, size: int) -> bytes:
    loop = asyncio.get_running_loop()
    data = b""
    while len(data) < size:
        chunk = await asyncio.wait_for(loop.sock_recv(sock, size - len(data)), timeout=5)
        if not chunk:
            break
        data += chunk
    return data


async def _send(sock: socket.socket, data: bytes) -> None:
    await asyncio.get_running_loop().sock_sendall(sock, data)


@pytest.fixture
def execs():
    return FakeExecs()


async def test_exec_stream_yields_stdout_and_logs_stderr(execs, caplog):
    stream = await open_exec_stream(execs.client, "box", ["daiv-relay", "connect", "39222"], user="1000:1000")
    process = execs.process["connect"]
    await _send(process, _docker_frame(1, b"hello") + _docker_frame(2, b"oops\n") + _docker_frame(1, b" world"))
    process.close()

    with caplog.at_level(logging.WARNING, logger="daiv_sandbox.browser"):
        chunks = [chunk async for chunk in stream.stdout_chunks()]

    assert chunks == [b"hello", b" world"]
    assert "daiv-relay connect: oops" in caplog.text
    assert execs.created[0] == {
        "container": "box",
        "cmd": ["daiv-relay", "connect", "39222"],
        "stdin": True,
        "stdout": True,
        "stderr": True,
        "user": "1000:1000",
    }
    await stream.close()


async def _started_pump(execs, egress_proxy=None) -> RelayPump:
    pump = RelayPump(
        execs.client,
        sandbox_id="sbx",
        browser_id="brw",
        relay_path="/home/daiv-sandbox/.daiv/relay",
        browser_user="1000:1000",
    )
    await pump.start(egress_proxy=egress_proxy)
    return pump


async def test_pump_starts_serve_in_the_sandbox_and_listen_in_the_browser(execs):
    pump = await _started_pump(execs, egress_proxy="10.0.0.2:8080")

    assert [(c["container"], c["cmd"], c["user"]) for c in execs.created] == [
        ("sbx", ["/home/daiv-sandbox/.daiv/relay", "serve"], f"{settings.RUN_UID}:{settings.RUN_GID}"),
        ("brw", ["daiv-relay", "listen", "--egress-proxy", "10.0.0.2:8080"], "1000:1000"),
    ]
    await pump.stop()


async def test_pump_carries_each_sides_stdout_to_the_others_stdin(execs):
    pump = await _started_pump(execs)
    serve, listen = execs.process["serve"], execs.process["listen"]

    await _send(listen, _docker_frame(1, b"open 5173"))
    assert await _recv(serve, 9) == b"open 5173"
    await _send(serve, _docker_frame(2, b"a log line") + _docker_frame(1, b"ok"))
    assert await _recv(listen, 2) == b"ok"
    assert pump.alive
    await pump.stop()


async def test_pump_stops_both_relays_when_one_exits(execs):
    pump = await _started_pump(execs)
    serve, listen = execs.process["serve"], execs.process["listen"]

    serve.close()

    assert await _recv(listen, 1) == b""
    await asyncio.wait_for(pump._task, timeout=5)
    assert not pump.alive


async def test_stop_closes_both_relays_stdin(execs):
    pump = await _started_pump(execs)
    serve, listen = execs.process["serve"], execs.process["listen"]

    await pump.stop()

    assert await _recv(serve, 1) == b""
    assert await _recv(listen, 1) == b""
    assert not pump.alive


async def test_a_failed_listen_start_closes_the_serve_exec(execs):
    starts = execs.client.api.exec_start.side_effect

    def _second_fails(exec_id, socket):  # noqa: A002 - mirrors docker SDK kwarg name
        if exec_id == "exec-1":
            raise RuntimeError("browser container is gone")
        return starts(exec_id, socket)

    execs.client.api.exec_start.side_effect = _second_fails
    pump = RelayPump(execs.client, sandbox_id="sbx", browser_id="brw", relay_path="/r", browser_user="1000:1000")

    with pytest.raises(RuntimeError):
        await pump.start(egress_proxy=None)

    assert await _recv(execs.process["serve"], 1) == b""
    assert not pump.alive


async def test_on_exit_runs_once_the_pump_stops(execs):
    pump = await _started_pump(execs)
    exited = asyncio.Event()
    pump.on_exit(exited.set)

    execs.process["listen"].close()

    await asyncio.wait_for(exited.wait(), timeout=5)
