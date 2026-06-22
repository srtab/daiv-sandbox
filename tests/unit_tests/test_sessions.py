import io
import tarfile
from unittest.mock import ANY, MagicMock, Mock, patch

import pytest
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import ExecResult
from docker.models.images import Image

from daiv_sandbox.config import settings
from daiv_sandbox.sessions import (
    _GREP_BAD_PATTERN_EXIT,
    _PATH_ABSENT_EXIT,
    _PATH_DENIED_EXIT,
    _PATH_WRONG_TYPE_EXIT,
    PIPEFAIL_WRAPPER,
    SANDBOX_HOME,
    SANDBOX_ROOT,
    SCRATCH_ROOT,
    SKILLS_ROOT,
    WORKSPACE_ROOT,
    SandboxDockerSession,
    SessionUnavailableError,
    _build_single_file_tar_stream,
    _sanitize_archive_stream,
    _sh_quote,
    _validate_sandbox_path,
)

EXPECTED_EXEC_ENV = {
    "HOME": SANDBOX_HOME,
    "XDG_CACHE_HOME": f"{SANDBOX_HOME}/.cache",
    "XDG_CONFIG_HOME": f"{SANDBOX_HOME}/.config",
    "XDG_STATE_HOME": f"{SANDBOX_HOME}/.local/state",
    "XDG_DATA_HOME": f"{SANDBOX_HOME}/.local/share",
}


@pytest.fixture
def mock_image():
    return Image({"id": "test-image", "RepoTags": ["test-image"]})


@pytest.fixture
def mock_docker_client(mock_image):
    # Reset the shared client singleton so each test gets a fresh mock.
    SandboxDockerSession._shared_client = None
    with patch("daiv_sandbox.sessions.from_env") as mock_from_env:
        mock_client = MagicMock(
            images=MagicMock(
                build=MagicMock(return_value=(mock_image, None)),
                get=MagicMock(return_value=mock_image),
                pull=MagicMock(return_value=mock_image),
                ping=MagicMock(return_value=True),
            )
        )
        mock_from_env.return_value = mock_client

        yield mock_client

    SandboxDockerSession._shared_client = None


def test_ping(mock_docker_client):
    with patch.object(SandboxDockerSession, "_ping", return_value=True) as mock_ping:
        assert SandboxDockerSession.ping() is True
        mock_ping.assert_called_once()


def test_start_with_image(mock_docker_client, mock_image):
    with (
        patch.object(SandboxDockerSession, "_pull_image") as mock_pull_image,
        patch.object(SandboxDockerSession, "_start_container") as mock_start_container,
    ):
        SandboxDockerSession.start(image="test-image")
        mock_pull_image.assert_called_once_with("test-image")
        mock_start_container.assert_called_once_with("test-image")


def test__pull_image_with_image_not_found(mock_docker_client):
    mock_docker_client.images.get.side_effect = ImageNotFound("test-image")
    session = SandboxDockerSession()
    session._pull_image("test-image")
    mock_docker_client.images.pull.assert_called_once_with("test-image")


def test__pull_image_with_image_found(mock_docker_client):
    session = SandboxDockerSession()
    session._pull_image("test-image")
    mock_docker_client.images.get.assert_called_once_with("test-image")


def test__start_container(mock_docker_client):
    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session._start_container("test-image")
    mock_docker_client.containers.run.assert_called_once_with(
        "test-image",
        entrypoint="/bin/sh",
        command=["-lc", "sleep infinity"],
        detach=True,
        tty=True,
        runtime=settings.RUNTIME,
        user=f"{settings.RUN_UID}:{settings.RUN_GID}",
    )
    assert session.container is not None
    assert session.container.id == mock_docker_client.containers.run.return_value.id
    assert session.session_id == mock_docker_client.containers.run.return_value.id
    # Should create sandbox directories and chown them
    mock_container.exec_run.assert_any_call(
        ["mkdir", "-p", "--", WORKSPACE_ROOT, SANDBOX_ROOT, SANDBOX_HOME, SKILLS_ROOT, SCRATCH_ROOT], user="root"
    )
    mock_container.exec_run.assert_any_call(
        [
            "chown",
            f"{settings.RUN_UID}:{settings.RUN_GID}",
            "--",
            WORKSPACE_ROOT,
            SANDBOX_ROOT,
            SANDBOX_HOME,
            SKILLS_ROOT,
            SCRATCH_ROOT,
        ],
        user="root",
    )


def test_start_container_force_removes_on_bootstrap_failure(mock_docker_client):
    """If mkdir/chown fails after the container starts, force-remove it.

    The container runs `sleep infinity` with no auto-remove, so it stays alive on failure;
    `_start_container` must force-remove it so a failed start() leaks nothing.
    """
    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=1, output=b"boom")

    with pytest.raises(RuntimeError):
        session._start_container("img:latest")

    mock_container.remove.assert_called_once_with(force=True)


def _resolv_exec_cmd(nameservers):
    """Mirror _override_resolv_conf's exec command so assertions don't hardcode shell quoting."""
    content = "".join(f"nameserver {ns}\n" for ns in nameservers)
    return ["sh", "-c", f"printf '%s' {_sh_quote(content)} > /etc/resolv.conf"]


def test_start_container_fixes_gvisor_dns_on_custom_network(mock_docker_client, monkeypatch):
    """Under runsc on a user-defined network, gVisor can't reach Docker's embedded resolver
    (127.0.0.11): inject EXTRA_HOSTS as static /etc/hosts entries and repoint resolv.conf at DNS."""
    monkeypatch.setattr(settings, "RUNTIME", "runsc")
    monkeypatch.setattr(settings, "DNS", ["1.1.1.1", "8.8.8.8"])
    monkeypatch.setattr(settings, "EXTRA_HOSTS", ["gitlab"])
    monkeypatch.setattr("daiv_sandbox.sessions.socket.gethostbyname", lambda name: "172.19.0.3")

    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=0, output=b"")

    session._start_container("img", network="daiv-net")

    run_kwargs = mock_docker_client.containers.run.call_args.kwargs
    assert run_kwargs["network"] == "daiv-net"
    assert run_kwargs["extra_hosts"] == {"gitlab": "172.19.0.3"}
    mock_container.exec_run.assert_any_call(_resolv_exec_cmd(["1.1.1.1", "8.8.8.8"]), user="root")


def test_start_container_overrides_resolv_conf_without_extra_hosts(mock_docker_client, monkeypatch):
    """resolv.conf is repointed even when EXTRA_HOSTS is empty; no extra_hosts kwarg is added."""
    monkeypatch.setattr(settings, "RUNTIME", "runsc")
    monkeypatch.setattr(settings, "DNS", ["9.9.9.9"])
    monkeypatch.setattr(settings, "EXTRA_HOSTS", [])

    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=0, output=b"")

    session._start_container("img", network="daiv-net")

    assert "extra_hosts" not in mock_docker_client.containers.run.call_args.kwargs
    mock_container.exec_run.assert_any_call(_resolv_exec_cmd(["9.9.9.9"]), user="root")


def test_start_container_skips_dns_fix_under_runc(mock_docker_client, monkeypatch):
    """runc honours Docker's embedded resolver, so the gVisor workaround must not fire (no
    resolv.conf rewrite, no extra_hosts) — otherwise we'd break working service-name DNS."""
    monkeypatch.setattr(settings, "RUNTIME", "runc")
    monkeypatch.setattr(settings, "EXTRA_HOSTS", ["gitlab"])

    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=0, output=b"")

    session._start_container("img", network="daiv-net")

    assert "extra_hosts" not in mock_docker_client.containers.run.call_args.kwargs
    assert not any("resolv.conf" in str(call) for call in mock_container.exec_run.call_args_list)


def test_start_container_skips_dns_fix_without_custom_network(mock_docker_client, monkeypatch):
    """Without an explicit network (Docker's default bridge) resolv.conf already carries real
    upstreams under gVisor, so the workaround must not fire."""
    monkeypatch.setattr(settings, "RUNTIME", "runsc")
    monkeypatch.setattr(settings, "EXTRA_HOSTS", ["gitlab"])

    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=0, output=b"")

    session._start_container("img")

    assert "extra_hosts" not in mock_docker_client.containers.run.call_args.kwargs
    assert not any("resolv.conf" in str(call) for call in mock_container.exec_run.call_args_list)


