import base64

import pytest

from .utils import make_tar_gz


@pytest.fixture
def workspace_session(client, sandbox_session):
    # alpine has sh/grep/find/ls but NOT python3 — proves the ops are Python-free.
    sid = sandbox_session(base_image="alpine:latest", extract_patch=True)
    # extract_patch sessions need their meta repo initialised before the run endpoint
    # computes per-turn diffs; seeding a minimal repo does that. /workspace/tmp is created at
    # container bootstrap and lives outside the repo volume, so it stays diff-invisible.
    seed = client.post(
        f"/session/{sid}/seed/",
        files={"repo_archive": ("repo.tar.gz", make_tar_gz({"README.md": b"# workspace test\n"}), "application/gzip")},
    )
    assert seed.status_code == 204, seed.text
    return sid


def test_agent_write_then_bash_read(client, workspace_session):
    sid = workspace_session
    w = client.post(
        f"/session/{sid}/fs/write",
        json={"path": "/workspace/tmp/note.txt", "content": base64.b64encode(b"from-agent\n").decode(), "mode": 0o644},
    )
    assert w.status_code == 200 and w.json()["ok"] is True, w.text

    run = client.post(f"/session/{sid}/", json={"commands": ["cat /workspace/tmp/note.txt"]})
    assert run.status_code == 200, run.text
    assert "from-agent" in run.json()["results"][0]["output"]


def test_bash_write_then_agent_read_and_grep(client, workspace_session):
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/", json={"commands": ["printf 'alpha\\nNEEDLE here\\n' > /workspace/tmp/out.txt"]}
    )
    assert run.status_code == 200, run.text

    r = client.post(f"/session/{sid}/fs/read", json={"path": "/workspace/tmp/out.txt", "offset": 0, "limit": 2000})
    assert r.status_code == 200 and "NEEDLE" in r.json()["content"], r.text

    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/workspace/tmp", "glob": None})
    assert any(m["text"].strip() == "NEEDLE here" for m in g.json()["matches"]), g.text


def test_tmp_changes_never_appear_in_patch(client, workspace_session):
    sid = workspace_session
    client.post(
        f"/session/{sid}/fs/write",
        json={"path": "/workspace/tmp/ephemeral.txt", "content": base64.b64encode(b"junk\n").decode(), "mode": 0o644},
    )
    run = client.post(f"/session/{sid}/", json={"commands": ["echo more >> /workspace/tmp/ephemeral.txt"]})
    # /workspace/tmp is not on the repo volume the patch-extractor diffs → patch stays empty.
    assert run.json()["patch"] is None, run.text


def test_grep_with_glob_filters_on_busybox(client, workspace_session):
    """grep with a `glob` must work on alpine's busybox grep (which has no --include)."""
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/",
        json={"commands": ["printf 'NEEDLE\\n' > /workspace/tmp/a.py", "printf 'NEEDLE\\n' > /workspace/tmp/b.txt"]},
    )
    assert run.status_code == 200, run.text

    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/workspace/tmp", "glob": "*.py"})
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["error"] is None, body
    paths = {m["path"] for m in body["matches"]}
    assert paths == {"/workspace/tmp/a.py"}, body


def test_ls_and_glob_roundtrip(client, workspace_session):
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/",
        json={
            "commands": [
                "mkdir -p /workspace/tmp/sub",
                "printf 'x\\n' > /workspace/tmp/top.py",
                "printf 'y\\n' > /workspace/tmp/sub/deep.py",
            ]
        },
    )
    assert run.status_code == 200, run.text

    ls = client.post(f"/session/{sid}/fs/ls", json={"path": "/workspace/tmp"})
    assert ls.status_code == 200, ls.text
    entries = {(e["path"], e["is_dir"]) for e in ls.json()["entries"]}
    assert ("/workspace/tmp/sub", True) in entries
    assert ("/workspace/tmp/top.py", False) in entries

    gl = client.post(f"/session/{sid}/fs/glob", json={"path": "/workspace/tmp", "pattern": "**/*.py"})
    assert gl.status_code == 200, gl.text
    glob_paths = {m["path"] for m in gl.json()["matches"]}
    assert {"/workspace/tmp/top.py", "/workspace/tmp/sub/deep.py"} <= glob_paths, gl.json()


