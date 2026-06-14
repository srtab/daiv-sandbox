from __future__ import annotations

import fnmatch
import io
import logging
import posixpath
import socket
import tarfile
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, NamedTuple

from docker import DockerClient, from_env
from docker.errors import APIError, ImageNotFound, NotFound

from daiv_sandbox.config import settings
from daiv_sandbox.schemas import RunResult

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = logging.getLogger("daiv_sandbox")

# Canonical sandbox root directory inside all containers
WORKSPACE_ROOT = "/workspace"
SANDBOX_ROOT = "/workspace/repo"
SANDBOX_HOME = "/home/daiv-sandbox"
SKILLS_ROOT = "/workspace/skills"
SCRATCH_ROOT = "/workspace/tmp"

# Container label identifying daiv-sandbox cmd-executor containers (used for discovery/reaping).
DAIV_SANDBOX_TYPE_LABEL = "daiv.sandbox.type"
TYPE_CMD_EXECUTOR = "cmd_executor"


class SessionUnavailableError(RuntimeError):
    """A session's container exists but could not be brought to (or left in) the desired state.

    Distinct from a genuinely missing session (which maps to 404): this signals an infrastructure
    fault (Docker daemon/runtime error) while restarting or stopping a container, and is mapped to
    503 so clients retry/alert instead of assuming the session was legitimately gone.
    """

    def __init__(self, session_id: str, action: str):
        super().__init__(f"Session '{session_id}' could not be {action}")
        self.session_id = session_id
        self.action = action


class DirEntry(NamedTuple):
    """A single filesystem entry returned by ``list_dir``/``find_paths``."""

    path: str
    is_dir: bool


class GrepHit(NamedTuple):
    """A single ``grep`` match: absolute path, 1-indexed line number, and the matching line text."""

    path: str
    line: int
    text: str


# Portable pipefail wrapper: uses bash when available (dash lacks pipefail support),
# otherwise falls back to ash/sh which accept `-o pipefail` as a CLI flag.
PIPEFAIL_WRAPPER = (
    'if [ -x /bin/bash ]; then exec /bin/bash -o pipefail -c "$1"; else exec /bin/sh -o pipefail -c "$1"; fi'
)

# Sentinel exit code the fs-primitive shell guards (`list_dir`/`find_paths`/`grep`) emit when the
# target path does not exist. The tools' own "cannot access" exits are ambiguous (`ls`/`grep` use 2
# for both missing and permission denied) and we discard stderr, so an explicit existence test lets
# us map only a true absence to FileNotFoundError. 7 is unused by `test`/`ls`/`grep`/`find` (which
# exit 0/1/2), so it can't collide.
_PATH_ABSENT_EXIT = 7

# Sentinel exits emitted by the fs-primitive shell guards (`_run_path_guarded`, and `delete_file`'s
# own inline test), alongside _PATH_ABSENT_EXIT (7), to disambiguate a real absence from a type
# mismatch or an access failure (the tools discard stderr and reuse exit 2 for several conditions).
# 8 and 9 are unused by test/ls/grep/find, so they can't collide.
_PATH_WRONG_TYPE_EXIT = 8
_PATH_DENIED_EXIT = 9

# Sentinel exit emitted by `grep` when the search pattern is not a valid POSIX ERE. The pattern is
# probed against /dev/null (no workspace file is read) before the real search, so this cleanly
# separates a bad regex (a model-fixable error) from a per-file read error (also exit 2). 6 is
# unused by test/ls/grep/find/xargs and the path-guard sentinels (7/8/9), so it can't collide.
_GREP_BAD_PATTERN_EXIT = 6


def _sh_quote(value: str) -> str:
    """
    Safely quote an arbitrary string for POSIX shell usage.

    This is intentionally tiny (avoid importing shlex just for one call site).
    """
    # POSIX-safe single-quote escaping:  abc'd -> 'abc'"'"'d'
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _prune_predicate(excludes: tuple[str, ...]) -> str:
    """
    Build a busybox-safe ``find`` fragment that prunes directories matching *excludes* by basename.

    Returns ``-type d \\( -name '<a>' -o -name '<b>' … \\) -prune -o`` (each name ``_sh_quote``'d, so
    arbitrary input is shell-safe), or ``""`` when *excludes* is empty so callers fall back to their
    original ``find`` expression. Names are passed to ``find -name``, so each entry is a *basename
    glob* (e.g. ``*.egg-info`` prunes any such directory at any depth). ``-type d`` is placed before
    the name group so non-directory nodes short-circuit the whole conjunction and never run the
    ``-name`` tests (nor the no-op ``-prune``).
    """
    if not excludes:
        return ""
    names = " -o ".join(f"-name {_sh_quote(name)}" for name in excludes)
    return rf"-type d \( {names} \) -prune -o"


def _validate_sandbox_path(path: str, allowed_roots: tuple[str, ...], *, allow_root: bool = False) -> str:
    """
    Lexically validate that *path* is a safe absolute path under one of *allowed_roots*.

    Returns the canonicalised absolute path. Raises ValueError on any rejection.

    By default the bare root itself is rejected (callers want a file/dir *inside* a root). Pass
    ``allow_root=True`` for directory ops (ls/grep/glob) that legitimately target the root itself.
    """
    if "\x00" in path or "\n" in path or "\r" in path:
        raise ValueError(f"path must not contain NUL or newline characters: {path!r}")
    p = PurePosixPath(path)
    if not p.is_absolute():
        raise ValueError(f"path must be absolute, got: {path!r}")
    if ".." in p.parts:
        raise ValueError(f"path must not contain '..' segments: {path!r}")
    canonical = str(p)
    for root in allowed_roots:
        root_norm = root.rstrip("/") or "/"
        if canonical == root_norm:
            if allow_root:
                return canonical
            raise ValueError(f"path must not equal a reserved root: {path!r}")
        if canonical.startswith(f"{root_norm}/"):
            return canonical
    raise ValueError(f"path must be under one of {allowed_roots}, got: {path!r}")