def test_start_container_force_removes_when_resolv_conf_override_fails(mock_docker_client, monkeypatch):
    """A failed resolv.conf rewrite is a bootstrap failure: surface it and leak no container."""
    monkeypatch.setattr(settings, "RUNTIME", "runsc")
    monkeypatch.setattr(settings, "DNS", ["1.1.1.1"])
    monkeypatch.setattr(settings, "EXTRA_HOSTS", [])

    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value

    def exec_run(cmd, **_):
        if "resolv.conf" in cmd[-1]:
            return ExecResult(exit_code=1, output=b"boom")
        return ExecResult(exit_code=0, output=b"")

    mock_container.exec_run.side_effect = exec_run

    with pytest.raises(RuntimeError, match="resolv.conf"):
        session._start_container("img", network="daiv-net")

    mock_container.remove.assert_called_once_with(force=True)


def test_resolve_extra_hosts_skips_unresolvable(mock_docker_client, monkeypatch):
    """Sibling names that don't resolve are dropped (and logged), not fatal to session start."""

    def gethostbyname(name):
        if name == "gitlab":
            return "172.19.0.3"
        raise OSError("name or service not known")

    monkeypatch.setattr("daiv_sandbox.sessions.socket.gethostbyname", gethostbyname)
    session = SandboxDockerSession()

    assert session._resolve_extra_hosts(["gitlab", "ghost"]) == {"gitlab": "172.19.0.3"}


def test_remove_container(mock_docker_client):
    session = SandboxDockerSession(session_id="test-session-id")
    session.remove_container()
    mock_docker_client.containers.get.assert_called_with(session.session_id)
    mock_docker_client.containers.get.return_value.remove.assert_called_once_with(force=True)


def test_remove_container_with_container_not_found(mock_docker_client):
    session = SandboxDockerSession(session_id="test-session-id")
    mock_docker_client.containers.get.side_effect = NotFound(session.session_id)
    session.remove_container()
    mock_docker_client.containers.get.assert_called_with(session.session_id)
    mock_docker_client.containers.get.return_value.remove.assert_not_called()


def test_session_type_label_constants():
    from daiv_sandbox.sessions import DAIV_SANDBOX_TYPE_LABEL, TYPE_CMD_EXECUTOR

    assert DAIV_SANDBOX_TYPE_LABEL == "daiv.sandbox.type"
    assert TYPE_CMD_EXECUTOR == "cmd_executor"


def test_stop_container(mock_docker_client):
    from daiv_sandbox.config import settings as cfg

    session = SandboxDockerSession(session_id="test-session-id")
    session.stop_container()
    mock_docker_client.containers.get.assert_called_with("test-session-id")
    mock_docker_client.containers.get.return_value.stop.assert_called_once_with(timeout=cfg.STOP_TIMEOUT_SECONDS)


def test_stop_container_with_container_not_found(mock_docker_client):
    session = SandboxDockerSession(session_id="test-session-id")
    mock_docker_client.containers.get.side_effect = NotFound(session.session_id)
    session.stop_container()  # must not raise
    mock_docker_client.containers.get.return_value.stop.assert_not_called()


def test_stop_container_vanished_before_stop_is_noop(mock_docker_client):
    """A container removed between the lookup and the stop counts as already-stopped (no raise)."""
    session = SandboxDockerSession(session_id="test-session-id")
    mock_docker_client.containers.get.return_value.stop.side_effect = NotFound("gone")
    session.stop_container()  # must not raise


def test_stop_container_raises_session_unavailable_on_api_error(mock_docker_client):
    """A Docker API fault on stop surfaces as SessionUnavailableError (mapped to 503), not a bare
    500 — the session may still be running and the client must be able to tell."""
    session = SandboxDockerSession(session_id="test-session-id")
    mock_docker_client.containers.get.return_value.stop.side_effect = APIError("daemon busy")
    with pytest.raises(SessionUnavailableError):
        session.stop_container()


def _bare_session(client):
    """A session whose __init__ is bypassed so _get_container can be driven against *client*."""
    s = SandboxDockerSession.__new__(SandboxDockerSession)
    s.client = client
    s.session_id = "sid"
    s.container = None
    return s


def _session_with_container():
    """A fully-constructed session (mock_docker_client patches from_env) with a mock container."""
    s = SandboxDockerSession()
    s.container = MagicMock()
    return s


def test_get_container_returns_running_container():
    container = Mock(status="running")
    client = Mock()
    client.containers.get.return_value = container
    assert _bare_session(client)._get_container("sid") is container
    container.restart.assert_not_called()


def test_get_container_restarts_stopped_container():
    """A stopped container is restarted and reloaded; once running it is returned (warm reuse)."""
    container = Mock(status="exited")

    def _warm():
        container.status = "running"

    container.reload.side_effect = _warm
    client = Mock()
    client.containers.get.return_value = container

    result = _bare_session(client)._get_container("sid")

    container.restart.assert_called_once()
    container.reload.assert_called_once()
    assert result is container


def test_get_container_returns_none_when_missing():
    client = Mock()
    client.containers.get.side_effect = NotFound("nope")
    assert _bare_session(client)._get_container("sid") is None


def test_get_container_returns_none_when_restart_does_not_take():
    """A restart that does not raise but leaves the container not-running is a benign 404, not a
    503 — keep returning None."""
    container = Mock(status="exited")  # reload leaves it stopped
    client = Mock()
    client.containers.get.return_value = container
    assert _bare_session(client)._get_container("sid") is None


def test_get_container_returns_none_when_vanishes_during_restart():
    container = Mock(status="exited")
    container.restart.side_effect = NotFound("gone")
    client = Mock()
    client.containers.get.return_value = container
    assert _bare_session(client)._get_container("sid") is None


def test_get_container_raises_session_unavailable_on_restart_fault():
    """A Docker fault while restarting is infrastructure, not a missing session: raise so the
    endpoint returns 503 instead of masking it as a 404."""
    container = Mock(status="exited")
    container.restart.side_effect = APIError("daemon down")
    client = Mock()
    client.containers.get.return_value = container
    with pytest.raises(SessionUnavailableError):
        _bare_session(client)._get_container("sid")


def test_copy_to_runtime_creates_directory(mock_docker_client):
    import io
    import tarfile

    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session.container.put_archive.return_value = True
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("a.txt")
        ti.size = 0
        tf.addfile(ti, io.BytesIO(b""))
    session.copy_to_container(buf)
    # Clears (rm), then creates the dest as the sandbox user (so a fresh dir needs no chown), then
    # ships the sanitized archive. No recursive chmod/chown: the sanitizer stamps uid/gid/mode and
    # put_archive preserves them.
    session.container.exec_run.assert_any_call(["/bin/sh", "-c", ANY], user="root")
    session.container.exec_run.assert_any_call(
        ["mkdir", "-p", "--", SANDBOX_ROOT], user=f"{settings.RUN_UID}:{settings.RUN_GID}"
    )
    assert session.container.put_archive.called
    verbs = [c.args[0][0] for c in session.container.exec_run.call_args_list if c.args and c.args[0]]
    assert "chmod" not in verbs and "chown" not in verbs


def test_install_ca_cert_ships_cert_and_updates_store(mock_docker_client):
    from daiv_sandbox.sessions import SANDBOX_CA_PATH

    s = _session_with_container()
    s.container.put_archive = Mock(return_value=True)
    s.container.exec_run = Mock(return_value=ExecResult(exit_code=0, output=b""))

    s.install_ca_cert(b"-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")

    # cert shipped to /usr/local/share/ca-certificates as root
    parent = SANDBOX_CA_PATH.rsplit("/", 1)[0]
    assert s.container.put_archive.call_args.args[0] == parent
    # update-ca-certificates run as root
    assert any(
        c.args[0][:1] == ["update-ca-certificates"] and c.kwargs.get("user") == "root"
        for c in s.container.exec_run.call_args_list
    )


