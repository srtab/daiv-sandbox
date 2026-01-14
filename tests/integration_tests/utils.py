import base64
import io
import os
import shutil
import subprocess  # noqa: S404
import tarfile
import tempfile
from pathlib import Path


def make_tar_gz(files: dict[str, bytes]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(content)
            tf.addfile(ti, io.BytesIO(content))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def make_tar_gz_with_git(
    files: dict[str, bytes] | None = None, *, commit_message: str = "Initial commit", default_branch: str = "main"
) -> str:
    """
    Create a .tar.gz archive containing a valid git repository at its root.

    The returned value is a base64-encoded string suitable for passing as the
    `archive` field in integration tests (the sandbox will extract it into its
    working directory).
    """
    files = files or {"README.md": b"# test repo\n"}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        git_bin = shutil.which("git") or "git"

        for rel_path, content in files.items():
            p = root / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)

        env = os.environ.copy()
        # Make commits deterministic-ish (helps reduce flakiness).
        env.setdefault("GIT_AUTHOR_DATE", "2000-01-01T00:00:00Z")
        env.setdefault("GIT_COMMITTER_DATE", "2000-01-01T00:00:00Z")

        subprocess.run(  # noqa: S603
            [git_bin, "init", "-b", default_branch],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,  # noqa: S607
        )
        subprocess.run([git_bin, "config", "user.email", "tests@example.com"], cwd=root, env=env, check=True)  # noqa: S603, S607
        subprocess.run([git_bin, "config", "user.name", "tests"], cwd=root, env=env, check=True)  # noqa: S603, S607
        subprocess.run([git_bin, "add", "-A"], cwd=root, env=env, check=True)  # noqa: S603, S607
        subprocess.run(  # noqa: S603
            [git_bin, "commit", "-m", commit_message],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,  # noqa: S607
        )

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            # Use os.walk to include dot-directories like .git/
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                filenames.sort()

                dir_rel = Path(dirpath).relative_to(root)
                if str(dir_rel) != ".":
                    tf.add(dirpath, arcname=str(dir_rel), recursive=False)

                for filename in filenames:
                    full_path = Path(dirpath) / filename
                    arcname = str(full_path.relative_to(root))
                    tf.add(full_path, arcname=arcname, recursive=False)

        return base64.b64encode(buf.getvalue()).decode("ascii")
