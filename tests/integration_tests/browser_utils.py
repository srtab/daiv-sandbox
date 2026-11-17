"""Drive a session's browser over the service's CDP tunnel the way DAIV does, for the browser integration tests.

``CdpTunnel`` is DAIV's worker-side port-forward (one WebSocket per accepted TCP connection), and ``CdpPage`` is a
minimal CDP client over it, so the tests exercise the real endpoint, relay and browser image end to end.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
import threading
import time
import zipfile
from dataclasses import dataclass

import httpx
import uvicorn
from websockets.asyncio.client import connect

from daiv_sandbox.config import settings


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class LiveServer:
    base_url: str
    ws_url: str
    server: uvicorn.Server
    thread: threading.Thread

    @classmethod
    def start(cls, app) -> LiveServer:
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("the live server did not start")
            time.sleep(0.05)
        return cls(f"http://127.0.0.1:{port}/api/v1", f"ws://127.0.0.1:{port}/api/v1", server, thread)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


def extension_zip() -> bytes:
    """A minimal MV3 extension; the tests drive Chromium over raw CDP, not through Stagehand."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"manifest_version": 3, "name": "daiv-test", "version": "1.0"}))
    return buf.getvalue()


class CdpTunnel:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._server: asyncio.Server | None = None
        self._bridges: set[asyncio.Task] = set()
        self.port = 0

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> CdpTunnel:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None
        self._server.close()
        for task in list(self._bridges):
            task.cancel()
        await asyncio.gather(*self._bridges, return_exceptions=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._bridges.add(task)
        try:
            async with connect(
                self.ws_url, additional_headers={"X-API-Key": settings.API_KEY.get_secret_value()}, max_size=None
            ) as ws:

                async def upstream() -> None:
                    while data := await reader.read(65536):
                        await ws.send(data)

                async def downstream() -> None:
                    async for message in ws:
                        writer.write(message if isinstance(message, bytes) else message.encode())
                        await writer.drain()

                jobs = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
                await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for job in jobs:
                    job.cancel()
        except Exception:  # noqa: S110 - a dropped bridge shows up as a failed CDP call in the test
            pass
        finally:
            writer.close()
            self._bridges.discard(task)

    async def json(self, path: str, *, method: str = "GET"):
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, f"{self.http_url}{path}")
            response.raise_for_status()
            return response.json()


class CdpPage:
    """One CDP target over the tunnel: calls by id, events kept for inspection."""

    def __init__(self, ws):
        self.ws = ws
        self.events: list[dict] = []
        self._next_id = 0

    @classmethod
    @contextlib.asynccontextmanager
    async def new(cls, tunnel: CdpTunnel, url: str = "about:blank"):
        target = await tunnel.json(f"/json/new?{url}", method="PUT")
        async with cls.attach(target["webSocketDebuggerUrl"]) as page:
            yield page

    @classmethod
    @contextlib.asynccontextmanager
    async def attach(cls, ws_url: str):
        async with connect(ws_url, max_size=None) as ws:
            yield cls(ws)

    async def call(self, method: str, params: dict | None = None, *, timeout: float = 30) -> dict:
        self._next_id += 1
        call_id = self._next_id
        await self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        async with asyncio.timeout(timeout):
            while True:
                message = json.loads(await self.ws.recv())
                if message.get("id") == call_id:
                    if "error" in message:
                        raise RuntimeError(f"{method}: {message['error']}")
                    return message.get("result", {})
                self.events.append(message)

    async def evaluate(self, expression: str, *, await_promise: bool = False):
        result = await self.call(
            "Runtime.evaluate", {"expression": expression, "awaitPromise": await_promise, "returnByValue": True}
        )
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("text"))
        return result["result"].get("value")

    async def navigate(self, url: str) -> dict:
        """Navigate and wait for the new document; returns Page.navigate's result (``errorText`` on failure)."""
        result = await self.call("Page.navigate", {"url": url})
        for _retry in range(3):
            # A new tab's own initial about:blank load can still commit and abort ours.
            if result.get("errorText") != "net::ERR_ABORTED":
                break
            await asyncio.sleep(0.5)
            result = await self.call("Page.navigate", {"url": url})
        try:
            await self.wait_for_document("chrome-error://chromewebdata/" if result.get("errorText") else url)
        except TimeoutError as exc:
            raise TimeoutError(f"{exc} (Page.navigate answered {result})") from exc
        return result

    async def wait_for_document(self, url: str, timeout: float = 20) -> None:
        """Wait until the page shows *url*, fully loaded; about:blank also reports readyState complete."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(RuntimeError):
                if await self.evaluate("location.href + ' ' + document.readyState") == f"{url} complete":
                    return
            await asyncio.sleep(0.2)
        shown = await self.evaluate("location.href + ' ' + document.readyState")
        raise TimeoutError(f"the page never finished loading {url}; it shows {shown}")