def test_install_ca_cert_fails_closed_when_mkdir_fails(mock_docker_client):
    s = _session_with_container()
    s.container.exec_run = Mock(return_value=ExecResult(exit_code=1, output=b"mkdir: permission denied"))
    with pytest.raises(RuntimeError, match="CA dir"):
        s.install_ca_cert(b"cert")


def test_install_ca_cert_fails_closed_when_put_archive_fails(mock_docker_client):
    s = _session_with_container()
    s.container.put_archive = Mock(return_value=False)
    s.container.exec_run = Mock(return_value=ExecResult(exit_code=0, output=b""))
    with pytest.raises(RuntimeError, match="copy CA cert"):
        s.install_ca_cert(b"cert")


def test_install_ca_cert_fails_closed_when_update_fails(mock_docker_client):
    s = _session_with_container()
    s.container.put_archive = Mock(return_value=True)
    s.container.exec_run = Mock(
        side_effect=[
            ExecResult(exit_code=0, output=b""),  # mkdir succeeds
            ExecResult(exit_code=1, output=b"update-ca-certificates: not found"),  # update fails
        ]
    )
    with pytest.raises(RuntimeError, match="update-ca-certificates"):
        s.install_ca_cert(b"cert")


def test_execute_command(mock_docker_client):
    session = SandboxDockerSession(session_id="test-session-id")
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"output")
    result = session.execute_command("echo hello")
    assert result.exit_code == 0
    assert result.output == "output"
    # Should use SANDBOX_ROOT by default
    session.container.exec_run.assert_called_once_with(
        ["/bin/sh", "-c", PIPEFAIL_WRAPPER, "--", "echo hello"],
        workdir=SANDBOX_ROOT,
        user=f"{settings.RUN_UID}:{settings.RUN_GID}",
        environment=EXPECTED_EXEC_ENV,
    )


def test_execute_command_pipefail_propagates_exit_code(mock_docker_client):
    """A pipeline where the first command fails should return a non-zero exit code."""
    session = SandboxDockerSession(session_id="test-session-id")
    session.container = MagicMock()
    # Simulate the shell returning exit code 1 because pipefail is active and
    # the first stage of the pipeline failed (e.g. `false | true`).
    session.container.exec_run.return_value = ExecResult(exit_code=1, output=b"")
    result = session.execute_command("false | true")
    assert result.exit_code == 1
    session.container.exec_run.assert_called_once_with(
        ["/bin/sh", "-c", PIPEFAIL_WRAPPER, "--", "false | true"],
        workdir=SANDBOX_ROOT,
        user=f"{settings.RUN_UID}:{settings.RUN_GID}",
        environment=EXPECTED_EXEC_ENV,
    )


def test_copy_to_container_with_relative_dest(mock_docker_client):
    """Test that relative dest paths are resolved under SANDBOX_ROOT"""
    import io
    import tarfile

    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session.container.put_archive.return_value = True
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("a.txt")
        ti.size = 0
        tf.addfile(ti, io.BytesIO(b""))
    session.copy_to_container(buf, dest="subdir")
    expected_path = f"{SANDBOX_ROOT}/subdir"
    session.container.exec_run.assert_any_call(["/bin/sh", "-c", ANY], user="root")
    session.container.exec_run.assert_any_call(
        ["mkdir", "-p", "--", expected_path], user=f"{settings.RUN_UID}:{settings.RUN_GID}"
    )
    verbs = [c.args[0][0] for c in session.container.exec_run.call_args_list if c.args and c.args[0]]
    assert "chmod" not in verbs and "chown" not in verbs


def test_execute_command_with_relative_workdir(mock_docker_client):
    """Test that relative workdir is resolved under SANDBOX_ROOT"""
    session = SandboxDockerSession(session_id="test-session-id")
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"output")
    result = session.execute_command("echo hello", workdir="subdir")
    assert result.exit_code == 0
    expected_workdir = f"{SANDBOX_ROOT}/subdir"
    session.container.exec_run.assert_called_once_with(
        ["/bin/sh", "-c", PIPEFAIL_WRAPPER, "--", "echo hello"],
        workdir=expected_workdir,
        user=f"{settings.RUN_UID}:{settings.RUN_GID}",
        environment=EXPECTED_EXEC_ENV,
    )


def test_execute_command_with_absolute_workdir(mock_docker_client):
    """Test that absolute workdir is used as-is"""
    session = SandboxDockerSession(session_id="test-session-id")
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"output")
    result = session.execute_command("echo hello", workdir="/custom/path")
    assert result.exit_code == 0
    session.container.exec_run.assert_called_once_with(
        ["/bin/sh", "-c", PIPEFAIL_WRAPPER, "--", "echo hello"],
        workdir="/custom/path",
        user=f"{settings.RUN_UID}:{settings.RUN_GID}",
        environment=EXPECTED_EXEC_ENV,
    )


def test_get_exec_environment(mock_docker_client):
    session = SandboxDockerSession()
    assert session._get_exec_environment() == EXPECTED_EXEC_ENV


def _sanitize_tar(build) -> tuple[tarfile.TarFile, int]:
    """Build an input tar via *build(tf)*, run the sanitizer, return (output TarFile, skip count)."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w") as tf:
        build(tf)
    in_buf.seek(0)
    out_buf = io.BytesIO()
    skipped = _sanitize_archive_stream(in_buf, out_buf, uid=1000, gid=1000)
    out_buf.seek(0)
    return tarfile.open(fileobj=out_buf), skipped


def _add_symlink(tf: tarfile.TarFile, name: str, target: str) -> None:
    sym = tarfile.TarInfo(name=name)
    sym.type = tarfile.SYMTYPE
    sym.linkname = target
    tf.addfile(sym)


def _add_file(tf: tarfile.TarFile, name: str, content: bytes = b"hello") -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(content)
    tf.addfile(info, io.BytesIO(content))


def _add_dir(tf: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.DIRTYPE
    tf.addfile(info)


def test_sanitize_archive_stream_preserves_in_tree_symlinks():
    """Relative symlinks resolving inside the archive root survive sanitization intact.

    Repos legitimately track symlinks; dropping them leaves the seeded git tree dirty
    (spurious symlink deletions polluting every captured diff). The ``a/b/link`` vector
    is the common real-repo shape (``bin/foo -> ../pkg/foo``): in-tree only because the
    ``..`` resolves against the member's parent dir — it pins the parent-join math."""

    def build(tf):
        _add_file(tf, "docs/file.txt")
        _add_symlink(tf, "docs/link.txt", "file.txt")
        _add_symlink(tf, "top-link.txt", "docs/../docs/file.txt")
        _add_symlink(tf, "a/b/link.txt", "../../docs/file.txt")

    out_tf, skipped = _sanitize_tar(build)
    with out_tf:
        link = out_tf.getmember("docs/link.txt")
        assert link.issym()
        assert link.linkname == "file.txt"
        assert link.uid == 1000 and link.gid == 1000
        top = out_tf.getmember("top-link.txt")
        assert top.issym()
        assert top.linkname == "docs/../docs/file.txt"
        nested = out_tf.getmember("a/b/link.txt")
        assert nested.issym()
        assert nested.linkname == "../../docs/file.txt"
    assert skipped == 0


def test_sanitize_archive_stream_skips_escaping_symlinks():
    """Absolute targets and targets resolving above the archive root are dropped.

    ``nested-up`` escapes only after resolving against its parent dir (``a/`` + ``../../``),
    pinning the parent-join math from the rejecting side; ``dotdot-link`` is the bare-``..``
    boundary case (the parent of the root itself)."""

    def build(tf):
        _add_file(tf, "file.txt")
        _add_symlink(tf, "abs-link", "/etc/passwd")
        _add_symlink(tf, "up-link", "../outside")
        _add_symlink(tf, "sneaky-link", "a/../../outside")
        _add_symlink(tf, "dotdot-link", "..")
        _add_symlink(tf, "a/nested-up", "../../outside")
        _add_symlink(tf, "empty-link", "")

    out_tf, skipped = _sanitize_tar(build)
    with out_tf:
        names = out_tf.getnames()
    assert "file.txt" in names
    assert "abs-link" not in names
    assert "up-link" not in names
    assert "sneaky-link" not in names
    assert "dotdot-link" not in names
    assert "a/nested-up" not in names
    assert "empty-link" not in names
    assert skipped == 6


