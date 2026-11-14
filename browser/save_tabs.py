"""Save the open tabs' URLs to the profile and, with --close, shut Chromium down so its profile is flushed.

Runs inside the browser container, as the browser user, through a daiv-sandbox exec. Prints {"saved_urls": [...]}.
"""

import argparse
import base64
import contextlib
import json
import os
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROFILE = Path("/profile")
TABS_FILE = PROFILE / ".daiv-tabs.json"
DEVTOOLS_PORT = 39222
CLOSE_WAIT_SECONDS = 5.0
NOT_RESTORABLE = ("about:", "chrome-extension://", "chrome-error://", "devtools://")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def devtools_json(path: str):
    with _OPENER.open(f"http://127.0.0.1:{DEVTOOLS_PORT}{path}", timeout=2) as response:
        return json.load(response)


def restorable_urls(targets: list[dict]) -> list[str]:
    """Page URLs, least recently active first: /json/list orders by last activity, newest first."""
    pages = [t.get("url", "") for t in targets if t.get("type") == "page"]
    return [url for url in reversed(pages) if url and not url.startswith(NOT_RESTORABLE)]


def save(urls: list[str]) -> None:
    tmp = TABS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(urls))
    tmp.replace(TABS_FILE)


def send_browser_close(ws_url: str) -> None:
    path = urllib.parse.urlsplit(ws_url).path
    with socket.create_connection(("127.0.0.1", DEVTOOLS_PORT), timeout=2) as sock:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{DEVTOOLS_PORT}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                return
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            return
        payload = json.dumps({"id": 1, "method": "Browser.close"}).encode()
        mask = os.urandom(4)
        header = bytes([0x81, 0x80 | len(payload)])
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        with contextlib.suppress(OSError):
            sock.recv(4096)


def chromium_pids() -> list[int]:
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() == "chromium":
                pids.append(int(entry.name))
        except OSError:
            continue
    return pids


def close_chromium(version: dict | None) -> None:
    if version is not None:
        try:
            send_browser_close(version["webSocketDebuggerUrl"])
        except (OSError, KeyError) as exc:
            print(f"could not ask Chromium to close: {exc}", file=sys.stderr)
    started = time.monotonic()
    while time.monotonic() - started < CLOSE_WAIT_SECONDS and chromium_pids():
        time.sleep(0.1)
    leftover = chromium_pids()
    if leftover:
        print(
            f"Chromium did not exit within {CLOSE_WAIT_SECONDS:.0f}s; killed {len(leftover)} processes", file=sys.stderr
        )
    for pid in leftover:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--close", action="store_true")
    args = parser.parse_args()

    saved: list[str] = []
    version = None
    try:
        version = devtools_json("/json/version")
        saved = restorable_urls(devtools_json("/json/list"))
        save(saved)
    except (OSError, ValueError) as exc:
        print(f"could not save the open tabs: {exc}", file=sys.stderr)
    if args.close:
        close_chromium(version)
    print(json.dumps({"saved_urls": saved}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
