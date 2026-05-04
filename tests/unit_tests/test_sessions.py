import io
import tarfile
from unittest.mock import ANY, MagicMock, patch

import pytest
from docker.errors import ImageNotFound, NotFound
from docker.models.containers import ExecResult
from docker.models.images import Image

from daiv_sandbox.config import settings
from daiv_sandbox.sessions import (
    PIPEFAIL_WRAPPER,
    SANDBOX_HOME,
    SANDBOX_ROOT,
    SKILLS_ROOT,
    WORKDIR_ROOT,
    SandboxDockerSession,
    _build_single_file_tar_stream,
    _sanitize_archive_bytes,
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
        command=["-lc", "sleep 3600"],
        detach=True,
        tty=True,
        runtime=settings.RUNTIME,
        remove=True,
        user=f"{settings.RUN_UID}:{settings.RUN_GID}",
    )
    assert session.container is not None
    assert session.container.id == mock_docker_client.containers.run.return_value.id
    assert session.session_id == mock_docker_client.containers.run.return_value.id
    # Should create sandbox directories and chown them
    mock_container.exec_run.assert_any_call(
        ["mkdir", "-p", "--", SANDBOX_ROOT, WORKDIR_ROOT, SANDBOX_HOME, SKILLS_ROOT], user="root"
    )
    mock_container.exec_run.assert_any_call(
        [
            "chown",
            f"{settings.RUN_UID}:{settings.RUN_GID}",
            "--",
            SANDBOX_ROOT,
            WORKDIR_ROOT,
            SANDBOX_HOME,
            SKILLS_ROOT,
        ],
        user="root",
    )


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