def test_sanitize_archive_stream_skips_chained_symlink_escape():
    """Lexical target resolution is unsound through another symlink: ``a/b -> ..`` resolves to
    the root (safe on its own), but ``c -> a/b/../z`` then really points at the root's *parent*
    even though it lexically normalizes to the in-tree ``a/z``. Targets traversing an emitted
    symlink mid-path must be rejected, not mis-resolved."""

    def build(tf):
        _add_dir(tf, "a")
        _add_symlink(tf, "a/b", "..")
        _add_symlink(tf, "c", "a/b/../z")

    out_tf, skipped = _sanitize_tar(build)
    with out_tf:
        names = out_tf.getnames()
        assert "a/b" in names  # safe on its own: resolves to the archive root
    assert "c" not in names
    assert skipped == 1


def test_sanitize_archive_stream_skips_members_under_symlinked_dirs():
    """No writes *through* links: a member whose path traverses an emitted symlink is dropped —
    including deep descendants, pinning the ancestor-prefix arithmetic."""

    def build(tf):
        _add_dir(tf, "real")
        _add_symlink(tf, "alias", "real")
        _add_file(tf, "alias/file.txt", b"pwned")
        _add_dir(tf, "a")
        _add_symlink(tf, "a/alias", "real")
        _add_file(tf, "a/alias/b/deep.txt", b"pwned")

    out_tf, skipped = _sanitize_tar(build)
    with out_tf:
        names = out_tf.getnames()
    assert "alias" in names
    assert "alias/file.txt" not in names
    assert "a/alias" in names
    assert "a/alias/b/deep.txt" not in names
    assert skipped == 2


def test_sanitize_archive_stream_skips_symlink_colliding_with_earlier_dir():
    """Out-of-order producer: children listed *before* their dir turns out to be a symlink.
    Emitting both a real dir and a same-named symlink would let Docker's untar delete the
    dir (and its contents) when the symlink is extracted — silent data loss. First wins:
    the dir and its file are preserved, the late symlink is dropped."""

    def build(tf):
        _add_file(tf, "alias/file.txt")
        _add_symlink(tf, "alias", "real")

    out_tf, skipped = _sanitize_tar(build)
    with out_tf:
        assert out_tf.getmember("alias").isdir()
        assert out_tf.getmember("alias/file.txt").isfile()
    assert skipped == 1


def test_sanitize_archive_stream_skips_file_colliding_with_earlier_symlink():
    """The mirror collision: a file/dir named exactly like an already-emitted symlink (not
    merely *under* it) would type-confuse extraction the same way. First wins."""

    def build(tf):
        _add_dir(tf, "real")
        _add_symlink(tf, "alias", "real")
        _add_file(tf, "alias")
        _add_symlink(tf, "alias", "elsewhere")

    out_tf, skipped = _sanitize_tar(build)
    with out_tf:
        alias = out_tf.getmember("alias")
        assert alias.issym()
        assert alias.linkname == "real"
        assert len([n for n in out_tf.getnames() if n == "alias"]) == 1
    assert skipped == 2


def test_sanitize_archive_stream_synthesizes_parents_for_symlinks():
    """A nested symlink arriving with no prior entry under its parent must still get a
    sandbox-owned ancestor dir — otherwise put_archive auto-creates it root-owned."""

    def build(tf):
        _add_symlink(tf, "pkg/link", "target")

    out_tf, skipped = _sanitize_tar(build)
    with out_tf:
        parent = out_tf.getmember("pkg")
        assert parent.isdir()
        assert parent.uid == 1000 and parent.gid == 1000
        assert out_tf.getmember("pkg/link").issym()
    assert skipped == 0


def test_sanitize_archive_stream_logs_one_skip_summary(caplog):
    """Per-member skip WARNINGs are easy to lose; a single ERROR summary with the count and
    names is the greppable signal that the seeded workspace is incomplete."""

    def build(tf):
        _add_file(tf, "file.txt")
        _add_symlink(tf, "abs-link", "/etc/passwd")
        _add_symlink(tf, "up-link", "../outside")

    with caplog.at_level("ERROR"):
        out_tf, skipped = _sanitize_tar(build)
    out_tf.close()
    assert skipped == 2
    summary = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(summary) == 1
    assert "dropped 2 of 3 members" in summary[0].getMessage()
    assert "abs-link" in summary[0].getMessage() and "up-link" in summary[0].getMessage()


def test_sanitize_archive_stream_skips_hardlinks():
    """Hardlink entries are silently skipped in the streamed output."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w") as tf:
        content = b"hello"
        info = tarfile.TarInfo(name="file.txt")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
        lnk = tarfile.TarInfo(name="hardlink.txt")
        lnk.type = tarfile.LNKTYPE
        lnk.linkname = "file.txt"
        tf.addfile(lnk)
    in_buf.seek(0)

    out_buf = io.BytesIO()
    _sanitize_archive_stream(in_buf, out_buf, uid=1000, gid=1000)
    out_buf.seek(0)

    with tarfile.open(fileobj=out_buf) as out_tf:
        names = out_tf.getnames()
    assert "file.txt" in names
    assert "hardlink.txt" not in names


def test_sanitize_archive_stream_synthesizes_missing_parent_dirs():
    """A tar that omits explicit directory entries must still yield sandbox-owned dir members for
    every ancestor (emitted before the file), so put_archive never auto-creates a root-owned,
    sandbox-unwritable directory. This is what lets copy_to_container drop the recursive chown."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w") as tf:
        info = tarfile.TarInfo(name="pkg/sub/mod.py")  # no pkg/ or pkg/sub/ entries
        info.size = 4
        tf.addfile(info, io.BytesIO(b"x=1\n"))
    in_buf.seek(0)

    out_buf = io.BytesIO()
    _sanitize_archive_stream(in_buf, out_buf, uid=1000, gid=1000)
    out_buf.seek(0)

    with tarfile.open(fileobj=out_buf) as out_tf:
        members = {m.name: m for m in out_tf.getmembers()}
        order = out_tf.getnames()
    for d in ("pkg", "pkg/sub"):
        assert d in members and members[d].isdir(), order
        assert members[d].uid == 1000 and members[d].gid == 1000
        assert (members[d].mode & 0o777) == 0o755  # rwxr-xr-x: sandbox-traversable
    assert order.index("pkg") < order.index("pkg/sub") < order.index("pkg/sub/mod.py")