_SINGLE_FILE_TAR_SPOOL_LIMIT = 1 << 20  # 1 MiB
_SANITIZED_ARCHIVE_SPOOL_LIMIT = 8 << 20  # 8 MiB — sanitized seed archives spill past this


def _build_single_file_tar_stream(filename: str, content: bytes, *, mode: int) -> IO[bytes]:
    """
    Build an uncompressed tar containing one regular-file member.

    Returns a seekable file-like object positioned at offset 0. Small archives stay
    in memory; larger ones spill to disk. Caller owns the stream and must close it
    (use as a context manager).
    """
    stream = tempfile.SpooledTemporaryFile(max_size=_SINGLE_FILE_TAR_SPOOL_LIMIT)  # noqa: SIM115
    try:
        with tarfile.open(fileobj=stream, mode="w") as tf:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            info.mode = mode & 0o7777
            info.type = tarfile.REGTYPE
            tf.addfile(info, io.BytesIO(content))
        stream.seek(0)
    except BaseException:
        stream.close()
        raise
    return stream


def _normalize_tar_member_name(name: str) -> str | None:
    """
    Normalize tar member names and reject traversal / absolute paths.

    Returns:
        Normalized POSIX path (no leading "./"), or None for empty/root entries.
    """
    # Strip leading "./" segments (common in tar archives).
    while name.startswith("./"):
        name = name[2:]

    if name in {"", "."}:
        return None

    p = PurePosixPath(name)
    if p.is_absolute():
        raise ValueError(f"Archive contains an absolute path: {name!r}")
    if ".." in p.parts:
        raise ValueError(f"Archive contains a parent-directory traversal path: {name!r}")

    normalized = str(p)
    return None if normalized in {"", "."} else normalized


def _symlink_target_is_safe(name: str, linkname: str, seen_symlinks: set[str]) -> bool:
    """
    Whether a symlink member may be preserved: its target must be relative and *lexically*
    resolve inside the archive root.

    ``name`` is the already-normalized member path; ``linkname`` is the raw tar link
    target, resolved against the member's parent directory (POSIX symlink semantics).

    Resolution is lexical (``..`` collapses path components textually), which is only
    sound when no intermediate component is itself a symlink — ``link/..`` resolves
    relative to the link's *target*, not its parent, so a chain like ``a/b -> ..`` +
    ``c -> a/b/../z`` would lexically pass while really escaping the root. Any target
    whose non-final components traverse a previously emitted symlink is therefore
    rejected outright rather than mis-resolved. A target whose *final* component is a
    symlink is fine (an in-tree link to a link resolves through independently-vetted
    links).
    """
    if not linkname or PurePosixPath(linkname).is_absolute():
        return False
    parent = str(PurePosixPath(name).parent)
    joined = posixpath.join("" if parent == "." else parent, linkname)
    components = [c for c in joined.split("/") if c not in ("", ".")]
    prefix: list[str] = []
    for i, component in enumerate(components):
        if component == "..":
            if not prefix:
                return False  # climbs above the archive root
            prefix.pop()
        else:
            prefix.append(component)
        if i < len(components) - 1 and "/".join(prefix) in seen_symlinks:
            return False  # lexical math is meaningless through a symlink
    return True


