"""The headless browser daiv-sandbox runs beside a session's sandbox, one browser container per session.

The browser container uses the sandbox's runtime and its own limits, and holds the session's browser profile
in its writable layer between runs. Chromium reaches the sandbox's loopback through ``daiv-relay`` (see
``relay.py``) and the internet only through the session's egress proxy, so its reach is the sandbox's.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import posixpath
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from docker.errors import APIError, ImageNotFound, NotFound

from daiv_sandbox.config import settings
from daiv_sandbox.egress.manager import EgressProxyManager
from daiv_sandbox.relay import RelayPump
from daiv_sandbox.schemas import BrowserStartRequest, BrowserStartResponse, BrowserStopResponse
from daiv_sandbox.sessions import (
    BROWSER_SESSION_LABEL,
    DAIV_SANDBOX_TYPE_LABEL,
    EGRESS_SESSION_LABEL,
    SANDBOX_HOME,
    TYPE_BROWSER,
    SandboxDockerSession,
    egress_token,
)

if TYPE_CHECKING:
    from docker import DockerClient
    from docker.models.containers import Container

logger = logging.getLogger("daiv_sandbox.browser")

BROWSER_UID = 1000
BROWSER_USER = f"{BROWSER_UID}:{BROWSER_UID}"
BROWSER_HOME = "/home/browser"
DEVTOOLS_PORT = 39222
EXTENSION_ROOT = "/opt/daiv/extension"
LAUNCH_SCRIPT = "/opt/daiv/launch.py"
SAVE_TABS_SCRIPT = "/opt/daiv/save_tabs.py"
IMAGE_RELAY_PATH = "/usr/local/bin/daiv-relay"
SANDBOX_DAIV_DIR = f"{SANDBOX_HOME}/.daiv"
SANDBOX_RELAY_PATH = f"{SANDBOX_DAIV_DIR}/relay"
SANDBOX_BROWSER_MARKER = f"{SANDBOX_DAIV_DIR}/browser"
# Never under /tmp: runsc hides what put_archive writes there from the running container.
BROWSER_CA_PATH = f"{BROWSER_HOME}/.daiv/egress-ca.crt"

_LAUNCH_TIMEOUT_EXIT = 5
_SAVE_TABS_TIMEOUT_SECONDS = 15
_MAX_EXTENSION_BYTES = 64 << 20


class BrowserError(Exception):
    """A browser start that failed for a reason the caller can be told about."""


class ExtensionMissingError(BrowserError):
    pass


class ExtensionInvalidError(BrowserError):
    pass


class BrowserNotReadyError(BrowserError):
    pass


class BrowserImageUnavailableError(BrowserError):
    pass


def _browser_labels(session_id: str) -> list[str]:
    return [f"{DAIV_SANDBOX_TYPE_LABEL}={TYPE_BROWSER}", f"{BROWSER_SESSION_LABEL}={session_id}"]


def find_browser(client: DockerClient, session_id: str) -> Container | None:
    found = client.containers.list(all=True, filters={"label": _browser_labels(session_id)})
    return found[0] if found else None


def remove_browser(client: DockerClient, session_id: str) -> bool:
    """Force-remove the session's browser container and its profile; True if one existed."""
    removed = False
    for container in client.containers.list(all=True, filters={"label": _browser_labels(session_id)}):
        with contextlib.suppress(NotFound):
            container.remove(force=True, v=True)
            removed = True
    if removed:
        logger.info("Browser: removed the browser container of session %s", session_id)
    return removed


