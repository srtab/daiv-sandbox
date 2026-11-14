import asyncio
import hashlib
import io
import json
import stat
import tarfile
import zipfile
from pathlib import PurePosixPath
from unittest.mock import AsyncMock, Mock, patch

import pytest
from docker.errors import APIError, ImageNotFound, NotFound

from daiv_sandbox import browser as browser_module
from daiv_sandbox.browser import (
    BROWSER_CA_PATH,
    EXTENSION_ROOT,
    SANDBOX_BROWSER_MARKER,
    SANDBOX_RELAY_PATH,
    BrowserImageUnavailableError,
    BrowserManager,
    BrowserNotReadyError,
    ExtensionInvalidError,
    ExtensionMissingError,
    extension_tar,
    probe_browser_image,
)
from daiv_sandbox.config import settings
from daiv_sandbox.schemas import BrowserStartRequest

SANDBOX_ID = "a" * 64
WS_PATH = "/devtools/browser/4f1c"


def _zip(files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


EXTENSION = _zip({"manifest.json": b'{"name": "Stagehand Runtime"}', "js/service-worker.js": b"//"})
DIGEST = hashlib.sha256(EXTENSION).hexdigest()


def _request(**overrides) -> BrowserStartRequest:
    return BrowserStartRequest(**{"memory_bytes": 1 << 30, "cpus": 1.5, "extension_sha256": DIGEST, **overrides})


def _single_file_tar(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class FakeContainer:
    """Just enough of docker-py's Container: files for get/put_archive, and scripted exec results."""

    def __init__(self, cid, *, status="running", labels=None, files=None, attrs=None):
        self.id = cid
        self.short_id = cid[:12]
        self.status = status
        self.labels = labels or {}
        self.files = dict(files or {})
        self.attrs = attrs or {"State": {"OOMKilled": False}, "HostConfig": {}, "Image": "sha256:img"}
        self.puts: list[tuple[str, list[tarfile.TarInfo]]] = []
        self.exec_calls: list[tuple[list[str], str | None]] = []
        self.exec_results: dict[str, tuple[int, bytes, bytes]] = {}
        self.start = Mock(side_effect=lambda: setattr(self, "status", "running"))
        self.stop = Mock(side_effect=lambda **kw: setattr(self, "status", "exited"))
        self.remove = Mock()

    def get_archive(self, path):
        if path not in self.files:
            raise NotFound(path)
        return iter([_single_file_tar(PurePosixPath(path).name, self.files[path])]), {}

    def put_archive(self, path, data):
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = tar.getmembers()
            for member in members:
                if member.isfile():
                    self.files[f"{path}/{member.name}"] = tar.extractfile(member).read()
        self.puts.append((path, members))
        return True

    def exec_run(self, cmd, user=None, demux=False):
        self.exec_calls.append((cmd, user))
        key = PurePosixPath(cmd[1]).name + (":probe" if "--probe" in cmd else "")
        exit_code, stdout, stderr = self.exec_results.get(key, (3, b"", b""))
        return Mock(exit_code=exit_code, output=(stdout, stderr))


def _launch_ok(urls=(), tabs="none") -> tuple[int, bytes, bytes]:
    return 0, json.dumps({"browser_ws_path": WS_PATH, "restored_urls": list(urls), "tabs": tabs}).encode(), b""


@pytest.fixture
def client():
    client = Mock()
    client.containers.list.return_value = []
    with patch("daiv_sandbox.browser.SandboxDockerSession._get_shared_client", return_value=client):
        yield client


@pytest.fixture
def pump():
    with patch("daiv_sandbox.browser.RelayPump") as pump_cls:
        pump_cls.return_value.start = AsyncMock()
        pump_cls.return_value.stop = AsyncMock()
        pump_cls.return_value.alive = True
        yield pump_cls


def _sandbox(labels=None, files=None) -> FakeContainer:
    return FakeContainer(SANDBOX_ID, labels=labels, files=files)


def _new_browser(client, **kwargs) -> FakeContainer:
    browser = FakeContainer("b" * 64, status="created", files={"/usr/local/bin/daiv-relay": b"\x7fELF"}, **kwargs)
    browser.exec_results["launch.py"] = _launch_ok()
    client.containers.create.return_value = browser
    return browser


# --- creation ----------------------------------------------------------------------------------------


def test_create_runs_an_isolated_browser_under_its_own_limits(client):
    BrowserManager()._create(_sandbox(), _request())

    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["labels"] == {"daiv.sandbox.type": "browser", "daiv.sandbox.session": SANDBOX_ID}
    assert kwargs["nano_cpus"] == 1_500_000_000
    assert "cpus" not in kwargs
    assert kwargs["mem_limit"] == kwargs["memswap_limit"] == 1 << 30
    assert kwargs["network_mode"] == "none"
    assert "network" not in kwargs
    assert kwargs["user"] == "1000:1000"
    assert kwargs["runtime"] == settings.RUNTIME
    assert kwargs["image"] == settings.BROWSER_IMAGE


def test_create_joins_the_sessions_egress_network(client):
    BrowserManager()._create(_sandbox(labels={"daiv.sandbox.egress": "tok123"}), _request())

    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["network"] == "daiv-egress-tok123"
    assert kwargs["labels"]["daiv.sandbox.egress"] == "tok123"
    assert "network_mode" not in kwargs


def test_create_reports_a_missing_image_as_unavailable(client):
    client.containers.create.side_effect = ImageNotFound("gone")
    with pytest.raises(BrowserImageUnavailableError):
        BrowserManager()._create(_sandbox(), _request())


def test_nothing_is_staged_under_tmp():
    """runsc hides what put_archive writes under /tmp from the running container."""
    for path in (BROWSER_CA_PATH, SANDBOX_RELAY_PATH, SANDBOX_BROWSER_MARKER, EXTENSION_ROOT):
        assert not path.startswith("/tmp"), path  # noqa: S108


# --- the extension -------------------------------------------------------------------------------------


def test_extension_tar_roots_the_extension_at_its_digest_owned_by_the_browser_user():
    with tarfile.open(fileobj=io.BytesIO(extension_tar(EXTENSION, DIGEST))) as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert set(members) == {DIGEST, f"{DIGEST}/js", f"{DIGEST}/manifest.json", f"{DIGEST}/js/service-worker.js"}
    assert all(m.uid == 1000 and m.gid == 1000 for m in members.values())
    assert members[f"{DIGEST}/js"].isdir()


@pytest.mark.parametrize(
    "archive",
    [
        _zip({"manifest.json": b"{}", "../escape.js": b""}),
        _zip({"manifest.json": b"{}", "/etc/cron.d/x": b""}),
        _zip({"manifest.json": b"{}"}, symlink="link"),
        _zip({"service-worker.js": b""}),
        b"not a zip",
    ],
    ids=["dot-dot", "absolute", "symlink", "no-manifest", "not-a-zip"],
)
def test_extension_tar_refuses_unsafe_or_broken_archives(archive):
    with pytest.raises(ExtensionInvalidError):
        extension_tar(archive, DIGEST)


def test_ensure_extension_skips_an_installed_digest():
    browser = FakeContainer("b", files={f"{EXTENSION_ROOT}/{DIGEST}/manifest.json": b"{}"})
    BrowserManager()._ensure_extension(browser, DIGEST, None)
    assert browser.puts == []


def test_ensure_extension_needs_the_archive_when_the_digest_is_not_cached():
    with pytest.raises(ExtensionMissingError):
        BrowserManager()._ensure_extension(FakeContainer("b"), DIGEST, None)


def test_ensure_extension_refuses_an_archive_that_does_not_match_its_digest():
    with pytest.raises(ExtensionInvalidError):
        BrowserManager()._ensure_extension(FakeContainer("b"), "0" * 64, EXTENSION)


def test_ensure_extension_installs_under_the_extension_root():
    browser = FakeContainer("b")
    BrowserManager()._ensure_extension(browser, DIGEST, EXTENSION)
    assert browser.puts[0][0] == EXTENSION_ROOT
    assert f"{EXTENSION_ROOT}/{DIGEST}/manifest.json" in browser.files


# --- start ------------------------------------------------------------------------------------------------


async def test_first_start_creates_an_empty_browser_and_relays_to_the_sandbox(client, pump):
    sandbox = _sandbox()
    browser = _new_browser(client)
    manager = BrowserManager()

    response = await manager.start(sandbox, _request(), EXTENSION)

    assert response.model_dump() == {
        "started": True,
        "profile": "empty",
        "restored_urls": [],
        "devtools_port": 39222,
        "browser_ws_path": WS_PATH,
    }
    browser.start.assert_called_once()
    assert sandbox.files[SANDBOX_RELAY_PATH] == b"\x7fELF"
    relay = next(m for path, members in sandbox.puts for m in members if m.name == ".daiv/relay")
    assert (relay.mode, relay.uid) == (0o755, settings.RUN_UID)
    assert SANDBOX_BROWSER_MARKER in sandbox.files
    pump.return_value.start.assert_awaited_once_with(egress_proxy=None)
    launch_cmd = next(cmd for cmd, _ in browser.exec_calls if cmd[1].endswith("launch.py") and "--probe" not in cmd)
    assert launch_cmd[launch_cmd.index("--extension") + 1] == f"{EXTENSION_ROOT}/{DIGEST}"
    assert "--ca" not in launch_cmd


async def test_a_missing_browser_whose_session_had_one_starts_reset(client, pump):
    sandbox = _sandbox(files={SANDBOX_BROWSER_MARKER: b""})
    _new_browser(client)

    response = await BrowserManager().start(sandbox, _request(), EXTENSION)

    assert response.profile == "reset"


async def test_a_stopped_browser_restarts_from_its_profile(client, pump):
    browser = _new_browser(client)
    browser.status = "exited"
    browser.files[f"{EXTENSION_ROOT}/{DIGEST}/manifest.json"] = b"{}"
    browser.exec_results["launch.py"] = _launch_ok(["http://localhost:5173/login"], tabs="ok")
    client.containers.list.return_value = [browser]

    response = await BrowserManager().start(_sandbox(), _request(), None)

    assert response.profile == "restored"
    assert response.restored_urls == ["http://localhost:5173/login"]
    client.containers.create.assert_not_called()
    browser.start.assert_called_once()


async def test_an_unreadable_tab_file_reports_the_profile_reset(client, pump):
    browser = _new_browser(client)
    browser.status = "exited"
    browser.exec_results["launch.py"] = _launch_ok(tabs="unreadable")
    client.containers.list.return_value = [browser]

    response = await BrowserManager().start(_sandbox(), _request(), EXTENSION)

    assert response.profile == "reset"


async def test_start_is_idempotent_while_the_browser_runs(client, pump):
    browser = _new_browser(client)
    client.containers.list.return_value = [browser]
    manager = BrowserManager()
    browser.status = "exited"
    browser.exec_results["launch.py"] = _launch_ok(["http://localhost:3000/"], tabs="ok")
    await manager.start(_sandbox(), _request(), EXTENSION)
    browser.exec_results["launch.py:probe"] = _launch_ok(tabs="running")
    launches = len(browser.exec_calls)

    response = await manager.start(_sandbox(), _request(), None)

    assert response.started is False
    assert response.profile == "restored"
    assert response.restored_urls == ["http://localhost:3000/"]
    assert [cmd for cmd, _ in browser.exec_calls[launches:]] == [["python3", "/opt/daiv/launch.py", "--probe"]]


async def test_a_running_container_whose_chromium_died_is_relaunched(client, pump):
    browser = _new_browser(client)
    browser.status = "running"
    client.containers.list.return_value = [browser]

    response = await BrowserManager().start(_sandbox(), _request(), EXTENSION)

    assert response.started is True
    browser.start.assert_not_called()


async def test_an_egress_session_stages_the_ca_and_relays_through_its_proxy(client, pump, tmp_path, monkeypatch):
    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"CERT")
    monkeypatch.setattr(settings, "EGRESS_CA_CERT_FILE", str(ca))
    monkeypatch.setattr(settings, "EGRESS_CA_KEY_FILE", str(ca))
    browser = _new_browser(client)
    with patch("daiv_sandbox.browser.EgressProxyManager") as mgr_cls:
        mgr_cls._internal_network_name.return_value = "daiv-egress-tok123"
        mgr_cls.return_value.proxy_internal_ip.return_value = "10.0.0.2"
        await BrowserManager().start(_sandbox(labels={"daiv.sandbox.egress": "tok123"}), _request(), EXTENSION)

    assert browser.files[BROWSER_CA_PATH] == b"CERT"
    pump.return_value.start.assert_awaited_once_with(egress_proxy=f"10.0.0.2:{settings.EGRESS_PROXY_PORT}")
    launch_cmd = next(cmd for cmd, _ in browser.exec_calls if "--extension" in cmd)
    assert launch_cmd[launch_cmd.index("--ca") + 1] == BROWSER_CA_PATH


async def test_a_launch_timeout_is_not_ready_and_a_new_container_is_removed(client, pump):
    sandbox = _sandbox()
    browser = _new_browser(client)
    browser.exec_results["launch.py"] = (5, b"", b"Chromium did not open its DevTools port in time")

    with pytest.raises(BrowserNotReadyError):
        await BrowserManager().start(sandbox, _request(), EXTENSION)

    browser.remove.assert_called_once_with(force=True, v=True)
    pump.return_value.stop.assert_awaited()
    assert SANDBOX_BROWSER_MARKER not in sandbox.files


async def test_a_failed_restart_keeps_the_existing_container_and_its_profile(client, pump):
    browser = _new_browser(client)
    browser.status = "exited"
    browser.exec_results["launch.py"] = (1, b"", b"boom")
    client.containers.list.return_value = [browser]

    with pytest.raises(RuntimeError):
        await BrowserManager().start(_sandbox(), _request(), EXTENSION)

    browser.remove.assert_not_called()


def test_new_limits_are_applied_to_an_existing_browser(client):
    browser = FakeContainer("b", attrs={"HostConfig": {"Memory": 1, "NanoCpus": 1}, "State": {}})
    BrowserManager()._apply_limits(browser, _request(memory_bytes=512 << 20, cpus=0.5))
    data = client.api._post_json.call_args.kwargs["data"]
    assert data == {"Memory": 512 << 20, "MemorySwap": 512 << 20, "NanoCpus": 500_000_000}


def test_unchanged_limits_are_left_alone(client):
    browser = FakeContainer("b", attrs={"HostConfig": {"Memory": 1 << 30, "NanoCpus": 1_500_000_000}, "State": {}})
    BrowserManager()._apply_limits(browser, _request())
    client.api._post_json.assert_not_called()


# --- stop and remove -----------------------------------------------------------------------------------------


async def test_stop_saves_the_tabs_then_stops_the_container_keeping_it(client, pump):
    browser = FakeContainer("b")
    browser.exec_results["save_tabs.py"] = (0, json.dumps({"saved_urls": ["http://localhost:5173/"]}).encode(), b"")
    client.containers.list.return_value = [browser]

    response = await BrowserManager().stop(SANDBOX_ID)

    assert response.model_dump() == {"saved_urls": ["http://localhost:5173/"], "oom_killed": False}
    assert browser.exec_calls[0] == (["python3", "/opt/daiv/save_tabs.py", "--close"], "1000:1000")
    browser.stop.assert_called_once_with(timeout=settings.STOP_TIMEOUT_SECONDS)
    browser.remove.assert_not_called()


async def test_stop_reports_a_browser_killed_for_memory(client, pump):
    browser = FakeContainer("b", status="exited", attrs={"State": {"OOMKilled": True}, "HostConfig": {}})
    client.containers.list.return_value = [browser]

    response = await BrowserManager().stop(SANDBOX_ID)

    assert response.oom_killed is True
    assert response.saved_urls == []
    assert browser.exec_calls == []


async def test_stop_with_force_removes_the_container_and_its_profile(client, pump):
    browser = FakeContainer("b")
    client.containers.list.return_value = [browser]

    await BrowserManager().stop(SANDBOX_ID, force=True)

    browser.remove.assert_called_once_with(force=True, v=True)
    assert browser.exec_calls == []


async def test_stop_of_a_session_without_a_browser_answers_empty(client, pump):
    response = await BrowserManager().stop(SANDBOX_ID)
    assert response.model_dump() == {"saved_urls": [], "oom_killed": False}


async def test_a_failed_tab_save_still_stops_the_browser(client, pump):
    browser = FakeContainer("b")
    browser.exec_results["save_tabs.py"] = (0, b"garbage", b"")
    client.containers.list.return_value = [browser]

    response = await BrowserManager().stop(SANDBOX_ID)

    assert response.saved_urls == []
    browser.stop.assert_called_once()


async def test_stop_and_remove_close_the_sessions_cdp_tunnels(client, pump):
    manager = BrowserManager()
    for close in (manager.stop, manager.remove):
        tunnel = asyncio.create_task(asyncio.sleep(3600))
        manager.track_tunnel(SANDBOX_ID, tunnel)
        await close(SANDBOX_ID)
        assert tunnel.cancelled()


async def test_remove_stops_the_relay_pump_and_removes_the_container(client, pump):
    browser = _new_browser(client)
    manager = BrowserManager()
    await manager.start(_sandbox(), _request(), EXTENSION)
    client.containers.list.return_value = [browser]

    await manager.remove(SANDBOX_ID)

    pump.return_value.stop.assert_awaited()
    browser.remove.assert_called_with(force=True, v=True)


# --- capability probe -----------------------------------------------------------------------------------------


async def test_probe_reports_the_browser_when_the_image_is_present(client):
    app = Mock()
    assert await probe_browser_image(app) is None
    assert app.state.browser_available is True


async def test_probe_pulls_a_missing_image_and_reports_the_browser_once_pulled(client):
    app = Mock()
    client.images.get.side_effect = ImageNotFound("missing")
    task = await probe_browser_image(app)
    assert app.state.browser_available is False
    await task
    assert app.state.browser_available is True
    client.images.pull.assert_called_once_with(settings.BROWSER_IMAGE)


async def test_probe_reports_no_browser_when_the_pull_fails(client, caplog):
    app = Mock()
    client.images.get.side_effect = ImageNotFound("missing")
    client.images.pull.side_effect = APIError("denied")
    await (await probe_browser_image(app))
    assert app.state.browser_available is False
    assert "cannot pull image" in caplog.text


async def test_probe_reports_no_browser_when_docker_is_unreachable(caplog):
    app = Mock()
    with patch.object(browser_module.SandboxDockerSession, "_get_shared_client", side_effect=RuntimeError("no daemon")):
        assert await probe_browser_image(app) is None
    assert app.state.browser_available is False
    assert "cannot inspect image" in caplog.text


async def test_a_pump_that_exits_on_its_own_is_forgotten(client, pump):
    exit_callbacks = []
    pump.return_value.on_exit = Mock(side_effect=exit_callbacks.append)
    _new_browser(client)
    manager = BrowserManager()
    await manager.start(_sandbox(), _request(), EXTENSION)
    assert manager._pump_alive(SANDBOX_ID)

    exit_callbacks[0]()

    assert SANDBOX_ID not in manager._pumps
