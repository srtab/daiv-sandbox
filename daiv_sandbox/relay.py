"""Byte pumps over Docker exec streams: the browser's loopback relay and the CDP tunnel.

An exec started with ``stdin=True`` and no TTY hands back a hijacked socket whose input is raw and whose
output is Docker's multiplexed stream (an 8-byte header per chunk naming stdout or stderr). These pumps
forward stdout payloads, log stderr, and never parse the bytes they carry.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from daiv_sandbox.config import settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from docker import DockerClient

logger = logging.getLogger("daiv_sandbox.browser")

_STDOUT = 1
_STDERR = 2
_READ_CHUNK = 64 * 1024


@dataclass
class ExecStream:
    exec_id: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    label: str

    async def stdout_chunks(self) -> AsyncIterator[bytes]:
        """Yield the exec's stdout payloads until it exits; stderr lines go to the log."""
        while True:
            try:
                header = await self.reader.readexactly(8)
                payload = await self.reader.readexactly(int.from_bytes(header[4:8], "big"))
            except asyncio.IncompleteReadError, ConnectionError:
                return
            if header[0] == _STDOUT:
                yield payload
            elif header[0] == _STDERR:
                logger.warning("%s: %s", self.label, payload.decode(errors="replace").rstrip())

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.writer.can_write_eof():
                self.writer.write_eof()
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


async def open_exec_stream(client: DockerClient, container_id: str, cmd: list[str], *, user: str) -> ExecStream:
    def _start():
        exec_id = client.api.exec_create(container_id, cmd, stdin=True, stdout=True, stderr=True, user=user)["Id"]
        return exec_id, client.api.exec_start(exec_id, socket=True)

    exec_id, sock = await asyncio.to_thread(_start)
    raw = getattr(sock, "_sock", sock)
    reader, writer = await asyncio.open_connection(sock=raw)
    return ExecStream(exec_id=exec_id, reader=reader, writer=writer, label=f"{cmd[0]} {cmd[1]}")


async def _forward(source: ExecStream, sink_write: Callable[[bytes], Awaitable[object]]) -> None:
    async for chunk in source.stdout_chunks():
        await sink_write(chunk)


class RelayPump:
    """Pumps ``daiv-relay serve`` in the sandbox against ``daiv-relay listen`` in the browser container."""

    def __init__(self, client: DockerClient, *, sandbox_id: str, browser_id: str, relay_path: str, browser_user: str):
        self.client = client
        self.sandbox_id = sandbox_id
        self.browser_id = browser_id
        self.relay_path = relay_path
        self.browser_user = browser_user
        self._task: asyncio.Task | None = None
        self._streams: list[ExecStream] = []

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def on_exit(self, callback: Callable[[], object]) -> None:
        if self._task is not None:
            self._task.add_done_callback(lambda _task: callback())

    async def start(self, *, egress_proxy: str | None) -> None:
        serve = await open_exec_stream(
            self.client, self.sandbox_id, [self.relay_path, "serve"], user=f"{settings.RUN_UID}:{settings.RUN_GID}"
        )
        listen_cmd = ["daiv-relay", "listen", *(["--egress-proxy", egress_proxy] if egress_proxy else [])]
        try:
            listen = await open_exec_stream(self.client, self.browser_id, listen_cmd, user=self.browser_user)
        except BaseException:
            await serve.close()
            raise
        self._streams = [serve, listen]
        self._task = asyncio.create_task(self._run(serve, listen), name=f"relay-pump-{self.sandbox_id[:12]}")

    async def _run(self, serve: ExecStream, listen: ExecStream) -> None:
        directions = [
            asyncio.create_task(_forward(serve, listen.write)),
            asyncio.create_task(_forward(listen, serve.write)),
        ]
        try:
            await asyncio.wait(directions, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in directions:
                task.cancel()
            await asyncio.gather(*directions, return_exceptions=True)
            await serve.close()
            await listen.close()
            logger.info("Browser: relay pump for session %s stopped", self.sandbox_id)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        for stream in self._streams:
            await stream.close()
