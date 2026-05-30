import base64

import pytest

from .utils import make_tar_gz


@pytest.fixture
def scratch_session(client, sandbox_session):
    # alpine has sh/grep/find/ls but NOT python3 — proves the ops are Python-free.
    sid = sandbox_session(base_image="alpine:latest", extract_patch=True)
    # extract_patch sessions need their meta repo initialised before the run endpoint
    # computes per-turn diffs; seeding a minimal /repo does that. /scratch is created at
    # container bootstrap and lives outside the /repo volume, so it stays diff-invisible.
    seed = client.post(
        f"/session/{sid}/seed/",
        files={"repo_archive": ("repo.tar.gz", make_tar_gz({"README.md": b"# scratch test\n"}), "application/gzip")},
    )
    assert seed.status_code == 204, seed.text
    return sid


def test_agent_write_then_bash_read(client, scratch_session):
    sid = scratch_session
    w = client.post(
        f"/session/{sid}/fs/write",
        json={"path": "/scratch/note.txt", "content": base64.b64encode(b"from-agent\n").decode(), "mode": 0o644},
    )
    assert w.status_code == 200 and w.json()["ok"] is True, w.text

    run = client.post(f"/session/{sid}/", json={"commands": ["cat /scratch/note.txt"]})
    assert run.status_code == 200, run.text
    assert "from-agent" in run.json()["results"][0]["output"]


def test_bash_write_then_agent_read_and_grep(client, scratch_session):
    sid = scratch_session
    run = client.post(f"/session/{sid}/", json={"commands": ["printf 'alpha\\nNEEDLE here\\n' > /scratch/out.txt"]})
    assert run.status_code == 200, run.text

    r = client.post(f"/session/{sid}/fs/read", json={"path": "/scratch/out.txt", "offset": 0, "limit": 2000})
    assert r.status_code == 200 and "NEEDLE" in r.json()["content"], r.text

    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/scratch", "glob": None})
    assert any(m["text"].strip() == "NEEDLE here" for m in g.json()["matches"]), g.text


def test_scratch_changes_never_appear_in_patch(client, scratch_session):
    sid = scratch_session
    client.post(
        f"/session/{sid}/fs/write",
        json={"path": "/scratch/ephemeral.txt", "content": base64.b64encode(b"junk\n").decode(), "mode": 0o644},
    )
    run = client.post(f"/session/{sid}/", json={"commands": ["echo more >> /scratch/ephemeral.txt"]})
    # /scratch is not on the /repo volume the patch-extractor diffs → patch stays empty.
    assert run.json()["patch"] is None, run.text


def test_grep_with_glob_filters_on_busybox(client, scratch_session):
    """grep with a `glob` must work on alpine's busybox grep (which has no --include)."""
    sid = scratch_session
    run = client.post(
        f"/session/{sid}/",
        json={"commands": ["printf 'NEEDLE\\n' > /scratch/a.py", "printf 'NEEDLE\\n' > /scratch/b.txt"]},
    )
    assert run.status_code == 200, run.text

    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/scratch", "glob": "*.py"})
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["error"] is None, body
    paths = {m["path"] for m in body["matches"]}
    assert paths == {"/scratch/a.py"}, body


def test_ls_and_glob_roundtrip(client, scratch_session):
    sid = scratch_session
    run = client.post(
        f"/session/{sid}/",
        json={
            "commands": [
                "mkdir -p /scratch/sub",
                "printf 'x\\n' > /scratch/top.py",
                "printf 'y\\n' > /scratch/sub/deep.py",
            ]
        },
    )
    assert run.status_code == 200, run.text

    ls = client.post(f"/session/{sid}/fs/ls", json={"path": "/scratch"})
    assert ls.status_code == 200, ls.text
    entries = {(e["path"], e["is_dir"]) for e in ls.json()["entries"]}
    assert ("/scratch/sub", True) in entries
    assert ("/scratch/top.py", False) in entries

    gl = client.post(f"/session/{sid}/fs/glob", json={"path": "/scratch", "pattern": "**/*.py"})
    assert gl.status_code == 200, gl.text
    glob_paths = {m["path"] for m in gl.json()["matches"]}
    assert {"/scratch/top.py", "/scratch/sub/deep.py"} <= glob_paths, gl.json()


def test_edit_roundtrip_and_bash_sees_it(client, scratch_session):
    sid = scratch_session
    w = client.post(
        f"/session/{sid}/fs/write",
        json={"path": "/scratch/e.txt", "content": base64.b64encode(b"alpha beta alpha\n").decode(), "mode": 0o644},
    )
    assert w.status_code == 200 and w.json()["ok"] is True, w.text

    e = client.post(
        f"/session/{sid}/fs/edit", json={"path": "/scratch/e.txt", "old": "alpha", "new": "ALPHA", "replace_all": True}
    )
    assert e.status_code == 200, e.text
    assert e.json()["occurrences"] == 2, e.json()

    run = client.post(f"/session/{sid}/", json={"commands": ["cat /scratch/e.txt"]})
    assert "ALPHA beta ALPHA" in run.json()["results"][0]["output"], run.text


def test_ls_outside_scratch_is_rejected(client, scratch_session):
    """`..` traversal must be refused (confinement)."""
    sid = scratch_session
    resp = client.post(f"/session/{sid}/fs/ls", json={"path": "/scratch/../repo"})
    assert resp.status_code == 400, resp.text