def test_edit_roundtrip_and_bash_sees_it(client, workspace_session):
    sid = workspace_session
    w = client.post(
        f"/session/{sid}/fs/write",
        json={
            "path": "/workspace/tmp/e.txt",
            "content": base64.b64encode(b"alpha beta alpha\n").decode(),
            "mode": 0o644,
        },
    )
    assert w.status_code == 200 and w.json()["ok"] is True, w.text

    e = client.post(
        f"/session/{sid}/fs/edit",
        json={"path": "/workspace/tmp/e.txt", "old": "alpha", "new": "ALPHA", "replace_all": True},
    )
    assert e.status_code == 200, e.text
    assert e.json()["occurrences"] == 2, e.json()

    run = client.post(f"/session/{sid}/", json={"commands": ["cat /workspace/tmp/e.txt"]})
    assert "ALPHA beta ALPHA" in run.json()["results"][0]["output"], run.text


def test_ls_traversal_above_tmp_is_rejected(client, workspace_session):
    """`..` is refused. `/workspace/tmp/../repo` stays under /workspace but `..` is rejected lexically."""
    sid = workspace_session
    resp = client.post(f"/session/{sid}/fs/ls", json={"path": "/workspace/tmp/../repo"})
    assert resp.status_code == 400, resp.text


def test_fs_ops_span_repo_skills_tmp(client, workspace_session):
    sid = workspace_session
    # write into each workspace subdir via the fs endpoints
    for path, body in [
        ("/workspace/repo/app.py", b"print('hi')\n"),
        ("/workspace/skills/s.md", b"# skill\n"),
        ("/workspace/tmp/scratch.txt", b"NEEDLE\n"),
    ]:
        w = client.post(
            f"/session/{sid}/fs/write", json={"path": path, "content": base64.b64encode(body).decode(), "mode": 0o644}
        )
        assert w.status_code == 200 and w.json()["ok"] is True, (path, w.text)

    # read one back
    r = client.post(f"/session/{sid}/fs/read", json={"path": "/workspace/repo/app.py", "offset": 0, "limit": 2000})
    assert r.status_code == 200 and "print('hi')" in r.json()["content"], r.text

    # grep across the whole workspace finds the tmp match
    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/workspace", "glob": None})
    assert any(m["path"] == "/workspace/tmp/scratch.txt" for m in g.json()["matches"]), g.text


def test_traversal_above_workspace_is_rejected(client, workspace_session):
    sid = workspace_session
    resp = client.post(f"/session/{sid}/fs/ls", json={"path": "/workspace/../etc"})
    assert resp.status_code == 400, resp.text


def test_repo_edits_patch_but_tmp_does_not(client, workspace_session):
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/",
        json={"commands": ["echo changed >> /workspace/repo/README.md", "echo junk > /workspace/tmp/junk.txt"]},
    )
    assert run.status_code == 200, run.text
    patch = run.json()["patch"]
    assert patch is not None, "repo change must produce a patch"
    decoded = base64.b64decode(patch).decode()
    assert "README.md" in decoded
    assert "junk.txt" not in decoded  # tmp is not on the diffed volume


def test_skills_edits_do_not_appear_in_patch(client, workspace_session):
    sid = workspace_session
    # skills/ lives under /workspace but off the repo volume the patch-extractor diffs, so it should
    # behave like tmp/: never surface in a patch. Seed a skills file via the fs endpoint first.
    w = client.post(
        f"/session/{sid}/fs/write",
        json={
            "path": "/workspace/skills/helper.md",
            "content": base64.b64encode(b"# helper\n").decode(),
            "mode": 0o644,
        },
    )
    assert w.status_code == 200 and w.json()["ok"] is True, w.text
    # A repo edit in the same turn forces a patch; the skills edit must not appear in it.
    run = client.post(
        f"/session/{sid}/",
        json={"commands": ["echo changed >> /workspace/repo/README.md", "echo more >> /workspace/skills/helper.md"]},
    )
    assert run.status_code == 200, run.text
    patch = run.json()["patch"]
    assert patch is not None, "repo change must produce a patch"
    decoded = base64.b64decode(patch).decode()
    assert "README.md" in decoded
    assert "helper.md" not in decoded  # skills/ is not on the diffed repo volume