def test_copy_to_runtime_creates_directory(mock_docker_client):
    import io
    import tarfile

    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.exec_run.side_effect = [
        ExecResult(exit_code=0, output=b""),  # rm
        ExecResult(exit_code=0, output=b""),  # mkdir
        ExecResult(exit_code=0, output=b""),  # chmod
        ExecResult(exit_code=0, output=b""),  # chown
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("a.txt")
        ti.size = 0
        tf.addfile(ti, io.BytesIO(b""))
    session.copy_to_container(buf)
    # Should use SANDBOX_ROOT by default
    session.container.exec_run.assert_any_call(["/bin/sh", "-c", ANY], user="root")
    session.container.exec_run.assert_any_call(["mkdir", "-p", "--", SANDBOX_ROOT], user="root")
    session.container.exec_run.assert_any_call(["chmod", "-R", "a+rX,u+w", "--", SANDBOX_ROOT], user="root")
    session.container.exec_run.assert_any_call(
        ["chown", "-R", f"{settings.RUN_UID}:{settings.RUN_GID}", "--", SANDBOX_ROOT], user="root"
    )


def test_copy_from_runtime_raises_error_if_file_not_found(mock_docker_client):
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.get_archive.return_value = ([], {"size": 0})
    with pytest.raises(FileNotFoundError):
        session.copy_from_container("/path/to/src")
    # Absolute paths should be used as-is
    session.container.get_archive.assert_called_once_with("/path/to/src")


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
    session.container.exec_run.side_effect = [
        ExecResult(exit_code=0, output=b""),  # rm
        ExecResult(exit_code=0, output=b""),  # mkdir
        ExecResult(exit_code=0, output=b""),  # chmod
        ExecResult(exit_code=0, output=b""),  # chown
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("a.txt")
        ti.size = 0
        tf.addfile(ti, io.BytesIO(b""))
    session.copy_to_container(buf, dest="subdir")
    expected_path = f"{SANDBOX_ROOT}/subdir"
    session.container.exec_run.assert_any_call(["/bin/sh", "-c", ANY], user="root")
    session.container.exec_run.assert_any_call(["mkdir", "-p", "--", expected_path], user="root")
    session.container.exec_run.assert_any_call(["chmod", "-R", "a+rX,u+w", "--", expected_path], user="root")
    session.container.exec_run.assert_any_call(
        ["chown", "-R", f"{settings.RUN_UID}:{settings.RUN_GID}", "--", expected_path], user="root"
    )


def test_copy_from_container_with_relative_path(mock_docker_client):
    """Test that relative paths are resolved under SANDBOX_ROOT"""
    session = SandboxDockerSession()
    session.container = MagicMock()
    session.container.get_archive.return_value = ([b"data"], {"size": 100})
    session.copy_from_container("subdir")
    expected_path = f"{SANDBOX_ROOT}/subdir"
    session.container.get_archive.assert_called_once_with(expected_path)


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


def test_sanitize_archive_bytes_skips_symlinks():
    """Symlink entries should be silently skipped, not raise ValueError."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        content = b"hello"
        info = tarfile.TarInfo(name="file.txt")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
        sym = tarfile.TarInfo(name="CLAUDE.md")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "file.txt"
        tf.addfile(sym)

    result = _sanitize_archive_bytes(buf.getvalue(), uid=1000, gid=1000)

    with tarfile.open(fileobj=io.BytesIO(result)) as out_tf:
        names = out_tf.getnames()
    assert "file.txt" in names
    assert "CLAUDE.md" not in names


def test_sanitize_archive_bytes_skips_hardlinks():
    """Hardlink entries should be silently skipped, not raise ValueError."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        content = b"hello"
        info = tarfile.TarInfo(name="file.txt")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
        lnk = tarfile.TarInfo(name="hardlink.txt")
        lnk.type = tarfile.LNKTYPE
        lnk.linkname = "file.txt"
        tf.addfile(lnk)

    result = _sanitize_archive_bytes(buf.getvalue(), uid=1000, gid=1000)

    with tarfile.open(fileobj=io.BytesIO(result)) as out_tf:
        names = out_tf.getnames()
    assert "file.txt" in names
    assert "hardlink.txt" not in names


def test_write_file_builds_singlefile_tar_for_repo_path(mock_docker_client, monkeypatch):
    """write_file places content at /repo/<rel> via copy_to_container with the right mode."""
    captured: dict = {}

    def fake_copy(self, tardata, dest=None, clear_before_copy=True):
        captured["dest"] = dest
        captured["clear"] = clear_before_copy
        captured["tardata_type"] = type(tardata)
        captured["tar_bytes"] = tardata.read()

    monkeypatch.setattr(SandboxDockerSession, "copy_to_container", fake_copy)

    session = SandboxDockerSession()
    session.container = MagicMock()
    session.write_file(f"{SANDBOX_ROOT}/sub/dir/foo.py", b"print('hi')\n", mode=0o755)

    assert captured["dest"] == f"{SANDBOX_ROOT}/sub/dir"
    assert captured["clear"] is False
    assert not issubclass(captured["tardata_type"], (bytes, bytearray))

    with tarfile.open(fileobj=io.BytesIO(captured["tar_bytes"])) as tf:
        members = tf.getmembers()
        assert len(members) == 1
        assert members[0].name == "foo.py"
        assert members[0].isfile()
        assert (members[0].mode & 0o7777) == 0o755
        assert tf.extractfile(members[0]).read() == b"print('hi')\n"


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


def test_start_container_creates_skills_root(mock_docker_client):
    """A freshly-started container has /skills owned by the sandbox user."""
    session = SandboxDockerSession()
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.exec_run.return_value = ExecResult(exit_code=0, output=b"")
    session._start_container("alpine:latest")

    # mkdir -p was called including SKILLS_ROOT alongside the other roots.
    mock_container.exec_run.assert_any_call(
        ["mkdir", "-p", "--", SANDBOX_ROOT, WORKDIR_ROOT, SANDBOX_HOME, SKILLS_ROOT], user="root"
    )
    # chown was called for SKILLS_ROOT alongside the other roots.
    mock_container.exec_run.assert_any_call(
        [
            "chown",
            f"{settings.RUN_UID}:{settings.RUN_GID}",
            "--",
            SANDBOX_ROOT,
            WORKDIR_ROOT,
            SANDBOX_HOME,
            SKILLS_ROOT,
        ],
        user="root",
    )
