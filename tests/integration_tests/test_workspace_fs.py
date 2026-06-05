import base64

import pytest

from .utils import make_tar_gz


@pytest.fixture
def workspace_session(client, sandbox_session):
    # alpine has sh/grep/find/ls but NOT python3 — proves the ops are Python-free.
    sid = sandbox_session(base_image="alpine:latest")
    # Seed a minimal repo so /workspace/repo has content to read/edit; /workspace/{repo,skills,tmp}
    # all exist from container bootstrap regardless.
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


def test_ls_missing_directory_reports_not_found(client, workspace_session):
    """Listing an absent directory is now a distinct, surfaced outcome (not_found), so an agent can't
    mistake 'absent' for 'exists but empty'."""
    sid = workspace_session
    ls = client.post(f"/session/{sid}/fs/ls", json={"path": "/workspace/repo/.claude/skills"})
    assert ls.status_code == 200, ls.text
    body = ls.json()
    assert body["entries"] == []
    assert body["error"]["code"] == "not_found"


def test_glob_missing_directory_reports_not_found(client, workspace_session):
    sid = workspace_session
    gl = client.post(f"/session/{sid}/fs/glob", json={"path": "/workspace/repo/.cursor/skills", "pattern": "**/*"})
    assert gl.status_code == 200, gl.text
    body = gl.json()
    assert body["matches"] == []
    assert body["error"]["code"] == "not_found"


def test_grep_missing_directory_reports_not_found(client, workspace_session):
    sid = workspace_session
    g = client.post(
        f"/session/{sid}/fs/grep", json={"pattern": "anything", "path": "/workspace/repo/.agents/skills", "glob": None}
    )
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["matches"] == []
    assert body["error"]["code"] == "not_found"


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
    """`..` is refused as a 200 body with error.code=invalid_path (unified across all fs ops)."""
    sid = workspace_session
    resp = client.post(f"/session/{sid}/fs/ls", json={"path": "/workspace/tmp/../repo"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "invalid_path"


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

    # Read each back by its own path: the three writes must land in distinct subtrees with their own
    # content (guards against a mis-rooted write or one subdir clobbering another now that repo/skills/
    # tmp are sibling dirs on one filesystem rather than separate volumes).
    for path, needle in [
        ("/workspace/repo/app.py", "print('hi')"),
        ("/workspace/skills/s.md", "# skill"),
        ("/workspace/tmp/scratch.txt", "NEEDLE"),
    ]:
        r = client.post(f"/session/{sid}/fs/read", json={"path": path, "offset": 0, "limit": 2000})
        assert r.status_code == 200, (path, r.text)
        assert needle in r.json()["content"], (path, r.json())

    # grep across the whole workspace finds the tmp match
    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/workspace", "glob": None})
    assert any(m["path"] == "/workspace/tmp/scratch.txt" for m in g.json()["matches"]), g.text


def test_traversal_above_workspace_is_rejected(client, workspace_session):
    sid = workspace_session
    resp = client.post(f"/session/{sid}/fs/ls", json={"path": "/workspace/../etc"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"]["code"] == "invalid_path"