def test_sanitize_archive_stream_preserves_explicit_dir_entry_once():
    """A well-formed tar that lists a directory before its contents keeps that single dir entry with
    its own normalized mode — no duplicate synthetic entry."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w") as tf:
        d = tarfile.TarInfo(name="pkg")
        d.type = tarfile.DIRTYPE
        d.mode = 0o775
        tf.addfile(d)
        f = tarfile.TarInfo(name="pkg/mod.py")
        f.size = 0
        f.mode = 0o644
        tf.addfile(f, io.BytesIO(b""))
    in_buf.seek(0)

    out_buf = io.BytesIO()
    _sanitize_archive_stream(in_buf, out_buf, uid=1000, gid=1000)
    out_buf.seek(0)

    with tarfile.open(fileobj=out_buf) as out_tf:
        members = list(out_tf.getmembers())
    names = [m.name for m in members]
    assert names.count("pkg") == 1
    pkg = next(m for m in members if m.name == "pkg")
    assert pkg.isdir() and pkg.uid == 1000 and (pkg.mode & 0o777) == 0o775


def test_write_file_ships_sanitized_single_file_via_put_archive(mock_docker_client):
    """write_file (default, create_only=False) ships one sanitized file straight to put_archive — no
    copy_to_container, no chmod/chown round-trips. The member lands under the right parent stamped
    with RUN_UID:RUN_GID (put_archive preserves the tar's uid/gid/mode, so no post-copy fix-up)."""
    captured: dict = {}

    def fake_put(path, stream):
        captured["dest"] = path
        captured["tar_bytes"] = stream.read()
        return True

    s = _session_with_container()
    s.execute_command = Mock()
    s.container.put_archive = Mock(side_effect=fake_put)

    s.write_file(f"{SANDBOX_ROOT}/sub/dir/foo.py", b"print('hi')\n", mode=0o755)

    # create_only=False: parent assumed to exist (edit's read-modify-write) → no exec round-trip.
    s.execute_command.assert_not_called()
    assert captured["dest"] == f"{SANDBOX_ROOT}/sub/dir"

    with tarfile.open(fileobj=io.BytesIO(captured["tar_bytes"])) as tf:
        members = tf.getmembers()
        assert len(members) == 1
        m = members[0]
        assert m.name == "foo.py" and m.isfile()
        assert m.uid == settings.RUN_UID and m.gid == settings.RUN_GID
        assert (m.mode & 0o7777) == 0o755
        assert tf.extractfile(m).read() == b"print('hi')\n"


def test_copy_to_container_streams_sanitized_output_to_put_archive(mock_docker_client):
    """copy_to_container hands a file-like (not bytes) to put_archive."""
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session.container.put_archive.return_value = True

    src = io.BytesIO()
    with tarfile.open(fileobj=src, mode="w") as tf:
        info = tarfile.TarInfo(name="hello.txt")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"hello"))
    src.seek(0)

    session.copy_to_container(src, dest=SANDBOX_ROOT, clear_before_copy=False)

    assert session.container.put_archive.called
    _path, sanitized_arg = session.container.put_archive.call_args.args
    # The Docker SDK accepts either bytes or a file-like; we now pass the latter
    # so large archives never materialize in memory.
    assert not isinstance(sanitized_arg, (bytes, bytearray))
    assert hasattr(sanitized_arg, "read") and hasattr(sanitized_arg, "seek")


def test_build_single_file_tar_stream_returns_seekable_stream():
    """Helper returns a seekable stream (not bytes) positioned at offset 0."""
    content = b"hello world"
    with _build_single_file_tar_stream("foo.txt", content, mode=0o644) as stream:
        assert not isinstance(stream, (bytes, bytearray))
        assert hasattr(stream, "read") and hasattr(stream, "seek")
        assert stream.tell() == 0

        with tarfile.open(fileobj=stream) as tf:
            members = tf.getmembers()
            assert len(members) == 1
            assert members[0].name == "foo.txt"
            assert members[0].isfile()
            assert (members[0].mode & 0o7777) == 0o644
            assert tf.extractfile(members[0]).read() == content


def test_build_single_file_tar_stream_handles_large_content():
    """Large content does not force the helper to materialize a `bytes` archive."""
    big = b"x" * (4 * 1024 * 1024)  # 4 MiB — well above the in-memory spool limit.
    with _build_single_file_tar_stream("big.bin", big, mode=0o600) as stream:
        assert not isinstance(stream, (bytes, bytearray))
        with tarfile.open(fileobj=stream) as tf:
            members = tf.getmembers()
            assert len(members) == 1
            assert members[0].name == "big.bin"
            assert (members[0].mode & 0o7777) == 0o600
            assert tf.extractfile(members[0]).read() == big


def test_write_file_rejects_path_outside_sandbox_root(mock_docker_client):
    """write_file refuses paths outside SANDBOX_ROOT."""
    session = SandboxDockerSession()
    session.container = MagicMock()

    with pytest.raises(ValueError, match="must be under"):
        session.write_file("/etc/passwd", b"pwned", mode=0o644)


def test_write_file_rejects_traversal(mock_docker_client):
    """write_file refuses paths with .. segments."""
    session = SandboxDockerSession()
    session.container = MagicMock()

    with pytest.raises(ValueError):
        session.write_file(f"{SANDBOX_ROOT}/../etc/passwd", b"pwned", mode=0o644)


def test_write_file_rejects_nul_in_path(mock_docker_client):
    """write_file refuses paths containing NUL or newline characters."""
    session = SandboxDockerSession()
    session.container = MagicMock()

    with pytest.raises(ValueError):
        session.write_file(f"{SANDBOX_ROOT}/foo\x00bar", b"x", mode=0o644)


def test_write_file_create_only_folds_probe_and_mkdir_into_one_exec(mock_docker_client):
    """create_only collapses the old probe + mkdir + chmod -R + chown -R into a SINGLE exec that both
    rejects an existing path and ensures the parent dir, followed by one put_archive — and never
    shells out to chmod/chown (put_archive carries the sanitized uid/gid/mode)."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output="OK"))
    s.container.put_archive = Mock(return_value=True)

    s.write_file(f"{SANDBOX_ROOT}/sub/foo.py", b"x", mode=0o644, allowed_roots=(SANDBOX_ROOT,), create_only=True)

    s.execute_command.assert_called_once()
    cmd = s.execute_command.call_args.args[0]
    assert "[ -e " in cmd and "mkdir -p" in cmd  # existence probe + parent creation in one command
    assert "chmod" not in cmd and "chown" not in cmd
    s.container.put_archive.assert_called_once()
    assert s.container.put_archive.call_args.args[0] == f"{SANDBOX_ROOT}/sub"


def test_write_file_create_only_rejects_existing(mock_docker_client):
    """create_only=True refuses to overwrite: the combined probe/mkdir exec reports EXISTS, so it
    raises FileExistsError before any archive is shipped."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output="EXISTS"))
    s.container.put_archive = Mock()
    with pytest.raises(FileExistsError, match="already exists"):
        s.write_file(f"{SANDBOX_ROOT}/a.txt", b"x", mode=0o644, allowed_roots=(SANDBOX_ROOT,), create_only=True)
    s.container.put_archive.assert_not_called()


def test_write_file_create_only_allows_new(mock_docker_client):
    """create_only=True writes when the combined exec reports OK (absent + parent ensured)."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output="OK"))
    s.container.put_archive = Mock(return_value=True)
    s.write_file(f"{SANDBOX_ROOT}/a.txt", b"x", mode=0o644, allowed_roots=(SANDBOX_ROOT,), create_only=True)
    s.container.put_archive.assert_called_once()


def test_write_file_create_only_staging_failure_raises(mock_docker_client):
    """A malfunctioning staging exec (unrecognised marker / non-zero exit) fails closed: it raises
    RuntimeError instead of being mistaken for 'absent' and silently overwriting the file."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=2, output="sh: mkdir: not found"))
    s.container.put_archive = Mock()
    with pytest.raises(RuntimeError, match="stag"):
        s.write_file(f"{SANDBOX_ROOT}/a.txt", b"x", mode=0o644, allowed_roots=(SANDBOX_ROOT,), create_only=True)
    s.container.put_archive.assert_not_called()


def test_write_file_default_skips_existence_probe(mock_docker_client):
    """create_only=False (edit's write-back) overwrites in place: no exec round-trip at all, just one
    put_archive into the existing parent."""
    s = _session_with_container()
    s.execute_command = Mock()
    s.container.put_archive = Mock(return_value=True)
    s.write_file(f"{SANDBOX_ROOT}/a.txt", b"x", mode=0o644, allowed_roots=(SANDBOX_ROOT,))
    s.execute_command.assert_not_called()
    s.container.put_archive.assert_called_once()


def test_edit_file_write_back_does_not_use_create_only():
    """edit's write-back must overwrite the file it just read (create_only must stay falsy)."""
    s = _edit_session(b"hello world\n")
    s.edit_file("/scratch/a.txt", "world", "there", replace_all=False, allowed_roots=("/scratch",))
    assert not s.write_file.call_args.kwargs.get("create_only")