def _sanitize_archive_stream(in_stream: IO[bytes], out_stream: IO[bytes], *, uid: int, gid: int) -> int:
    """
    Sanitize an incoming (possibly compressed) tar archive for safer extraction,
    writing an *uncompressed* tar to ``out_stream``. Returns the number of members
    that were skipped (0 when the archive passed through intact) so callers can
    surface a partially seeded workspace instead of discovering it via a dirty tree.

    Escape safety rests on the *combination* of three guards — weakening any one of
    them in isolation re-opens the others:

    - ``_normalize_tar_member_name`` rejects absolute member names and ``..`` segments.
    - ``_symlink_target_is_safe`` admits only relative symlink targets that lexically
      resolve inside the root and don't traverse another symlink mid-path.
    - Members whose path traverses, or whose name collides with, a previously emitted
      symlink are skipped (no writes *through* links and no symlink/file type confusion);
      symmetrically, a symlink whose name was already emitted as a dir or link is skipped
      (first entry wins — duplicate same-name members would make extraction
      extractor-dependent).

    Preserved symlinks keep their linkname (repos legitimately track them; dropping them
    dirties the seeded git tree). Hardlinks, device nodes, and FIFOs are always rejected.

    Other normalizations:

    - Ownership is stamped to the sandbox uid/gid.
    - Permissions are normalized similar to: chmod -R a+rX,u+w
    - A sandbox-owned entry is synthesized for every missing ancestor directory, so extraction
      never auto-creates a root-owned (sandbox-unwritable) intermediate directory.

    Both streams are consumed/written from their current positions. The caller must
    seek ``in_stream`` to the desired start offset before calling this function.
    ``out_stream`` is not seeked back afterward.

    Callers extract the result with ``put_archive``, which preserves each member's uid/gid/mode — so
    this is the single point where ownership/permissions are established (no post-extraction
    ``chmod``/``chown`` pass is needed).
    """
    seen_dirs: set[str] = set()
    seen_symlinks: set[str] = set()
    total_members = 0
    skipped: list[str] = []

    def _skip(member_name: str, detail: str) -> None:
        skipped.append(member_name)
        logger.warning("Skipping archive entry %r: %s", member_name, detail)

    def _traverses_symlink(name: str) -> bool:
        parts = name.split("/")[:-1]
        return any("/".join(parts[:i]) in seen_symlinks for i in range(1, len(parts) + 1))

    def _emit_dir(out_tf: tarfile.TarFile, dirpath: str, mode: int) -> None:
        info = tarfile.TarInfo(name=dirpath)
        info.type = tarfile.DIRTYPE
        info.size = 0
        info.mode = mode
        info.uid = uid
        info.gid = gid
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        out_tf.addfile(info)
        seen_dirs.add(dirpath)

    def _ensure_parents(out_tf: tarfile.TarFile, name: str) -> None:
        # Emit a sandbox-owned, traversable entry for each not-yet-seen ancestor of *name*, top-down,
        # so a tar that omits explicit directory entries can't leave put_archive to auto-create them
        # root-owned. 0o755 = rwxr-xr-x (the a+rX,u+w dir baseline).
        parts = name.split("/")[:-1]  # drop the leaf component
        for i in range(1, len(parts) + 1):
            ancestor = "/".join(parts[:i])
            if ancestor and ancestor not in seen_dirs:
                _emit_dir(out_tf, ancestor, 0o755)

    try:
        with tarfile.open(fileobj=in_stream, mode="r:*") as in_tf, tarfile.open(fileobj=out_stream, mode="w") as out_tf:
            for member in in_tf:
                normalized_name = _normalize_tar_member_name(member.name)
                if normalized_name is None:
                    continue
                total_members += 1

                if not (member.isfile() or member.isdir() or member.issym()):
                    _skip(member.name, f"unsupported type {member.type!r}; only files/dirs/symlinks are allowed")
                    continue

                if _traverses_symlink(normalized_name):
                    _skip(member.name, "path traverses a symlinked directory")
                    continue

                if not member.issym() and normalized_name in seen_symlinks:
                    # A same-named file/dir after a symlink would put two members with conflicting
                    # types in the output tar; extraction is then extractor-dependent (Docker's
                    # untar deletes the existing path). First entry wins, consistently.
                    _skip(member.name, "name collides with a previously emitted symlink")
                    continue

                if member.issym():
                    if normalized_name in seen_dirs or normalized_name in seen_symlinks:
                        # The dir case includes parents synthesized for an earlier out-of-order
                        # child (`alias/file.txt` listed before `alias -> real`): emitting the
                        # symlink anyway would make Docker's untar delete the dir and its contents.
                        _skip(member.name, "symlink name collides with a previously emitted directory or symlink")
                        continue
                    if not _symlink_target_is_safe(normalized_name, member.linkname, seen_symlinks):
                        _skip(member.name, f"symlink target {member.linkname!r} escapes the archive root")
                        continue
                    _ensure_parents(out_tf, normalized_name)
                    out_info = tarfile.TarInfo(name=normalized_name)
                    out_info.type = tarfile.SYMTYPE
                    out_info.linkname = member.linkname
                    out_info.size = 0
                    # Ignored on Linux; access through a symlink is governed by the target's perms.
                    out_info.mode = 0o777
                    out_info.uid = uid
                    out_info.gid = gid
                    out_info.uname = ""
                    out_info.gname = ""
                    out_info.mtime = 0
                    out_tf.addfile(out_info)
                    seen_symlinks.add(normalized_name)
                    continue

                _ensure_parents(out_tf, normalized_name)

                # Mirror `chmod -R a+rX,u+w` semantics while clearing special bits.
                base_mode = member.mode & 0o777
                base_mode &= ~0o7000  # clear suid/sgid/sticky

                mode = base_mode | 0o444 | 0o200  # a+r, u+w
                if member.isdir() or (base_mode & 0o111):  # a+X
                    mode |= 0o111
                else:
                    mode &= ~0o111

                if member.isdir():
                    if normalized_name not in seen_dirs:  # not already emitted as a synthesized ancestor
                        _emit_dir(out_tf, normalized_name, mode)
                    continue

                # Regular file
                extracted = in_tf.extractfile(member)
                if extracted is None:
                    raise ValueError(f"Failed to read file entry from archive: {member.name!r}")

                out_info = tarfile.TarInfo(name=normalized_name)
                out_info.uid = uid
                out_info.gid = gid
                out_info.uname = ""
                out_info.gname = ""
                out_info.mtime = 0
                out_info.mode = mode
                out_info.type = tarfile.REGTYPE
                out_info.size = member.size
                try:
                    out_tf.addfile(out_info, fileobj=extracted)
                finally:
                    extracted.close()
    except (tarfile.TarError, EOFError, OSError) as e:
        raise ValueError(f"Invalid or truncated archive: {e}") from e

    if skipped:
        # The per-member WARNINGs above are easy to lose in log noise, and the symptom of a
        # partially seeded repo (a dirty git tree polluting every captured patch) surfaces far
        # away, on the client side. One high-severity, greppable summary per archive.
        logger.error(
            "Archive sanitization dropped %d of %d members: %s",
            len(skipped),
            total_members,
            ", ".join(skipped[:20]) + (", …" if len(skipped) > 20 else ""),
        )
    return len(skipped)


