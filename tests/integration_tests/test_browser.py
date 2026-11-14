"""The browser beside a session's sandbox, end to end, under runc and runsc.

Needs a Docker daemon with both runtimes and the browser image built locally (`make build-browser`). The egress
scenario also needs the sidecar image built from this checkout and internet access from the host.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json

import docker
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from websockets.exceptions import ConnectionClosed

from daiv_sandbox.browser import BrowserManager, find_browser
from daiv_sandbox.config import settings
from daiv_sandbox.locks import NoopSessionLockManager
from daiv_sandbox.main import app
from daiv_sandbox.reaper import _reap_orphan_browsers, _remove_guarded
from daiv_sandbox.sessions import BROWSER_SESSION_LABEL, DAIV_SANDBOX_TYPE_LABEL, TYPE_BROWSER

from .browser_utils import CdpPage, CdpTunnel, LiveServer, extension_zip

SANDBOX_IMAGE = "python:3.14-slim-trixie"
EGRESS_TEST_IMAGE = "daiv-sandbox-egress:browser-test"
PAGE_TITLE = "daiv local page"
EXTENSION = extension_zip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_present(client, ref: str) -> bool:
    try:
        client.images.get(ref)
    except docker.errors.ImageNotFound:
        return False
    return True


@pytest.fixture(scope="module")
def docker_client():
    return docker.from_env()


@pytest.fixture(params=["runc", "runsc"])
def runtime(request, docker_client):
    if request.param not in docker_client.info().get("Runtimes", {}):
        pytest.skip(f"the Docker daemon has no {request.param} runtime")
    return request.param


@pytest.fixture
def live(runtime, docker_client, monkeypatch):
    if not _image_present(docker_client, settings.BROWSER_IMAGE):
        pytest.skip(f"browser image {settings.BROWSER_IMAGE} not built; run `make build-browser`")
    monkeypatch.setattr(settings, "RUNTIME", runtime)
    monkeypatch.setattr(settings, "REAPER_ENABLED", False)
    original_manager = app.state.browser_manager
    app.state.browser_manager = BrowserManager()
    server = LiveServer.start(app)
    try:
        yield server
    finally:
        server.stop()
        app.state.browser_manager = original_manager


class Api:
    def __init__(self, live: LiveServer, docker_client):
        self.live = live
        self.docker = docker_client
        self.http = httpx.Client(
            base_url=live.base_url, headers={"X-API-Key": settings.API_KEY.get_secret_value()}, timeout=120
        )
        self.sessions: list[str] = []

    def session(self, **payload) -> str:
        response = self.http.post("/session/", json={"base_image": SANDBOX_IMAGE, **payload})
        assert response.status_code == 200, response.text
        session_id = response.json()["session_id"]
        self.sessions.append(session_id)
        return session_id

    def run(self, session_id: str, *commands: str) -> list[dict]:
        response = self.http.post(f"/session/{session_id}/", json={"commands": list(commands), "fail_fast": True})
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert all(r["exit_code"] == 0 for r in results), results
        return results

    def serve_page(self, session_id: str, port: int = 8000, body: str = "ok") -> None:
        html = f"<html><head><title>{PAGE_TITLE}</title></head><body>{body}</body></html>"
        self.run(
            session_id,
            f"mkdir -p /workspace/tmp/site && cd /workspace/tmp/site && printf '%s' '{html}' > index.html"
            f" && (setsid python -m http.server {port} --bind 127.0.0.1 >/dev/null 2>&1 < /dev/null &)"
            f" && for i in $(seq 50); do python -c 'import urllib.request as u;"
            f' u.urlopen("http://127.0.0.1:{port}/")\' 2>/dev/null && exit 0; sleep 0.1; done; exit 1',
        )

    def start_browser(self, session_id: str, *, memory_bytes: int = 1 << 30, archive: bool = True) -> httpx.Response:
        request = {"memory_bytes": memory_bytes, "cpus": 1.0, "extension_sha256": _sha256(EXTENSION)}
        files = {"extension_archive": ("extension.zip", EXTENSION, "application/zip")} if archive else None
        return self.http.post(f"/session/{session_id}/browser/", data={"request": json.dumps(request)}, files=files)

    def stop_browser(self, session_id: str, *, force: bool = False) -> dict:
        response = self.http.delete(f"/session/{session_id}/browser/", params={"force": str(force).lower()})
        assert response.status_code == 200, response.text
        return response.json()

    def tunnel(self, session_id: str) -> CdpTunnel:
        return CdpTunnel(f"{self.live.ws_url}/session/{session_id}/browser/cdp")

    def browser_container(self, session_id: str):
        return find_browser(self.docker, session_id)

    def cleanup(self) -> None:
        for session_id in self.sessions:
            self.http.delete(f"/session/{session_id}/", params={"force": "true"})
            for container in self.docker.containers.list(
                all=True,
                filters={
                    "label": [f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_BROWSER}", f"{BROWSER_SESSION_LABEL}={session_id}"]
                },
            ):
                container.remove(force=True, v=True)
        self.http.close()


@pytest.fixture
def api(live, docker_client):
    api = Api(live, docker_client)
    try:
        yield api
    finally:
        api.cleanup()


def _started(api: Api, session_id: str, **kwargs) -> dict:
    response = api.start_browser(session_id, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


# --- local services and the profile (US1) ----------------------------------------------------------------


async def test_the_browser_opens_a_service_bound_to_the_sandbox_loopback(api):
    session_id = api.session()
    api.serve_page(session_id)
    started = await asyncio.to_thread(_started, api, session_id)
    assert started["started"] is True
    assert started["profile"] == "empty"
    assert started["devtools_port"] == 39222
    assert started["browser_ws_path"].startswith("/devtools/browser/")

    async with api.tunnel(session_id) as tunnel, CdpPage.new(tunnel) as page:
        for url in ("http://localhost:8000/", "http://127.0.0.1:8000/"):
            result = await page.navigate(url)
            assert "errorText" not in result, result
            assert await page.evaluate("document.title") == PAGE_TITLE

        result = await page.navigate("http://localhost:8001/")
        assert result.get("errorText"), "a port with nothing listening must fail the navigation"

    again = await asyncio.to_thread(_started, api, session_id)
    assert again["started"] is False
    assert again["browser_ws_path"] == started["browser_ws_path"]


async def test_the_profile_keeps_cookies_and_tabs_across_a_stop(api):
    session_id = api.session()
    api.serve_page(session_id)
    await asyncio.to_thread(_started, api, session_id)
    async with api.tunnel(session_id) as tunnel, CdpPage.new(tunnel) as page:
        await page.navigate("http://localhost:8000/")
        await page.evaluate("document.cookie = 'daiv=signed-in; max-age=3600; path=/'")

    stopped = await asyncio.to_thread(api.stop_browser, session_id)
    assert stopped == {"saved_urls": ["http://localhost:8000/"], "oom_killed": False}
    assert api.browser_container(session_id).status == "exited"

    restarted = await asyncio.to_thread(_started, api, session_id, archive=False)
    assert restarted["profile"] == "restored"
    assert restarted["restored_urls"] == ["http://localhost:8000/"]
    async with api.tunnel(session_id) as tunnel:
        pages = [t for t in await tunnel.json("/json/list") if t["type"] == "page"]
        restored = next(t for t in pages if t["url"] == "http://localhost:8000/")
        async with CdpPage.attach(restored["webSocketDebuggerUrl"]) as page:
            await page.wait_for_document("http://localhost:8000/")
            assert "daiv=signed-in" in await page.evaluate("document.cookie")


async def test_a_browser_recreated_after_an_outside_removal_starts_reset(api):
    session_id = api.session()
    await asyncio.to_thread(_started, api, session_id)
    await asyncio.to_thread(api.browser_container(session_id).remove, force=True)

    restarted = await asyncio.to_thread(_started, api, session_id)

    assert restarted["profile"] == "reset"


async def test_a_start_without_the_archive_of_an_uncached_extension_is_refused(api):
    session_id = api.session()
    response = await asyncio.to_thread(api.start_browser, session_id, archive=False)
    assert response.status_code == 409
    assert response.json() == {"detail": "extension missing"}


# --- reach and isolation (US2) ---------------------------------------------------------------------------


async def test_a_network_none_session_reaches_no_external_host(api):
    session_id = api.session()
    await asyncio.to_thread(_started, api, session_id)

    async with api.tunnel(session_id) as tunnel, CdpPage.new(tunnel) as page:
        result = await page.navigate("https://example.com/")
        assert result.get("errorText") == "net::ERR_SOCKS_CONNECTION_FAILED"
        assert await page.evaluate("location.href") == "chrome-error://chromewebdata/"


async def test_a_page_cannot_reach_the_devtools_port(api):
    session_id = api.session()
    api.serve_page(session_id)
    started = await asyncio.to_thread(_started, api, session_id)

    async with api.tunnel(session_id) as tunnel, CdpPage.new(tunnel) as page:
        await page.navigate("http://localhost:8000/")
        fetched = await page.evaluate(
            "fetch('http://127.0.0.1:39222/json/version').then(r => r.text()).catch(e => 'refused: ' + e.name)",
            await_promise=True,
        )
        assert fetched.startswith("refused"), fetched
        opened = await page.evaluate(
            "new Promise(resolve => { const ws = new WebSocket('ws://127.0.0.1:39222"
            + started["browser_ws_path"]
            + "'); ws.onopen = () => resolve('open'); ws.onerror = () => resolve('refused'); })",
            await_promise=True,
        )
        assert opened == "refused"


async def test_sessions_never_share_cookies(api):
    first, second = api.session(), api.session()
    for session_id in (first, second):
        api.serve_page(session_id)
        await asyncio.to_thread(_started, api, session_id)

    async with api.tunnel(first) as tunnel, CdpPage.new(tunnel) as page:
        await page.navigate("http://localhost:8000/")
        await page.evaluate("document.cookie = 'owner=first; max-age=3600; path=/'")
    async with api.tunnel(second) as tunnel, CdpPage.new(tunnel) as page:
        await page.navigate("http://localhost:8000/")
        assert await page.evaluate("document.cookie") == ""


async def test_downloads_are_blocked_by_policy(api):
    session_id = api.session()
    api.serve_page(session_id)
    api.run(session_id, "head -c 4096 /dev/urandom > /workspace/tmp/site/file.bin")
    await asyncio.to_thread(_started, api, session_id)

    async with api.tunnel(session_id) as tunnel:
        version = await tunnel.json("/json/version")
        async with CdpPage.attach(version["webSocketDebuggerUrl"]) as browser:
            await browser.call(
                "Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": "/home/browser/downloads"}
            )
        async with CdpPage.new(tunnel) as page:
            await page.navigate("http://localhost:8000/")
            await page.evaluate(
                "const a = document.createElement('a'); a.href = '/file.bin'; a.download = 'file.bin';"
                " document.body.appendChild(a); a.click();"
            )
            await asyncio.sleep(3)

    listing = await asyncio.to_thread(
        api.browser_container(session_id).exec_run, ["sh", "-c", "ls -A /home/browser/downloads 2>/dev/null"]
    )
    assert listing.output.strip() == b""


def test_capabilities_report_the_browser_without_a_key(live):
    response = httpx.get(f"{live.base_url}/-/capabilities/", timeout=10)
    assert response.status_code == 200
    assert response.json() == {"browser": True}


def _throwaway_ca(tmp_path) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "daiv-browser-test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509
        .CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "ca.crt", tmp_path / "ca.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )
    return str(cert_path), str(key_path)


async def test_a_networked_session_reaches_only_the_allowed_host(api, docker_client, monkeypatch, tmp_path):
    if not _image_present(docker_client, EGRESS_TEST_IMAGE):
        pytest.skip(f"egress image {EGRESS_TEST_IMAGE} not built from this checkout")
    cert, key = _throwaway_ca(tmp_path)
    monkeypatch.setattr(settings, "EGRESS_CA_CERT_FILE", cert)
    monkeypatch.setattr(settings, "EGRESS_CA_KEY_FILE", key)
    monkeypatch.setattr(settings, "EGRESS_PROXY_IMAGE", EGRESS_TEST_IMAGE)
    session_id = api.session(egress={"policy": {"default": "deny", "rules": [{"host": "example.com"}]}})
    await asyncio.to_thread(_started, api, session_id)

    async with api.tunnel(session_id) as tunnel, CdpPage.new(tunnel) as page:
        result = await page.navigate("https://example.com/")
        assert "errorText" not in result, result
        assert await page.evaluate("document.title") == "Example Domain"
        assert await page.evaluate("fetch('/').then(r => r.status)", await_promise=True) == 200
        blocked = await page.evaluate(
            "fetch('https://www.iana.org/').then(r => 'loaded ' + r.status).catch(e => 'blocked')", await_promise=True
        )
        assert blocked == "blocked"
        image = await page.evaluate(
            "new Promise(resolve => { const i = new Image(); i.onload = () => resolve('loaded');"
            " i.onerror = () => resolve('blocked'); i.src = 'https://www.iana.org/favicon.ico'; })",
            await_promise=True,
        )
        assert image == "blocked"
        refused = await page.navigate("https://www.iana.org/")
        assert refused.get("errorText") == "net::ERR_SOCKS_CONNECTION_FAILED"

    token = api.browser_container(session_id).labels["daiv.sandbox.egress"]
    api.stop_browser(session_id)
    assert api.http.delete(f"/session/{session_id}/").status_code == 204
    # mitmdump block-buffers its log when stdout is not a TTY; the stop above flushed it.
    proxy = docker_client.containers.list(
        all=True, filters={"label": ["daiv.sandbox.type=egress_proxy", f"daiv.sandbox.egress={token}"]}
    )[0]
    assert "blocked CONNECT www.iana.org" in proxy.logs().decode()

    restarted = await asyncio.to_thread(_started, api, session_id, archive=False)
    assert restarted["profile"] == "restored"
    async with api.tunnel(session_id) as tunnel, CdpPage.new(tunnel) as page:
        result = await page.navigate("https://example.com/")
        assert "errorText" not in result, "the browser must come back on the resumed egress network"


# --- lifecycle (US4) -------------------------------------------------------------------------------------------


def _container_gone(docker_client, container_id: str) -> bool:
    try:
        docker_client.containers.get(container_id)
    except docker.errors.NotFound:
        return True
    return False


async def test_a_sandbox_stop_stops_its_browser_and_force_close_removes_it(api, docker_client):
    session_id = api.session()
    await asyncio.to_thread(_started, api, session_id)
    browser_id = api.browser_container(session_id).id

    assert api.http.delete(f"/session/{session_id}/").status_code == 204
    assert docker_client.containers.get(browser_id).status == "exited"

    assert api.http.delete(f"/session/{session_id}/", params={"force": "true"}).status_code == 204
    assert _container_gone(docker_client, browser_id)


async def test_the_reaper_removes_the_browser_with_its_sandbox(api, docker_client):
    session_id = api.session()
    await asyncio.to_thread(_started, api, session_id)
    browser_id = api.browser_container(session_id).id
    assert api.http.delete(f"/session/{session_id}/").status_code == 204

    sandbox = docker_client.containers.get(session_id)
    assert await _remove_guarded(sandbox, NoopSessionLockManager()) is True

    assert _container_gone(docker_client, browser_id)


class _ScopedClient:
    """Lists only this test's browser, so the orphan sweep never touches another browser on the host."""

    def __init__(self, client, session_id: str):
        self._client = client
        self._session_id = session_id
        self.containers = self

    def list(self, all=True, filters=None):  # noqa: A002 - mirrors docker SDK kwarg name
        label = (filters or {}).get("label")
        if label == f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_BROWSER}":
            filters = {"label": [label, f"{BROWSER_SESSION_LABEL}={self._session_id}"]}
        return self._client.containers.list(all=all, filters=filters)