def test_copy_to_container_allows_skills_root(mock_docker_client):
    """copy_to_container accepts /skills (and subdirs) as a destination."""
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session.container.put_archive.return_value = True

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name="skill.md")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"hello"))
    buf.seek(0)

    # Should not raise; /skills is accepted.
    session.copy_to_container(buf, dest=SKILLS_ROOT, clear_before_copy=False)

    # Subpaths under /skills also accepted.
    buf.seek(0)
    session.copy_to_container(buf, dest=f"{SKILLS_ROOT}/builtin", clear_before_copy=False)


def test_copy_to_container_rejects_non_reserved_root(mock_docker_client):
    """Paths outside reserved roots are still refused."""
    session = SandboxDockerSession()
    session.container = MagicMock()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name="x")
        info.size = 0
        tf.addfile(info, io.BytesIO(b""))
    buf.seek(0)

    with pytest.raises(ValueError, match="Refusing to extract"):
        session.copy_to_container(buf, dest="/etc/passwd", clear_before_copy=False)


def test_copy_to_container_allows_bare_workspace_root(mock_docker_client):
    """A bare /workspace dest is accepted (its subdirs repo/skills/tmp all live under it)."""
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session.container.put_archive.return_value = True

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name="x")
        info.size = 0
        tf.addfile(info, io.BytesIO(b""))
    buf.seek(0)

    session.copy_to_container(buf, dest=WORKSPACE_ROOT, clear_before_copy=False)


def test_copy_to_container_skips_redundant_chmod_and_chown(mock_docker_client):
    """copy_to_container no longer runs the recursive chmod/chown after put_archive: the sanitizer
    stamps every member's uid/gid/mode and put_archive preserves them, so the post-copy passes (which
    for a large seed tree walked thousands of files) are pure waste and have been removed."""
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.put_archive.return_value = True
    session.container.exec_run.return_value = ExecResult(exit_code=0, output=b"")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name="a.txt")
        info.size = 0
        tf.addfile(info, io.BytesIO(b""))
    buf.seek(0)

    session.copy_to_container(buf)

    verbs = [c.args[0][0] for c in session.container.exec_run.call_args_list if c.args and c.args[0]]
    assert "chmod" not in verbs and "chown" not in verbs


def test_copy_to_container_rejects_dest_traversal(mock_docker_client):
    """A `..` in an absolute dest is rejected at the boundary (defense-in-depth, not caller-dependent)."""
    session = SandboxDockerSession()
    session.container = MagicMock()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name="x")
        info.size = 0
        tf.addfile(info, io.BytesIO(b""))
    buf.seek(0)

    with pytest.raises(ValueError, match="Refusing to extract"):
        session.copy_to_container(buf, dest=f"{WORKSPACE_ROOT}/../etc", clear_before_copy=False)


def test_start_container_creates_skills_root(mock_docker_client):
    """A freshly-started container has /skills owned by the sandbox user."""
    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session._start_container("alpine:latest")

    # mkdir -p was called including SKILLS_ROOT alongside the other roots.
    mock_container.exec_run.assert_any_call(
        ["mkdir", "-p", "--", WORKSPACE_ROOT, SANDBOX_ROOT, SANDBOX_HOME, SKILLS_ROOT, SCRATCH_ROOT], user="root"
    )
    # chown was called for SKILLS_ROOT alongside the other roots.
    mock_container.exec_run.assert_any_call(
        [
            "chown",
            f"{settings.RUN_UID}:{settings.RUN_GID}",
            "--",
            WORKSPACE_ROOT,
            SANDBOX_ROOT,
            SANDBOX_HOME,
            SKILLS_ROOT,
            SCRATCH_ROOT,
        ],
        user="root",
    )


def test_sanitize_archive_stream_raises_on_empty_stream():
    """An empty input stream raises ValueError."""
    in_buf = io.BytesIO(b"")
    out_buf = io.BytesIO()
    with pytest.raises(ValueError, match="Invalid or truncated archive"):
        _sanitize_archive_stream(in_buf, out_buf, uid=1000, gid=1000)


def test_sanitize_archive_stream_raises_on_garbage_bytes():
    """Bytes that are not a valid tar raise ValueError."""
    in_buf = io.BytesIO(b"this is definitely not a tar archive!!!!!")
    out_buf = io.BytesIO()
    with pytest.raises(ValueError, match="Invalid or truncated archive"):
        _sanitize_archive_stream(in_buf, out_buf, uid=1000, gid=1000)


def test_sanitize_archive_stream_raises_on_absolute_path_member():
    """Archive with an absolute-path member raises ValueError."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w") as tf:
        info = tarfile.TarInfo(name="/etc/passwd")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"hello"))
    in_buf.seek(0)

    out_buf = io.BytesIO()
    with pytest.raises(ValueError, match="absolute path"):
        _sanitize_archive_stream(in_buf, out_buf, uid=1000, gid=1000)


def test_sanitize_archive_stream_raises_on_traversal_member():
    """Archive with a '..' traversal path raises ValueError."""
    in_buf = io.BytesIO()
    with tarfile.open(fileobj=in_buf, mode="w") as tf:
        info = tarfile.TarInfo(name="../evil.py")
        info.size = 5
        tf.addfile(info, io.BytesIO(b"hello"))
    in_buf.seek(0)

    out_buf = io.BytesIO()
    with pytest.raises(ValueError, match="traversal"):
        _sanitize_archive_stream(in_buf, out_buf, uid=1000, gid=1000)


def test_start_container_creates_scratch_root(mock_docker_client):
    """The container bootstrap must mkdir + chown /scratch alongside the other roots."""
    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session._start_container("img:latest")

    mkdir_calls = [c for c in mock_container.exec_run.call_args_list if c.args and c.args[0][0] == "mkdir"]
    assert any(SCRATCH_ROOT in c.args[0] for c in mkdir_calls), "scratch root not created"
    chown_calls = [c for c in mock_container.exec_run.call_args_list if c.args and c.args[0][0] == "chown"]
    assert any(SCRATCH_ROOT in c.args[0] for c in chown_calls), "scratch root not chowned"


def _session_with_container():
    s = SandboxDockerSession.__new__(SandboxDockerSession)
    s.container = Mock()
    s.client = Mock()
    s.session_id = "sid"
    return s


def _tar_of(path_in_tar: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=path_in_tar)
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_read_file_bytes_extracts_single_member():
    s = _session_with_container()
    s.container.get_archive.return_value = (iter([_tar_of("foo.txt", b"hello\n")]), {"size": 6})
    assert s.read_file_bytes("/scratch/foo.txt") == b"hello\n"


def test_list_dir_parses_ls_output():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output="sub/\nfile.py\n"))
    entries = s.list_dir("/scratch")
    assert ("/scratch/sub", True) in entries
    assert ("/scratch/file.py", False) in entries


def test_grep_parses_matches():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output="/scratch/a.py:3:found here\n"))
    matches = s.grep("found", "/scratch", glob=None)
    assert matches == [("/scratch/a.py", 3, "found here")]


def test_grep_uses_extended_regex_not_fixed_strings():
    """grep must run in ERE mode (-E), never fixed-strings (-F), and probe the pattern first."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=1, output=""))
    s.grep("foo|bar", "/scratch", glob=None)
    cmd = s.execute_command.call_args.args[0]
    assert "-HnE" in cmd
    assert "-HnF" not in cmd
    assert "grep -E -e" in cmd  # the /dev/null validity probe
    assert f"exit {_GREP_BAD_PATTERN_EXIT}" in cmd