class SandboxDockerSession:
    """
    A session is a Docker container that is used to execute commands.
    """

    _shared_client: DockerClient | None = None
    _client_lock: threading.Lock = threading.Lock()

    @classmethod
    def _get_shared_client(cls) -> DockerClient:
        """Return a lazily-initialized, reused Docker client."""
        if cls._shared_client is None:
            with cls._client_lock:
                if cls._shared_client is None:
                    cls._shared_client = from_env()
        return cls._shared_client

    def __init__(self, session_id: str | None = None, client: DockerClient | None = None):
        """
        Create a new sandbox session using Docker.

        Args:
            client: Docker client, if not provided, the shared client will be reused.
        """
        self.session_id: str | None = session_id
        self.client: DockerClient = client or self._get_shared_client()
        self.container: Container | None = self._get_container(session_id) if session_id else None

    @classmethod
    def ping(cls, *, client: DockerClient | None = None) -> bool:
        """
        Ping the Docker client.

        Args:
            client (DockerClient | None): Docker client, if not provided, the shared client will be reused.

        Returns:
            bool: True if the client is pingable, False otherwise.
        """
        return cls(client=client)._ping()

    @classmethod
    def start(cls, image: str, *, client: DockerClient | None = None, **kwargs) -> SandboxDockerSession:
        """
        Start a new session by pulling the image and creating a new container.

        Args:
            image (str): Docker image to use.
            client (DockerClient | None): Docker client, if not provided, the shared client will be reused.

        Returns:
            SandboxDockerSession: The session object.
        """
        instance = cls(client=client)

        instance._pull_image(image)
        instance._start_container(image, **kwargs)
        return instance

    def _ping(self) -> bool:
        """
        Ping the Docker client.
        """
        return self.client.ping()

    def _pull_image(self, image: str):
        """
        Pull the image from the registry.

        Args:
            image (str): The tag of the image to pull.
        """
        try:
            found_image = self.client.images.get(image)
            logger.info("Found already existing image '%s'", found_image.tags[-1])
        except ImageNotFound:
            logger.info("Pulling image '%s'", image)
            self.client.images.pull(image)

    def _start_container(self, image: str, **kwargs):
        """
        Create a new container from the image.

        Args:
            image (str): The tag of the image to use.
        """
        if "user" in kwargs:
            raise ValueError("Sandbox containers always run as a non-root user; overriding `user` is not allowed.")

        # gVisor's netstack can't reach Docker's embedded DNS resolver (127.0.0.11) that a user-defined
        # network injects, so a cmd-executor attached to one resolves nothing. When that's the case,
        # resolve sibling services ourselves and inject them as static /etc/hosts entries, then (after
        # start) point resolv.conf at real upstreams. runc honours the embedded resolver, so it needs none
        # of this; and without an explicit `network` (Docker's default bridge) resolv.conf already carries
        # real upstreams, so the gVisor failure mode doesn't apply.
        fix_gvisor_dns = bool(kwargs.get("network")) and settings.RUNTIME == "runsc"
        if fix_gvisor_dns and settings.EXTRA_HOSTS:
            kwargs.setdefault("extra_hosts", {}).update(self._resolve_extra_hosts(settings.EXTRA_HOSTS))

        container = self.client.containers.run(
            image,
            entrypoint="/bin/sh",
            command=["-lc", "sleep infinity"],  # long-lived; reaper owns the lifetime
            detach=True,
            tty=True,
            runtime=settings.RUNTIME,
            user=self._get_user(),
            **kwargs,
        )

        self.session_id = container.id
        self.container = container

        logger.info("Container '%s' started (status: %s)", container.short_id, container.status)

        # Bootstrap the directory layout. On failure, force-remove so a failed start() leaks nothing
        # (no leaked container holding its runtime/cpu/memory reservations).
        sandbox_dirs = [WORKSPACE_ROOT, SANDBOX_ROOT, SANDBOX_HOME, SKILLS_ROOT, SCRATCH_ROOT]
        try:
            # Ensure the sandbox directories exist and are writable by the sandbox user.
            mkdir_result = container.exec_run(["mkdir", "-p", "--", *sandbox_dirs], user="root")
            if mkdir_result.exit_code != 0:
                raise RuntimeError(
                    f"Failed to create sandbox directories in {container.short_id}: "
                    f"(exit_code: {mkdir_result.exit_code}) -> {mkdir_result.output}"
                )

            chown_result = container.exec_run(["chown", self._get_user(), "--", *sandbox_dirs], user="root")
            if chown_result.exit_code != 0:
                raise RuntimeError(
                    f"Failed to chown sandbox directories in {container.short_id}: "
                    f"(exit_code: {chown_result.exit_code}) -> {chown_result.output}"
                )

            if fix_gvisor_dns:
                self._override_resolv_conf(container, settings.DNS)
        except Exception:
            try:
                container.remove(force=True)
            except Exception:
                logger.warning("Failed to remove container %s after bootstrap failure", container.short_id)
            raise

    def _resolve_extra_hosts(self, hostnames: list[str]) -> dict[str, str]:
        """Resolve sibling-service names to IPs for injection as static cmd-executor /etc/hosts entries.

        gVisor cmd-executors can't use Docker's embedded DNS (127.0.0.11), so compose-service names
        (e.g. "gitlab") are pinned in /etc/hosts instead. The daiv-sandbox service container itself runs
        under the default runtime on the same network, so its own resolver maps these names to the IPs
        the executor will reach. Unresolvable names are skipped with a warning rather than failing the
        whole session start.
        """
        resolved: dict[str, str] = {}
        for name in hostnames:
            try:
                resolved[name] = socket.gethostbyname(name)
            except OSError:
                logger.warning("Could not resolve sibling host %r for cmd-executor /etc/hosts; skipping", name)
        return resolved

    def _override_resolv_conf(self, container: Container, nameservers: list[str]) -> None:
        """Repoint a cmd-executor's DNS at real upstream resolvers.

        gVisor's netstack can't reach the embedded Docker resolver (127.0.0.11) that user-defined
        networks inject, so name resolution fails outright. Overwrite resolv.conf with real recursive
        resolvers; sibling-service names are handled separately via static /etc/hosts entries.
        """
        content = "".join(f"nameserver {ns}\n" for ns in nameservers)
        result = container.exec_run(["sh", "-c", f"printf '%s' {_sh_quote(content)} > /etc/resolv.conf"], user="root")
        if result.exit_code != 0:
            raise RuntimeError(
                f"Failed to override resolv.conf in {container.short_id}: "
                f"(exit_code: {result.exit_code}) -> {result.output}"
            )

    def remove_container(self):
        """
        Remove the container.

        """
        try:
            self.client.containers.get(self.session_id).remove(force=True)
        except NotFound:
            logger.warning("Container '%s' not found", self.session_id)
        else:
            logger.info("Container '%s' removed", self.session_id)

    def stop_container(self):
        """
        Stop the container without removing it, preserving its writable layer for warm reuse.

        Idempotent: a missing container is logged and ignored; an already-stopped container is a
        Docker no-op. PID 1 is ``sleep``, which ignores SIGTERM, so ``stop`` waits the (small)
        ``STOP_TIMEOUT_SECONDS`` and then SIGKILLs — the filesystem is preserved either way.

        A container that vanished between the lookup and the stop counts as already-stopped. Any
        other Docker error (daemon busy, stop conflict) is raised as ``SessionUnavailableError`` so
        the DELETE endpoint returns 503 rather than a bare 500 — the session may still be running.
        """
        try:
            container = self.client.containers.get(self.session_id)
        except NotFound:
            logger.warning("Container '%s' not found", self.session_id)
            return

        try:
            container.stop(timeout=settings.STOP_TIMEOUT_SECONDS)
        except NotFound:
            # Removed between the lookup and the stop: a stop is already satisfied.
            logger.warning("Container '%s' vanished before it could be stopped", self.session_id)
            return
        except APIError as exc:
            logger.exception("Failed to stop container '%s'", self.session_id)
            raise SessionUnavailableError(self.session_id, "stopped") from exc
        logger.info("Container '%s' stopped", self.session_id)

    def copy_to_container(self, tardata: IO[bytes], dest: str | None = None, clear_before_copy: bool = True):
        """
        Copy a file or directory to a specific path in the container.

        Args:
            tardata (IO[bytes]): Seekable tar archive to copy. Must be positioned at offset 0 or
                pre-seeked; the method rewinds it before sanitization.
            dest (str | None): Destination path inside the container. Defaults to SANDBOX_ROOT.
            clear_before_copy (bool): Clear the destination directory before extracting.
        """
        tardata.seek(0)

        # Resolve destination: default to SANDBOX_ROOT, absolute stays absolute, relative resolves under SANDBOX_ROOT
        to_dir = SANDBOX_ROOT
        if dest:
            to_dir = dest if Path(dest).is_absolute() else (Path(SANDBOX_ROOT) / dest).as_posix()

        to_dir_norm = to_dir.rstrip("/") or "/"
        if to_dir_norm in {"", "/"}:
            raise ValueError("Refusing to extract an archive into the container root directory")

        # Confine the destination to WORKSPACE_ROOT (SANDBOX_ROOT/SKILLS_ROOT/SCRATCH_ROOT all live
        # under it, so the single /workspace prefix subsumes them). Validate lexically through the
        # shared validator so a `..`/NUL/newline in `dest` is rejected at this boundary rather than
        # relying on every caller pre-validating. allow_root=True permits a bare-root dest.
        # (The seed marker lives in the sandbox home, outside /workspace — written there via exec, not
        # copied — so it stays container-local and out of reach of the fs/* endpoints.)
        try:
            _validate_sandbox_path(to_dir_norm, allowed_roots=(WORKSPACE_ROOT,), allow_root=True)
        except ValueError as exc:
            raise ValueError(f"Refusing to extract an archive outside of {WORKSPACE_ROOT!r}: {exc}") from exc

        if clear_before_copy:
            q = _sh_quote(to_dir_norm)
            rm_cmd = f"rm -rf -- {q}/* {q}/.[!.]* {q}/..?* 2>/dev/null || true"
            rm_result = self.container.exec_run(["/bin/sh", "-c", rm_cmd], user="root")
            if rm_result.exit_code != 0:
                raise RuntimeError(
                    f"Failed to clear directory {self.container.short_id}:{to_dir_norm}: "
                    f"(exit_code: {rm_result.exit_code}) -> {rm_result.output}"
                )

        # Create the dest as the sandbox user, so a freshly-created directory is already owned
        # correctly and needs no follow-up chown. Within /workspace the sandbox user owns the tree
        # (set at container bootstrap), so this always succeeds for the real seed destinations.
        mkdir_result = self.container.exec_run(["mkdir", "-p", "--", to_dir_norm], user=self._get_user())
        if mkdir_result.exit_code != 0:
            raise RuntimeError(
                f"Failed to create directory {self.container.short_id}:{to_dir_norm}: "
                f"(exit_code: {mkdir_result.exit_code}) -> {mkdir_result.output}"
            )

        with tempfile.SpooledTemporaryFile(max_size=_SANITIZED_ARCHIVE_SPOOL_LIMIT) as sanitized:
            _sanitize_archive_stream(tardata, sanitized, uid=settings.RUN_UID, gid=settings.RUN_GID)
            sanitized.seek(0)
            put_ok = self.container.put_archive(to_dir_norm, sanitized)

        if not put_ok:
            raise RuntimeError(f"Failed to copy archive to {self.container.short_id}:{to_dir_norm}")

        # No post-copy `chmod -R`/`chown -R`: `_sanitize_archive_stream` already stamps every member
        # with the sandbox uid/gid and normalized `a+rX,u+w` permissions (special bits cleared), and
        # `put_archive` preserves the tar's ownership/mode on extraction. The recursive passes that
        # used to walk the whole extracted tree — thousands of files for a large seed — were therefore
        # redundant and have been removed. (The archive also rejects symlinks/hardlinks, so nothing
        # in it can point outside the tree.)

    def execute_command(self, command: str, workdir: str | None = None) -> RunResult:
        """
        Execute a command in the container.

        Args:
            command (str): The command to execute.
            workdir (str | None): The working directory of the command. Defaults to SANDBOX_ROOT.

        Returns:
            RunResult: The result of the command.
        """
        # Resolve workdir: default to SANDBOX_ROOT, absolute stays absolute, relative resolves under SANDBOX_ROOT
        command_workdir = SANDBOX_ROOT
        if workdir:
            command_workdir = workdir if Path(workdir).is_absolute() else (Path(SANDBOX_ROOT) / workdir).as_posix()

        logger.info("Executing command in %s:%s -> '%s'", self.container.short_id, command_workdir, command)

        result = self.container.exec_run(
            ["/bin/sh", "-c", PIPEFAIL_WRAPPER, "--", command],
            workdir=command_workdir,
            user=self._get_user(),
            environment=self._get_exec_environment(),
        )

        # Decode the output to UTF-8, replacing invalid characters with U+FFFD. This is to avoid raising an exception
        # when the output contains invalid characters.
        output = result.output.decode("utf-8", errors="replace")

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Command in %s:%s exited with code %s: %s",
                self.container.short_id,
                command_workdir,
                result.exit_code,
                output,
            )
        elif result.exit_code != 0:
            logger.warning(
                "Command in %s:%s exited with code %s", self.container.short_id, command_workdir, result.exit_code
            )

        return RunResult(command=command, output=output, exit_code=result.exit_code, workdir=command_workdir)

    def _run_path_guarded(self, path: str, body: str, *, require: str | None = None) -> RunResult:
        """Run *body* only if *path* exists (and, when *require* is given, has the expected type and
        is accessible), mapping each failure to a distinct exception.

        Prepends shell tests that exit with sentinel codes: missing -> _PATH_ABSENT_EXIT
        (FileNotFoundError); ``require="dir"`` but not a directory -> _PATH_WRONG_TYPE_EXIT
        (NotADirectoryError); existing-but-inaccessible -> _PATH_DENIED_EXIT (PermissionError). This
        disambiguates a true absence/type-mismatch from a tool's own "cannot access" exit since stderr
        is discarded. *body* must already quote *path* itself. Permission detection is best-effort:
        deeper nested failures fall through to the tool's own non-zero exit.
        """
        quoted = _sh_quote(path)
        prologue = f"[ -e {quoted} ] || exit {_PATH_ABSENT_EXIT}; "
        if require == "dir":
            prologue += f"[ -d {quoted} ] || exit {_PATH_WRONG_TYPE_EXIT}; "
            prologue += f"{{ [ -r {quoted} ] && [ -x {quoted} ]; }} || exit {_PATH_DENIED_EXIT}; "
        else:
            prologue += f"[ -r {quoted} ] || exit {_PATH_DENIED_EXIT}; "
        result = self.execute_command(prologue + body)
        if result.exit_code == _PATH_ABSENT_EXIT:
            raise FileNotFoundError(path)
        if result.exit_code == _PATH_WRONG_TYPE_EXIT:
            raise NotADirectoryError(path)
        if result.exit_code == _PATH_DENIED_EXIT:
            raise PermissionError(path)
        return result

    def write_file(
        self,
        path: str,
        content: bytes,
        *,
        mode: int,
        allowed_roots: tuple[str, ...] = (SANDBOX_ROOT,),
        create_only: bool = False,
    ) -> None:
        """
        Write *content* to *path* (absolute, under one of *allowed_roots*) inside the container.

        The path is validated lexically. The file is shipped as a single sanitized tar member
        through the Docker archive API (``put_archive``), which preserves the member's uid/gid/mode —
        so sandbox-user ownership and the normalized ``a+rX,u+w`` permissions are baked into the tar
        by ``_sanitize_archive_stream`` and need no post-copy ``chmod``/``chown`` round-trips.

        When *create_only* is True (the ``fs/write`` contract, matching deepagents'), a single exec —
        run as the sandbox user — both refuses an existing path and ensures the parent directory,
        folding what used to be a separate existence probe plus ``mkdir`` (plus the now-redundant
        recursive ``chmod``/``chown``). There is an inherent TOCTOU window between that check and the
        write. When *create_only* is False (``edit_file``'s read-modify-write back), the parent is
        assumed to already exist, so no exec runs at all — just the archive copy.
        """
        canonical = _validate_sandbox_path(path, allowed_roots=allowed_roots)
        parent_dir, _, filename = canonical.rpartition("/")
        if not parent_dir or not filename:
            raise ValueError(f"path resolves to an unusable location: {path!r}")

        if create_only:
            # One round-trip (as the sandbox user): reject an existing path, otherwise create the
            # parent. Fail *closed* — an unrecognised marker or non-zero exit must raise (caught and
            # logged by fs_write) rather than be mistaken for "absent" and clobber the file. Creating
            # the parent as the sandbox user means it needs no follow-up chown.
            q_file, q_parent = _sh_quote(canonical), _sh_quote(parent_dir)
            staged = self.execute_command(
                f"if [ -e {q_file} ]; then printf EXISTS; exit 0; fi; "
                f"mkdir -p -- {q_parent} || {{ printf MKDIR_FAILED; exit 1; }}; printf OK"
            )
            marker = staged.output.strip()
            if staged.exit_code == 0 and marker == "EXISTS":
                raise FileExistsError(
                    f"Cannot write to {path} because it already exists. "
                    "Read and then make an edit, or write to a new path."
                )
            if staged.exit_code != 0 or marker != "OK":
                raise RuntimeError(
                    f"write staging failed for {path!r} (exit {staged.exit_code}, output {staged.output!r})"
                )

        with (
            _build_single_file_tar_stream(filename, content, mode=mode) as raw_tar,
            tempfile.SpooledTemporaryFile(max_size=_SINGLE_FILE_TAR_SPOOL_LIMIT) as sanitized,
        ):
            _sanitize_archive_stream(raw_tar, sanitized, uid=settings.RUN_UID, gid=settings.RUN_GID)
            sanitized.seek(0)
            if not self.container.put_archive(parent_dir, sanitized):
                raise RuntimeError(f"Failed to write {self.container.short_id}:{canonical}")

    def read_file_bytes(self, path: str) -> bytes:
        """Return the raw bytes of a single file via the Docker archive API.

        Raises FileNotFoundError when the path does not exist, and IsADirectoryError when it is a
        directory. A genuinely empty file returns ``b""`` (it is not treated as missing).
        """
        try:
            bits, _stat = self.container.get_archive(path)
        except NotFound as exc:
            # get_archive raises docker.errors.NotFound for a missing path (it is NOT a
            # FileNotFoundError); translate so endpoints' FileNotFoundError handling works.
            raise FileNotFoundError(path) from exc
        raw = b"".join(bits)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tf:
            members = tf.getmembers()
            if members and members[0].isdir():
                # get_archive on a directory returns its whole subtree; refuse rather than return an
                # arbitrary inner file's bytes as if they were this path's content.
                raise IsADirectoryError(path)
            files = [m for m in members if m.isfile()]
            if not files:
                raise FileNotFoundError(path)
            extracted = tf.extractfile(files[0])
            if extracted is None:
                raise FileNotFoundError(path)
            return extracted.read()

    def list_dir(self, path: str) -> list[DirEntry]:
        """List one directory level. Uses `ls -1Ap` (portable; dirs get a trailing '/').

        Goes through the path guard (require="dir"), so it raises FileNotFoundError when the path
        does not exist, NotADirectoryError when it exists but is not a directory, and PermissionError
        when the directory is not readable/traversable by the sandbox user. RuntimeError is raised
        only when `ls` itself fails for some other reason after the guard passes. All are distinct
        from a real but empty directory, which returns [].
        """
        quoted = _sh_quote(path)
        result = self._run_path_guarded(path, f"ls -1Ap -- {quoted} 2>/dev/null", require="dir")
        if result.exit_code != 0:
            raise RuntimeError(f"ls failed (exit {result.exit_code}) for {path!r}")
        entries: list[DirEntry] = []
        base = path.rstrip("/")
        for line in result.output.splitlines():
            name = line.rstrip("\n")
            if not name:
                continue
            is_dir = name.endswith("/")
            clean = name[:-1] if is_dir else name
            entries.append(DirEntry(f"{base}/{clean}", is_dir))
        return entries

    def grep(self, pattern: str, path: str, glob: str | None, excludes: tuple[str, ...] = ()) -> list[GrepHit]:
        """Literal recursive search. Returns GrepHit(path, line, text).

        For a directory target, files are enumerated with a prune-aware ``find`` and piped to
        ``grep`` (``--exclude-dir`` is GNU-only, so busybox can't prune in grep itself); this makes
        grep skip *opening* files under excluded directories rather than reading and discarding them.
        For a single-file target, grep runs directly.

        `glob`, when given, restricts results to files whose basename matches it (applied host-side;
        busybox grep has no `--include`). `excludes` (directory basenames/globs) are pruned via
        ``_prune_predicate``.

        Read-error contract: a file ``grep`` is asked to search but cannot read (EACCES, or a file
        that vanished in the find->grep window) is surfaced, not swallowed. ``xargs`` collapses
        grep's exit 1 (no match) and 2 (read error) into a single 123, so the read error can't be
        told apart from the exit code alone; instead grep's stderr is captured into a shell variable
        (matches still flow to stdout via fd 3) and a non-empty capture forces exit 2 -> RuntimeError.
        This matches the previous ``grep -rHnF`` (which exited 2 on a per-file read error) and the
        single-file branch below (which raises on grep's own exit >= 2). An unreadable *subdirectory*
        met while traversing is still silently skipped: ``find``'s stderr is discarded, so it is
        never enumerated and never reaches grep. With no read error, exit 0 (match) and 123 (no
        match) both parse the output (empty -> []); any other code (e.g. 127 grep-missing) raises.
        The path guard (require=None) still raises FileNotFoundError on a true absence and
        PermissionError on an unreadable *target*, distinct from grep's own exits.
        """
        quoted = _sh_quote(path)
        quoted_pattern = _sh_quote(pattern)
        prune = _prune_predicate(excludes)
        body = (
            f"if [ -d {quoted} ]; then "
            # Capture grep's stderr (per-file read errors) into `errs` while matches flow to the real
            # stdout via fd 3; `find`'s own stderr stays discarded, so an unreadable *subdirectory* is
            # silently skipped, but a file grep cannot read leaves a message in `errs`. fd 3 is opened
            # in *this* shell with `exec 3>&1` (then closed afterwards) rather than via a `3>&1`
            # redirection on the assignment: bash does not make a redirection attached to an
            # assignment-only command visible inside its command substitution, so the inline form
            # left fd 3 closed under bash (`1>&3` -> "Bad file descriptor"), swallowing every match
            # and forcing a spurious exit 2. `exec 3>&1` works identically across bash, dash and
            # busybox ash. fd 3 must be closed *after* `ec=$?` so the close can't clobber the status.
            f"exec 3>&1; "
            f"errs=$({{ {{ find {quoted} -mindepth 1 {prune} -type f -print0 2>/dev/null || true; }} "
            f"| xargs -0 grep -HnF -e {quoted_pattern} /dev/null; }} 2>&1 1>&3 3>&-); ec=$?; exec 3>&-; "
            # A non-empty `errs` means a searched file couldn't be read -> surface it as exit 2.
            f'[ -z "$errs" ] || exit 2; '
            # No read error: xargs collapsed grep's 1 (no match) and 2 into 123, so 0/123 are both
            # success here; anything else (e.g. 127 grep-missing) becomes exit 2 -> RuntimeError.
            f'[ "$ec" -eq 0 ] || [ "$ec" -eq 123 ] || exit 2; '
            f"else grep -HnF -e {quoted_pattern} -- {quoted} 2>/dev/null; fi"
        )
        result = self._run_path_guarded(path, body)
        if result.exit_code >= 2:
            raise RuntimeError(f"grep failed (exit {result.exit_code}) for {path!r}")
        matches: list[GrepHit] = []
        for line in result.output.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                file_path, line_no, text = parts[0], int(parts[1]), parts[2]
                if glob is None or fnmatch.fnmatchcase(PurePosixPath(file_path).name, glob):
                    matches.append(GrepHit(file_path, line_no, text))
        return matches

    def find_paths(self, path: str, excludes: tuple[str, ...] = ()) -> list[DirEntry]:
        """Recursively enumerate entries under `path` via POSIX `find` (for glob matching).

        Uses a busybox-safe type-marker scheme (GNU `find -printf` is unavailable on
        images like alpine): directories are suffixed with ``/D`` and files with ``/F``.
        *excludes* (directory basenames/globs) are pruned via ``_prune_predicate`` so their
        subtrees are never descended, enumerated, or transported. Goes through the path guard
        (require="dir"), so it raises FileNotFoundError when the path does not exist,
        NotADirectoryError when it exists but is not a directory, and PermissionError when it is
        not readable/traversable. RuntimeError is raised only when the traversal itself genuinely
        fails after the guard passes.
        """
        quoted = _sh_quote(path)
        prune = _prune_predicate(excludes)
        body = (
            f"{{ find {quoted} -mindepth 1 {prune} -type d -print 2>/dev/null | sed 's/$/\\/D/'; "
            f"find {quoted} -mindepth 1 {prune} ! -type d -print 2>/dev/null | sed 's/$/\\/F/'; }}"
        )
        result = self._run_path_guarded(path, body, require="dir")
        if result.exit_code != 0:
            raise RuntimeError(f"find failed (exit {result.exit_code}) for {path!r}")
        out: list[DirEntry] = []
        for line in result.output.splitlines():
            if line.endswith("/D"):
                out.append(DirEntry(line[:-2], True))
            elif line.endswith("/F"):
                out.append(DirEntry(line[:-2], False))
        return out

    def edit_file(self, path: str, old: str, new: str, replace_all: bool, *, allowed_roots: tuple[str, ...]) -> int:
        """Read, replace, and write back a text file. CRLF-aware. Returns occurrence count.

        Raises FileNotFoundError or UnicodeDecodeError, or a ValueError carrying a human-readable
        message: ``"string_not_found"``, an EOF-newline mismatch hint (when ``old`` carries a
        trailing newline the file lacks at EOF), or a multiple-occurrences message that includes the
        count. ``fs_edit`` forwards the ValueError text to the caller verbatim.
        """
        raw = self.read_file_bytes(path)
        text = raw.decode("utf-8")  # UnicodeDecodeError → not a text file
        # Match-driven CRLF handling: try the literal old, then a CRLF-normalized form, then LF.
        old_crlf = old.replace("\r\n", "\n").replace("\n", "\r\n")
        old_lf = old.replace("\r\n", "\n")
        new_crlf = new.replace("\r\n", "\n").replace("\n", "\r\n")
        new_lf = new.replace("\r\n", "\n")
        count = 0
        matched_old, matched_new = old, new
        for cand_old, cand_new in ((old, new), (old_crlf, new_crlf), (old_lf, new_lf)):
            c = text.count(cand_old)
            if c >= 1:
                matched_old, matched_new, count = cand_old, cand_new, c
                break
        if count == 0:
            # EOF-newline mismatch hint (port of deepagents perform_string_replacement): the model
            # appended a terminator `old` carries but the file lacks at EOF. Compare on LF-normalized
            # forms so a CRLF file is handled the same way the variant loop above does.
            text_lf = text.replace("\r\n", "\n")
            if old_lf.endswith("\n") and len(old_lf) > 1 and text_lf.endswith(old_lf.removesuffix("\n")):
                stripped = old_lf.removesuffix("\n")
                stripped_count = text_lf.count(stripped)
                if stripped_count == 1:
                    raise ValueError(
                        "old_string ends with a newline, but the file does not end with a newline. "
                        "Retry with the trailing newline removed from old_string "
                        "(and from new_string if it also ends with a newline)."
                    )
                raise ValueError(
                    f"old_string ends with a newline, but the file does not end with a newline. "
                    f"With the trailing newline removed, old_string would appear {stripped_count} "
                    f"times in the file. Retry with the trailing newline removed and add surrounding "
                    f"context so the match is unique."
                )
            raise ValueError("string_not_found")
        if count > 1 and not replace_all:
            raise ValueError(
                f"String appears {count} times in file. Use replace_all=True to replace all instances, "
                f"or provide a more specific string with surrounding context."
            )
        result = text.replace(matched_old, matched_new) if replace_all else text.replace(matched_old, matched_new, 1)
        self.write_file(path, result.encode("utf-8"), mode=0o644, allowed_roots=allowed_roots)
        return count

    def delete_file(self, path: str) -> bool:
        """Remove a non-directory path (regular file, symlink, FIFO, etc.). Returns True if something
        was removed, False if the path was already absent (idempotent). Raises IsADirectoryError when
        the path is a directory — delete refuses directories.
        """
        quoted = _sh_quote(path)
        result = self.execute_command(
            f"if [ -d {quoted} ]; then exit {_PATH_WRONG_TYPE_EXIT}; "
            f"elif [ -e {quoted} ]; then rm -f -- {quoted}; "
            f"else exit {_PATH_ABSENT_EXIT}; fi"
        )
        if result.exit_code == _PATH_WRONG_TYPE_EXIT:
            raise IsADirectoryError(path)
        if result.exit_code == _PATH_ABSENT_EXIT:
            return False
        if result.exit_code != 0:
            raise RuntimeError(f"rm failed: {result.output}")
        return True

    def _get_user(self) -> str:
        """
        Get the user to execute sandbox commands as.
        """
        return f"{settings.RUN_UID}:{settings.RUN_GID}"

    def _get_exec_environment(self) -> dict[str, str]:
        """
        Provide a writable HOME/XDG environment for sandboxed commands.

        This avoids failures when HOME is unset, set to '/', or non-writable.
        """
        home = SANDBOX_HOME
        return {
            "HOME": home,
            "XDG_CACHE_HOME": f"{home}/.cache",
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_STATE_HOME": f"{home}/.local/state",
            "XDG_DATA_HOME": f"{home}/.local/share",
        }

    def _get_container(self, session_id: str) -> Container | None:
        """
        Get the container by ID. If the container is not running, attempt to restart it.

        Returns the container when it exists and is running. Returns ``None`` for a genuinely missing
        session (so the endpoint maps it to 404) — including a container that vanished mid-restart, or
        one that does not come back up after a clean restart. Raises ``SessionUnavailableError`` when
        the restart itself fails with a Docker fault (daemon/runtime error): that is an infrastructure
        problem, not a missing session, so the endpoint must surface 503 rather than masking it as 404.
        """
        try:
            container = self.client.containers.get(session_id)
        except NotFound:
            return None

        if container.status == "running":
            return container

        logger.warning(
            "Container '%s' is not running (status: %s). Attempting to restart...", container.short_id, container.status
        )
        try:
            container.restart()
            container.reload()
        except NotFound:
            # Removed between the lookup and the restart: treat as a missing session (404).
            return None
        except Exception as exc:
            # A restart fault is an infrastructure error, not a missing session. Surface it so the
            # endpoint returns 503 instead of a misleading 404 that makes clients spin up new
            # containers against a degraded daemon.
            logger.exception("Failed to restart container %s", container.short_id)
            raise SessionUnavailableError(session_id, "restarted") from exc

        if container.status != "running":
            # Restart didn't raise but the container still isn't up. This is not a daemon fault, so
            # keep the benign behavior: report it as a session that can't be warmed (404).
            logger.error("Failed to restart container %s. Current status: %s", container.short_id, container.status)
            return None

        return container