async def test_the_orphan_sweep_removes_a_browser_whose_sandbox_is_gone(api, docker_client):
    session_id = api.session()
    await asyncio.to_thread(_started, api, session_id)
    browser_id = api.browser_container(session_id).id
    await asyncio.to_thread(docker_client.containers.get(session_id).remove, force=True)

    await _reap_orphan_browsers(_ScopedClient(docker_client, session_id))

    assert _container_gone(docker_client, browser_id)


async def _page_crashed(page: CdpPage, timeout: float) -> bool:
    """Whether the page's renderer died: a targetCrashed event, or its socket dropping with it."""
    try:
        async with asyncio.timeout(timeout):
            while not any(event.get("method") == "Inspector.targetCrashed" for event in page.events):
                page.events.append(json.loads(await page.ws.recv()))
    except TimeoutError:
        return False
    except ConnectionClosed:
        return True
    return True


async def test_a_page_past_the_memory_limit_kills_only_the_browser(api, docker_client, runtime):
    session_id = api.session()
    api.serve_page(session_id)
    api.run(session_id, "(setsid sleep 600 >/dev/null 2>&1 < /dev/null & echo $! > /workspace/tmp/sleep.pid)")
    # Under runsc, 512 MiB is already tight enough to kill the renderer of a plain page.
    await asyncio.to_thread(_started, api, session_id, memory_bytes=768 << 20)
    browser = api.browser_container(session_id)

    async with api.tunnel(session_id) as tunnel, CdpPage.new(tunnel) as page:
        await page.navigate("http://localhost:8000/")
        await page.call("Inspector.enable")
        # Fire and forget: the renderer is killed before it could answer.
        await page.ws.send(
            json.dumps({
                "id": 999,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": "window.hog = []; setInterval(() => { for (let i = 0; i < 20; i++)"
                    " window.hog.push(new Array(1 << 20).fill(Math.random())); }, 10); 'started'"
                },
            })
        )
        assert await _page_crashed(page, timeout=60), "the page outgrew the browser's memory limit without dying"

    api.run(session_id, "kill -0 $(cat /workspace/tmp/sleep.pid)")
    browser.reload()
    stopped = await asyncio.to_thread(api.stop_browser, session_id)
    # gVisor kills the renderer inside its own sandbox, so Docker records no OOM kill under runsc.
    assert stopped["oom_killed"] is (runtime == "runc")
    assert browser.attrs["State"]["OOMKilled"] is (runtime == "runc")