def _file_tar(members: list[tuple[str, bytes | None, int, int]]) -> bytes:
    """A tar of ``(name, content or None for a directory, mode, uid)`` members, owned by uid:uid."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content, mode, uid in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.uid = info.gid = uid
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def extension_tar(archive: bytes, digest: str) -> bytes:
    """Repack the extension zip as a tar rooted at ``<digest>/``, refusing links and paths that escape it."""
    members: list[tuple[str, bytes | None, int, int]] = [(digest, None, 0o755, BROWSER_UID)]
    dirs = {digest}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                normalized = posixpath.normpath(name)
                if (
                    name.startswith("/")
                    or "\\" in name
                    or normalized in {".", ".."}
                    or normalized.startswith("../")
                    or stat.S_ISLNK(info.external_attr >> 16)
                ):
                    raise ExtensionInvalidError(f"extension archive has an unsafe member: {name!r}")
                total += info.file_size
                if total > _MAX_EXTENSION_BYTES:
                    raise ExtensionInvalidError("extension archive is too large")
                parts = normalized.split("/")
                for depth in range(1, len(parts)):
                    parent = posixpath.join(digest, *parts[:depth])
                    if parent not in dirs:
                        dirs.add(parent)
                        members.append((parent, None, 0o755, BROWSER_UID))
                members.append((posixpath.join(digest, normalized), zf.read(info), 0o644, BROWSER_UID))
    except zipfile.BadZipFile as exc:
        raise ExtensionInvalidError("extension archive is not a zip file") from exc
    if not any(name == f"{digest}/manifest.json" for name, *_ in members):
        raise ExtensionInvalidError("extension archive has no manifest.json")
    return _file_tar(members)


def _path_exists(container: Container, path: str) -> bool:
    try:
        bits, _stat = container.get_archive(path)
    except NotFound:
        return False
    for _chunk in bits:
        pass
    return True


def _read_single_file(container: Container, path: str) -> bytes:
    bits, _stat = container.get_archive(path)
    with tarfile.open(fileobj=io.BytesIO(b"".join(bits))) as tar:
        member = next(m for m in tar.getmembers() if m.isfile())
        extracted = tar.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"browser: {path} is not a regular file")
        return extracted.read()


class BrowserManager:
    """Starts, stops and removes session browsers, and owns their relay pumps and CDP tunnels."""

    def __init__(self) -> None:
        self._pumps: dict[str, RelayPump] = {}
        self._tunnels: dict[str, set[asyncio.Task]] = {}
        self._state: dict[str, BrowserStartResponse] = {}
        self._relay_binary: dict[str, bytes] = {}

    @property
    def client(self) -> DockerClient:
        return SandboxDockerSession._get_shared_client()

    def find(self, session_id: str) -> Container | None:
        return find_browser(self.client, session_id)

    # --- start ------------------------------------------------------------------------------------

    async def start(
        self, sandbox: Container, request: BrowserStartRequest, extension_archive: bytes | None
    ) -> BrowserStartResponse:
        session_id = sandbox.id
        existing = await asyncio.to_thread(self.find, session_id)
        if existing is not None and existing.status == "running":
            ws_path = await asyncio.to_thread(self._probe, existing)
            if ws_path is not None:
                if not self._pump_alive(session_id):
                    await self._restart_pump(sandbox, existing)
                known = self._state.get(session_id)
                return BrowserStartResponse(
                    started=False,
                    profile=known.profile if known else "restored",
                    restored_urls=known.restored_urls if known else [],
                    devtools_port=DEVTOOLS_PORT,
                    browser_ws_path=ws_path,
                )

        created = existing is None
        recreated = False
        if existing is None:
            recreated = await asyncio.to_thread(_path_exists, sandbox, SANDBOX_BROWSER_MARKER)
            browser = await asyncio.to_thread(self._create, sandbox, request)
        else:
            browser = existing
            await asyncio.to_thread(self._apply_limits, browser, request)

        try:
            await asyncio.to_thread(self._ensure_extension, browser, request.extension_sha256, extension_archive)
            ca_staged = await asyncio.to_thread(self._stage_ca, sandbox, browser)
            if browser.status != "running":
                await asyncio.to_thread(browser.start)
            await self._restart_pump(sandbox, browser)
            launch = await self._launch(browser, request.extension_sha256, ca_staged=ca_staged)
            await asyncio.to_thread(self._write_marker, sandbox)
        except BaseException:
            await self._stop_pump(session_id)
            if created:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(browser.remove, force=True, v=True)
            raise

        profile: Literal["empty", "restored", "reset"]
        if recreated or launch["tabs"] == "unreadable":
            profile = "reset"
        elif created:
            profile = "empty"
        else:
            profile = "restored"
        response = BrowserStartResponse(
            started=True,
            profile=profile,
            restored_urls=launch["restored_urls"],
            devtools_port=DEVTOOLS_PORT,
            browser_ws_path=launch["browser_ws_path"],
        )
        self._state[session_id] = response
        logger.info("Browser: started for session %s (profile %s)", session_id, profile)
        return response

    def _create(self, sandbox: Container, request: BrowserStartRequest) -> Container:
        labels = {DAIV_SANDBOX_TYPE_LABEL: TYPE_BROWSER, BROWSER_SESSION_LABEL: sandbox.id}
        kwargs: dict = {
            "image": settings.BROWSER_IMAGE,
            "detach": True,
            "init": True,
            "runtime": settings.RUNTIME,
            "user": BROWSER_USER,
            "mem_limit": request.memory_bytes,
            "memswap_limit": request.memory_bytes,
            "nano_cpus": int(request.cpus * 1e9),
            "labels": labels,
        }
        token = egress_token(sandbox)
        if token:
            labels[EGRESS_SESSION_LABEL] = token
            kwargs["network"] = EgressProxyManager._internal_network_name(token)
        else:
            kwargs["network_mode"] = "none"
        try:
            container = self.client.containers.create(**kwargs)
        except ImageNotFound as exc:
            raise BrowserImageUnavailableError(f"browser image {settings.BROWSER_IMAGE} is not available") from exc
        logger.info("Browser: created container %s for session %s", container.short_id, sandbox.id)
        return container

    def _apply_limits(self, browser: Container, request: BrowserStartRequest) -> None:
        host_config = browser.attrs.get("HostConfig", {})
        nano_cpus = int(request.cpus * 1e9)
        if host_config.get("Memory") == request.memory_bytes and host_config.get("NanoCpus") == nano_cpus:
            return
        # docker-py's update_container has no NanoCpus, and the limits were created as NanoCpus.
        api = self.client.api
        try:
            api._post_json(
                api._url("/containers/{0}/update", browser.id),
                data={"Memory": request.memory_bytes, "MemorySwap": request.memory_bytes, "NanoCpus": nano_cpus},
            ).raise_for_status()
        except Exception:
            logger.warning("Browser: could not apply new limits to container %s", browser.short_id, exc_info=True)

    def _ensure_extension(self, browser: Container, digest: str, archive: bytes | None) -> None:
        if _path_exists(browser, f"{EXTENSION_ROOT}/{digest}/manifest.json"):
            return
        if archive is None:
            raise ExtensionMissingError("extension missing")
        if hashlib.sha256(archive).hexdigest() != digest:
            raise ExtensionInvalidError("extension archive does not match extension_sha256")
        if not browser.put_archive(EXTENSION_ROOT, extension_tar(archive, digest)):
            raise RuntimeError(f"browser: failed to install the extension into {browser.short_id}")

    def _stage_ca(self, sandbox: Container, browser: Container) -> bool:
        if not (egress_token(sandbox) and settings.egress_enabled and settings.EGRESS_CA_CERT_FILE):
            return False
        cert = Path(settings.EGRESS_CA_CERT_FILE).read_bytes()
        relative = posixpath.relpath(BROWSER_CA_PATH, BROWSER_HOME)
        tar = _file_tar([(posixpath.dirname(relative), None, 0o755, BROWSER_UID), (relative, cert, 0o644, BROWSER_UID)])
        if not browser.put_archive(BROWSER_HOME, tar):
            raise RuntimeError(f"browser: failed to stage the egress CA into {browser.short_id}")
        return True

    def _relay_bytes(self, browser: Container) -> bytes:
        image_id = browser.attrs.get("Image", "")
        if image_id not in self._relay_binary:
            self._relay_binary[image_id] = _read_single_file(browser, IMAGE_RELAY_PATH)
        return self._relay_binary[image_id]

    def _install_relay(self, sandbox: Container, browser: Container) -> None:
        tar = _file_tar([
            (".daiv", None, 0o755, settings.RUN_UID),
            (".daiv/relay", self._relay_bytes(browser), 0o755, settings.RUN_UID),
        ])
        if not sandbox.put_archive(SANDBOX_HOME, tar):
            raise RuntimeError(f"browser: failed to copy daiv-relay into sandbox {sandbox.short_id}")

    def _write_marker(self, sandbox: Container) -> None:
        tar = _file_tar([(".daiv", None, 0o755, settings.RUN_UID), (".daiv/browser", b"", 0o644, settings.RUN_UID)])
        if not sandbox.put_archive(SANDBOX_HOME, tar):
            logger.warning("Browser: could not write the browser marker into sandbox %s", sandbox.short_id)

    def _egress_proxy_address(self, sandbox: Container) -> str | None:
        token = egress_token(sandbox)
        if not (token and settings.egress_enabled):
            return None
        ip = EgressProxyManager(self.client).proxy_internal_ip(token)
        return f"{ip}:{settings.EGRESS_PROXY_PORT}"

    def _probe(self, browser: Container) -> str | None:
        result = browser.exec_run(["python3", LAUNCH_SCRIPT, "--probe"], user=BROWSER_USER, demux=True)
        if result.exit_code != 0:
            return None
        stdout, _stderr = result.output
        try:
            return json.loads(stdout or b"")["browser_ws_path"]
        except ValueError, KeyError, TypeError:
            return None

    async def _launch(self, browser: Container, digest: str, *, ca_staged: bool) -> dict:
        timeout = settings.BROWSER_START_TIMEOUT_SECONDS
        cmd = ["python3", LAUNCH_SCRIPT, "--extension", f"{EXTENSION_ROOT}/{digest}", "--timeout", str(timeout)]
        if ca_staged:
            cmd += ["--ca", BROWSER_CA_PATH]
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(browser.exec_run, cmd, user=BROWSER_USER, demux=True), timeout=timeout + 10
            )
        except TimeoutError as exc:
            raise BrowserNotReadyError("the browser did not become ready in time") from exc
        stdout, stderr = result.output
        if result.exit_code == _LAUNCH_TIMEOUT_EXIT:
            logger.error("Browser: %s did not become ready: %s", browser.short_id, (stderr or b"").decode().strip())
            raise BrowserNotReadyError("the browser did not become ready in time")
        if result.exit_code != 0:
            raise RuntimeError(f"browser launch failed (exit {result.exit_code}): {(stderr or b'').decode().strip()}")
        try:
            return json.loads(stdout or b"")
        except ValueError as exc:
            raise RuntimeError("browser launch reported no DevTools endpoint") from exc

    # --- relay pump -------------------------------------------------------------------------------

    def _pump_alive(self, session_id: str) -> bool:
        pump = self._pumps.get(session_id)
        return pump is not None and pump.alive

    async def _restart_pump(self, sandbox: Container, browser: Container) -> None:
        await self._stop_pump(sandbox.id)
        await asyncio.to_thread(self._install_relay, sandbox, browser)
        proxy = await asyncio.to_thread(self._egress_proxy_address, sandbox)
        pump = RelayPump(
            self.client,
            sandbox_id=sandbox.id,
            browser_id=browser.id,
            relay_path=SANDBOX_RELAY_PATH,
            browser_user=BROWSER_USER,
        )
        await pump.start(egress_proxy=proxy)
        self._pumps[sandbox.id] = pump
        pump.on_exit(lambda: self._forget_pump(sandbox.id, pump))

    def _forget_pump(self, session_id: str, pump: RelayPump) -> None:
        if self._pumps.get(session_id) is pump:
            del self._pumps[session_id]

    async def _stop_pump(self, session_id: str) -> None:
        pump = self._pumps.pop(session_id, None)
        if pump is not None:
            await pump.stop()

    # --- CDP tunnels --------------------------------------------------------------------------------

    def track_tunnel(self, session_id: str, task: asyncio.Task) -> None:
        self._tunnels.setdefault(session_id, set()).add(task)
        task.add_done_callback(lambda done: self._tunnels.get(session_id, set()).discard(done))

    async def close_tunnels(self, session_id: str) -> None:
        tasks = self._tunnels.pop(session_id, set())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- stop and remove ----------------------------------------------------------------------------

    async def stop(self, session_id: str, *, force: bool = False) -> BrowserStopResponse:
        """Save the open tabs, close Chromium so its profile flushes, then stop (or, with force, remove) it."""
        await self.close_tunnels(session_id)
        self._state.pop(session_id, None)
        browser = await asyncio.to_thread(self.find, session_id)
        if browser is None:
            await self._stop_pump(session_id)
            return BrowserStopResponse()
        oom_killed = bool(browser.attrs.get("State", {}).get("OOMKilled"))
        saved_urls: list[str] = []
        if browser.status == "running" and not force:
            saved_urls = await self._save_and_close(browser)
        await self._stop_pump(session_id)
        try:
            if force:
                await asyncio.to_thread(browser.remove, force=True, v=True)
            else:
                await asyncio.to_thread(browser.stop, timeout=settings.STOP_TIMEOUT_SECONDS)
        except NotFound:
            pass
        if oom_killed:
            logger.error("Browser: the browser of session %s had been killed for memory", session_id)
        return BrowserStopResponse(saved_urls=saved_urls, oom_killed=oom_killed)

    async def remove(self, session_id: str) -> None:
        await self.close_tunnels(session_id)
        await self._stop_pump(session_id)
        self._state.pop(session_id, None)
        await asyncio.to_thread(remove_browser, self.client, session_id)

    async def _save_and_close(self, browser: Container) -> list[str]:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    browser.exec_run, ["python3", SAVE_TABS_SCRIPT, "--close"], user=BROWSER_USER, demux=True
                ),
                timeout=_SAVE_TABS_TIMEOUT_SECONDS,
            )
            stdout, stderr = result.output
            if stderr:
                logger.warning("Browser: closing %s: %s", browser.short_id, stderr.decode(errors="replace").strip())
            saved = json.loads(stdout or b"")["saved_urls"]
        except (TimeoutError, APIError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Browser: could not save the open tabs of %s: %s", browser.short_id, exc)
            return []
        return [url for url in saved if isinstance(url, str)]


async def probe_browser_image(app) -> asyncio.Task | None:
    """Set ``app.state.browser_available``: true once ``BROWSER_IMAGE`` is present, pulling it if missing."""
    app.state.browser_available = False
    image = settings.BROWSER_IMAGE
    try:
        client = SandboxDockerSession._get_shared_client()
        await asyncio.to_thread(client.images.get, image)
    except ImageNotFound:
        logger.warning("Browser: image %s is not present; pulling it, and reporting no browser until then", image)
        return asyncio.create_task(_pull_browser_image(app, client, image))
    except Exception as exc:
        logger.error("Browser: cannot inspect image %s, so the service reports no browser: %s", image, exc)
        return None
    app.state.browser_available = True
    return None


async def _pull_browser_image(app, client: DockerClient, image: str) -> None:
    try:
        await asyncio.to_thread(client.images.pull, image)
    except Exception as exc:
        logger.error("Browser: cannot pull image %s, so the service reports no browser: %s", image, exc)
        return
    app.state.browser_available = True
    logger.info("Browser: pulled image %s", image)
