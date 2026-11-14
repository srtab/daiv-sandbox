"""Start Chromium detached on the session's profile and report its DevTools endpoint as JSON.

Runs inside the browser container, as the browser user, through a daiv-sandbox exec.
"""

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROFILE = Path("/profile")
TABS_FILE = PROFILE / ".daiv-tabs.json"
CHROMIUM = "/usr/bin/chromium"
DEVTOOLS_PORT = 39222
RELAY_PORT = 39223
TIMEOUT_EXIT = 5
NOT_RUNNING_EXIT = 3

# Stagehand's own launcher defaults, minus --remote-allow-origins=*, which would let any page drive the browser.
STAGEHAND_FLAGS = [
    "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,"
    "CalculateNativeWinOcclusion,InterestFeedContentSuggestions,"
    "CertificateTransparencyComponentUpdater,AutofillServerCommunication,"
    "PrivacySandboxSettings4,RenderDocument",
    "--disable-component-extensions-with-background-pages",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-client-side-phishing-detection",
    "--disable-sync",
    "--metrics-recording-only",
    "--disable-default-apps",
    "--mute-audio",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-ipc-flooding-protection",
    "--password-store=basic",
    "--use-mock-keychain",
    "--force-fieldtrials=*BackgroundTracing/default/",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--disable-domain-reliability",
    "--propagate-iph-for-testing",
    "--enable-features=WebMCP,WebMCPTesting,DevToolsWebMCPSupport",
    "--window-size=1280,800",
]

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def extension_id(path: str) -> str:
    """The id Chromium gives an unpacked extension: its absolute path's sha256, first 32 hex digits mapped to a-p."""
    digest = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


def chromium_argv(extension: str, urls: list[str]) -> list[str]:
    return [
        CHROMIUM,
        "--headless=new",
        f"--user-data-dir={PROFILE}",
        f"--load-extension={extension}",
        "--enable-unsafe-extension-debugging",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={DEVTOOLS_PORT}",
        f"--remote-allow-origins=chrome-extension://{extension_id(extension)}",
        f"--proxy-server=socks5://127.0.0.1:{RELAY_PORT}",
        f"--proxy-bypass-list=<-loopback>;127.0.0.1:{DEVTOOLS_PORT}",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        *STAGEHAND_FLAGS,
        *(urls or ["about:blank"]),
    ]


def read_tabs() -> tuple[list[str], str]:
    try:
        raw = TABS_FILE.read_text()
    except FileNotFoundError:
        return [], "none"
    except OSError:
        return [], "unreadable"
    try:
        urls = json.loads(raw)
    except ValueError:
        return [], "unreadable"
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        return [], "unreadable"
    return urls, "ok"


def devtools_version() -> dict | None:
    try:
        with _OPENER.open(f"http://127.0.0.1:{DEVTOOLS_PORT}/json/version", timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def ws_path(version: dict) -> str:
    return urllib.parse.urlsplit(version["webSocketDebuggerUrl"]).path


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def install_ca(ca_path: str) -> None:
    nssdb = Path.home() / ".pki" / "nssdb"
    nssdb.mkdir(parents=True, exist_ok=True)
    db = f"sql:{nssdb}"
    if not (nssdb / "cert9.db").exists():
        subprocess.run(["certutil", "-N", "-d", db, "--empty-password"], check=True)
    subprocess.run(["certutil", "-D", "-d", db, "-n", "daiv-egress"], check=False, capture_output=True)
    subprocess.run(["certutil", "-A", "-d", db, "-n", "daiv-egress", "-t", "C,,", "-i", ca_path], check=True)


def wait_until(deadline: float, check) -> bool:
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return False


def report(version: dict, urls: list[str], tabs: str) -> None:
    print(json.dumps({"browser_ws_path": ws_path(version), "restored_urls": urls, "tabs": tabs}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extension")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--ca")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    version = devtools_version()
    if args.probe or version is not None:
        if version is None:
            return NOT_RUNNING_EXIT
        report(version, [], "running")
        return 0
    if not args.extension:
        parser.error("--extension is required to launch")

    deadline = time.monotonic() + args.timeout
    if args.ca:
        install_ca(args.ca)
    urls, tabs = read_tabs()
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (PROFILE / name).unlink(missing_ok=True)
    if not wait_until(deadline, lambda: port_open(RELAY_PORT)):
        print("the relay is not listening", file=sys.stderr)
        return TIMEOUT_EXIT

    with (Path.home() / "chromium.log").open("wb") as log:
        subprocess.Popen(  # noqa: S603
            chromium_argv(args.extension, urls),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    while time.monotonic() < deadline:
        version = devtools_version()
        if version is not None:
            report(version, urls, tabs)
            return 0
        time.sleep(0.1)
    print("Chromium did not open its DevTools port in time", file=sys.stderr)
    return TIMEOUT_EXIT


if __name__ == "__main__":
    sys.exit(main())