def test_grep_invalid_pattern_raises_value_error():
    """An invalid ERE (probe exits 2 -> sentinel) surfaces as ValueError, not RuntimeError."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_GREP_BAD_PATTERN_EXIT, output=""))
    with pytest.raises(ValueError, match="invalid regular expression"):
        s.grep("foo(", "/scratch", glob=None)


def test_delete_file_runs_rm():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output=""))
    assert s.delete_file("/scratch/x") is True
    s.execute_command.assert_called_once()
    assert "rm -f" in s.execute_command.call_args.args[0]


def test_read_file_bytes_missing_path_raises_file_not_found():
    """docker get_archive raises NotFound for a missing path; surface it as FileNotFoundError."""
    s = _session_with_container()
    s.container.get_archive.side_effect = NotFound("no such path")
    with pytest.raises(FileNotFoundError):
        s.read_file_bytes("/scratch/missing.txt")


def test_read_file_bytes_empty_file_returns_empty_bytes():
    """A genuinely empty file must return b'' (not be reported as missing)."""
    s = _session_with_container()
    s.container.get_archive.return_value = (iter([_tar_of("empty.txt", b"")]), {"size": 0})
    assert s.read_file_bytes("/scratch/empty.txt") == b""


def test_grep_filters_by_basename_glob():
    """grep with a glob filters results host-side by basename (busybox grep has no --include)."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output="/scratch/a.py:1:hit\n/scratch/sub/b.txt:2:hit\n"))
    matches = s.grep("hit", "/scratch", glob="*.py")
    assert matches == [("/scratch/a.py", 1, "hit")]
    # --include must NOT be used (busybox lacks it).
    assert "--include" not in s.execute_command.call_args.args[0]


def test_grep_raises_on_real_error_exit():
    """grep exit code >= 2 (other than the absent-path sentinel) is a real error, so surface it."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=2, output="grep: bad things"))
    with pytest.raises(RuntimeError):
        s.grep("x", "/scratch", glob=None)


def test_grep_no_matches_exit_1_is_ok():
    """grep exit code 1 means 'no matches' and must return an empty list, not raise."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=1, output=""))
    assert s.grep("x", "/scratch", glob=None) == []


def test_grep_missing_path_raises_file_not_found():
    """A genuinely absent search path is reported via FileNotFoundError (sentinel exit), distinct
    from grep's own exit 2 — so fs_grep can treat it as 'no matches' rather than an error."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_PATH_ABSENT_EXIT, output=""))
    with pytest.raises(FileNotFoundError):
        s.grep("x", "/scratch/missing", glob=None)
    assert f"|| exit {_PATH_ABSENT_EXIT}" in s.execute_command.call_args.args[0]


def test_list_dir_raises_on_error_exit():
    """A non-zero exit that is NOT the absent-path sentinel (e.g. ls's exit 2 for permission
    denied on an existing path) is a genuine failure and must raise RuntimeError."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=2, output="ls: cannot access"))
    with pytest.raises(RuntimeError):
        s.list_dir("/scratch/denied")


def test_list_dir_missing_path_raises_file_not_found():
    """A genuinely absent path is reported via FileNotFoundError (the shell guard's sentinel
    exit code), distinct from a real listing failure — so fs_ls can treat it as an empty listing."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_PATH_ABSENT_EXIT, output=""))
    with pytest.raises(FileNotFoundError):
        s.list_dir("/scratch/missing")
    # The listing must probe existence so a true absence is distinguishable from a real error.
    assert f"|| exit {_PATH_ABSENT_EXIT}" in s.execute_command.call_args.args[0]


def test_find_paths_parses_type_markers():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output="/scratch/sub/D\n/scratch/f.py/F\n"))
    entries = s.find_paths("/scratch")
    assert ("/scratch/sub", True) in entries
    assert ("/scratch/f.py", False) in entries


def test_find_paths_raises_on_error_exit():
    """A non-zero exit that is NOT the absent-path sentinel (e.g. a genuine traversal failure on an
    existing tree) must raise RuntimeError."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=1, output="find: bad things"))
    with pytest.raises(RuntimeError):
        s.find_paths("/scratch/denied")


def test_find_paths_missing_path_raises_file_not_found():
    """A genuinely absent path is reported via FileNotFoundError (sentinel exit), distinct from a
    real traversal failure — so fs_glob can treat it as no matches."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_PATH_ABSENT_EXIT, output=""))
    with pytest.raises(FileNotFoundError):
        s.find_paths("/scratch/missing")
    assert f"|| exit {_PATH_ABSENT_EXIT}" in s.execute_command.call_args.args[0]


def _edit_session(initial: bytes):
    s = _session_with_container()
    s.read_file_bytes = Mock(return_value=initial)
    s.write_file = Mock()
    return s


def test_edit_file_single_replacement():
    s = _edit_session(b"hello world\n")
    assert s.edit_file("/scratch/a.txt", "world", "there", replace_all=False, allowed_roots=("/scratch",)) == 1
    written = s.write_file.call_args.args[1]
    assert written == b"hello there\n"


def test_edit_file_string_not_found():
    s = _edit_session(b"hello\n")
    with pytest.raises(ValueError, match="string_not_found"):
        s.edit_file("/scratch/a.txt", "absent", "x", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()


def test_edit_file_multiple_occurrences_without_replace_all():
    s = _edit_session(b"x x x\n")
    with pytest.raises(ValueError, match="appears 3 times"):
        s.edit_file("/scratch/a.txt", "x", "y", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()


def test_edit_file_eof_newline_unique_hint():
    """old ends with a newline the file lacks at EOF, and the stripped key is unique → precise hint."""
    s = _edit_session(b"abcdefkey")
    with pytest.raises(ValueError, match="trailing newline removed"):
        s.edit_file("/scratch/a.txt", "key\n", "KEY\n", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()


def test_edit_file_eof_newline_ambiguous_hint():
    """old ends with a newline the file lacks at EOF, and the stripped key is ambiguous →
    hint to drop the newline AND add surrounding context."""
    s = _edit_session(b"abckeydefkey")
    with pytest.raises(ValueError, match="add surrounding context"):
        s.edit_file("/scratch/a.txt", "key\n", "KEY\n", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()


def test_edit_file_eof_newline_hint_normalizes_crlf():
    """The hint LF-normalizes the file, so a CRLF body with no trailing newline still triggers the
    unique 'trailing newline removed' hint — guards the text_lf normalization in the hint branch."""
    s = _edit_session(b"abcdef\r\nkey")
    with pytest.raises(ValueError, match="trailing newline removed"):
        s.edit_file("/scratch/a.txt", "key\n", "KEY\n", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()


def test_edit_file_single_newline_old_not_eof_hint():
    """A lone-newline `old` must not enter the hint branch (the len(old_lf) > 1 guard): it falls
    through to the plain string_not_found rather than a misleading EOF hint."""
    s = _edit_session(b"abcdef")
    with pytest.raises(ValueError, match="string_not_found"):
        s.edit_file("/scratch/a.txt", "\n", "X", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()


def test_edit_file_replace_all_counts_all():
    s = _edit_session(b"x x x\n")
    assert s.edit_file("/scratch/a.txt", "x", "y", replace_all=True, allowed_roots=("/scratch",)) == 3
    assert s.write_file.call_args.args[1] == b"y y y\n"


def test_edit_file_matches_lf_old_against_crlf_file():
    """An LF-supplied `old` should match a CRLF file and write back CRLF-preserving content."""
    s = _edit_session(b"a\r\nb\r\n")
    count = s.edit_file("/scratch/a.txt", "a\nb", "a\nB", replace_all=False, allowed_roots=("/scratch",))
    assert count == 1
    assert s.write_file.call_args.args[1] == b"a\r\nB\r\n"


def test_roots_are_under_workspace():
    assert WORKSPACE_ROOT == "/workspace"
    assert SANDBOX_ROOT == "/workspace/repo"
    assert SKILLS_ROOT == "/workspace/skills"
    assert SCRATCH_ROOT == "/workspace/tmp"


@pytest.mark.parametrize("path", ["/workspace/repo/main.py", "/workspace/skills/x/SKILL.md", "/workspace/tmp/note.txt"])
def test_validate_accepts_anything_under_workspace(path):
    assert _validate_sandbox_path(path, allowed_roots=(WORKSPACE_ROOT,)) == path


def test_validate_allows_workspace_root_only_with_allow_root():
    assert _validate_sandbox_path("/workspace", allowed_roots=(WORKSPACE_ROOT,), allow_root=True) == "/workspace"
    with pytest.raises(ValueError):
        _validate_sandbox_path("/workspace", allowed_roots=(WORKSPACE_ROOT,))


@pytest.mark.parametrize("path", ["/etc/passwd", "/workspace/../etc", "/repo/main.py", "relative/x"])
def test_validate_rejects_outside_workspace(path):
    with pytest.raises(ValueError):
        _validate_sandbox_path(path, allowed_roots=(WORKSPACE_ROOT,))


def _tar_of_dir(dir_name: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=dir_name)
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
    return buf.getvalue()


def test_list_dir_wrong_type_raises_not_a_directory():
    """The classifying guard exits 8 when the path exists but is not a directory."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_PATH_WRONG_TYPE_EXIT, output=""))
    with pytest.raises(NotADirectoryError):
        s.list_dir("/scratch/a-file")
    # Assert the require="dir" prologue actually emits the type test (not just that the mock exit maps).
    cmd = s.execute_command.call_args.args[0]
    assert "[ -d " in cmd and f"exit {_PATH_WRONG_TYPE_EXIT}" in cmd


def test_list_dir_permission_denied_raises():
    """The guard exits 9 when an existing directory is not readable/traversable by the sandbox user."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_PATH_DENIED_EXIT, output=""))
    with pytest.raises(PermissionError):
        s.list_dir("/scratch/denied")
    cmd = s.execute_command.call_args.args[0]
    assert f"exit {_PATH_DENIED_EXIT}" in cmd and "[ -x " in cmd


def test_find_paths_wrong_type_raises_not_a_directory():
    """find_paths also uses require="dir", so a non-directory base exits 8 -> NotADirectoryError."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_PATH_WRONG_TYPE_EXIT, output=""))
    with pytest.raises(NotADirectoryError):
        s.find_paths("/scratch/a-file")
    cmd = s.execute_command.call_args.args[0]
    assert "[ -d " in cmd and f"exit {_PATH_WRONG_TYPE_EXIT}" in cmd


def test_find_paths_includes_prune_fragment_in_command():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output=""))
    s.find_paths("/scratch", excludes=(".git", "__pycache__"))
    cmd = s.execute_command.call_args.args[0]
    assert r"-type d \( -name '.git' -o -name '__pycache__' \) -prune -o" in cmd
    assert "-print" in cmd


def test_find_paths_without_excludes_omits_prune_fragment():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output=""))
    s.find_paths("/scratch")
    cmd = s.execute_command.call_args.args[0]
    assert "-prune" not in cmd  # empty predicate => no prune fragment
    assert "-type d -print" in cmd  # dir pass still present (find's default action made explicit)
    assert "! -type d -print" in cmd  # file pass still present
    # NOTE: assert on these fragments, NOT "-mindepth 1 -type d -print": an empty predicate leaves a
    # double space ("-mindepth 1  -type d") in the command, so a single-space match would falsely fail.


def test_grep_permission_denied_raises():
    """grep uses the default guard (require=None): existence + readability only. An unreadable path
    exits 9 -> PermissionError, and the prologue must NOT emit the dir-only wrong-type test."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_PATH_DENIED_EXIT, output=""))
    with pytest.raises(PermissionError):
        s.grep("x", "/scratch/denied", glob=None)
    cmd = s.execute_command.call_args.args[0]
    assert f"exit {_PATH_DENIED_EXIT}" in cmd
    assert f"exit {_PATH_WRONG_TYPE_EXIT}" not in cmd  # require=None -> no directory type test


def test_grep_directory_branch_uses_find_xargs_with_prune_and_sentinel():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output=""))
    s.grep("needle", "/scratch", glob=None, excludes=(".git",))
    cmd = s.execute_command.call_args.args[0]
    assert "if [ -d '/scratch' ]" in cmd  # directory branch
    assert r"\( -name '.git' \) -prune -o" in cmd  # pruning applied
    assert "-type f -print0" in cmd
    assert "xargs -0 grep -HnE -e 'needle' /dev/null" in cmd  # /dev/null sentinel, ERE pattern
    assert "else grep -HnE -e 'needle' --" in cmd  # single-file fallback branch
    # Lock the read-error contract (only integration tests exercise it for real, so pin its shape
    # here so a refactor can't silently drop it): grep's stderr is captured via the fd-swap while
    # matches flow through fd 3, and a non-empty capture surfaces the read error as exit 2.
    # fd 3 MUST be opened with a standalone `exec 3>&1` (not a `3>&1` redirection on the assignment):
    # bash does not expose an assignment-command redirection inside its command substitution, so the
    # inline form left fd 3 closed under bash (`1>&3` -> "Bad file descriptor"), breaking every
    # directory grep on images that ship /bin/bash. Pin the portable form here so it can't regress.
    assert "exec 3>&1; errs=$(" in cmd  # fd 3 opened in the parent shell, visible to the substitution
    assert "2>&1 1>&3 3>&-); ec=$?; exec 3>&-;" in cmd  # capture stderr, keep matches on stdout, then close fd 3
    assert "3>&1;" not in cmd.replace("exec 3>&1;", "")  # the broken assignment-redirection form is gone
    assert '[ -z "$errs" ] || exit 2;' in cmd  # unreadable file -> surface, not swallow
    # And the xargs exit-code normalization (xargs collapses grep 1/2 -> 123) stays after it.
    assert '[ "$ec" -eq 0 ] || [ "$ec" -eq 123 ] || exit 2;' in cmd


def test_grep_without_excludes_still_builds_directory_branch():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output=""))
    s.grep("x", "/scratch", glob=None)
    cmd = s.execute_command.call_args.args[0]
    assert "if [ -d '/scratch' ]" in cmd
    assert "-prune" not in cmd  # empty predicate => no prune fragment


def test_read_file_bytes_directory_raises_is_a_directory():
    """Reading a directory must raise IsADirectoryError, not return an inner file's bytes."""
    s = _session_with_container()
    s.container.get_archive.return_value = (iter([_tar_of_dir("somedir/")]), {"size": 0})
    with pytest.raises(IsADirectoryError):
        s.read_file_bytes("/scratch/somedir")


def test_delete_file_absent_returns_false():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_PATH_ABSENT_EXIT, output=""))
    assert s.delete_file("/scratch/missing") is False


def test_delete_file_directory_raises_is_a_directory():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=_PATH_WRONG_TYPE_EXIT, output=""))
    with pytest.raises(IsADirectoryError):
        s.delete_file("/scratch/adir")


def test_delete_file_removed_returns_true():
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output=""))
    assert s.delete_file("/scratch/x") is True


def test_prune_predicate_empty_returns_empty_string():
    from daiv_sandbox.sessions import _prune_predicate

    assert _prune_predicate(()) == ""


def test_prune_predicate_single_name_has_no_alternation():
    from daiv_sandbox.sessions import _prune_predicate

    # A single exclude must not emit a dangling `-o`.
    frag = _prune_predicate(("node_modules",))
    assert frag == r"-type d \( -name 'node_modules' \) -prune -o"


def test_prune_predicate_builds_quoted_name_alternation():
    from daiv_sandbox.sessions import _prune_predicate

    frag = _prune_predicate((".git", "__pycache__", "*.egg-info"))
    assert frag == r"-type d \( -name '.git' -o -name '__pycache__' -o -name '*.egg-info' \) -prune -o"


def test_prune_predicate_shell_quotes_each_name():
    from daiv_sandbox.sessions import _prune_predicate

    # A name containing a single quote must be POSIX-escaped, never break out of quoting.
    frag = _prune_predicate(("a'b",))
    assert frag == r"""-type d \( -name 'a'"'"'b' \) -prune -o"""


def test_fs_glob_grep_requests_default_exclude_to_empty_list():
    from daiv_sandbox.schemas import FsGlobRequest, FsGrepRequest

    assert FsGlobRequest(path="/workspace", pattern="**/*.py").exclude == []
    assert FsGrepRequest(path="/workspace", pattern="x").exclude == []
    assert FsGlobRequest(path="/workspace", pattern="*", exclude=["node_modules"]).exclude == ["node_modules"]
    assert FsGrepRequest(path="/workspace", pattern="x", exclude=["node_modules"]).exclude == ["node_modules"]
