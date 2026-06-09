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


def test_ls_on_file_reports_not_a_directory(client, workspace_session):
    sid = workspace_session
    ls = client.post(f"/session/{sid}/fs/ls", json={"path": "/workspace/repo/README.md"})
    assert ls.status_code == 200, ls.text
    body = ls.json()
    assert body["entries"] == []
    assert body["error"]["code"] == "not_a_directory"


def test_read_directory_reports_is_a_directory(client, workspace_session):
    sid = workspace_session
    r = client.post(f"/session/{sid}/fs/read", json={"path": "/workspace/repo", "offset": 0, "limit": 2000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] is None
    assert body["error"]["code"] == "is_a_directory"


def test_glob_on_file_base_reports_not_a_directory(client, workspace_session):
    sid = workspace_session
    gl = client.post(f"/session/{sid}/fs/glob", json={"path": "/workspace/repo/README.md", "pattern": "*"})
    assert gl.status_code == 200, gl.text
    body = gl.json()
    assert body["matches"] == []
    assert body["error"]["code"] == "not_a_directory"


def test_delete_missing_reports_not_removed(client, workspace_session):
    sid = workspace_session
    d = client.post(f"/session/{sid}/fs/delete", json={"path": "/workspace/tmp/never-existed.txt"})
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["ok"] is True
    assert body["removed"] is False
    assert body["error"] is None


def test_delete_existing_reports_removed(client, workspace_session):
    sid = workspace_session
    w = client.post(
        f"/session/{sid}/fs/write",
        json={"path": "/workspace/tmp/doomed.txt", "content": base64.b64encode(b"x\n").decode(), "mode": 0o644},
    )
    assert w.status_code == 200 and w.json()["ok"] is True, w.text
    d = client.post(f"/session/{sid}/fs/delete", json={"path": "/workspace/tmp/doomed.txt"})
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["ok"] is True
    assert body["removed"] is True


def test_delete_directory_reports_is_a_directory(client, workspace_session):
    sid = workspace_session
    run = client.post(f"/session/{sid}/", json={"commands": ["mkdir -p /workspace/tmp/adir"]})
    assert run.status_code == 200, run.text
    d = client.post(f"/session/{sid}/fs/delete", json={"path": "/workspace/tmp/adir"})
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "is_a_directory"


def test_glob_prunes_caches_but_keeps_dependency_source(client, workspace_session):
    """Default pruning skips cache/VCS junk (incl. __pycache__ inside deps) but still returns
    dependency source under node_modules (not a default-pruned dir)."""
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/",
        json={
            "commands": [
                "mkdir -p /workspace/tmp/pkg/__pycache__ /workspace/tmp/.ruff_cache "
                "/workspace/tmp/node_modules/dep/__pycache__",
                "printf 'real\\n' > /workspace/tmp/pkg/real.py",
                # `.py` (not `.pyc`) so it WOULD match **/*.py if __pycache__ were not pruned — proves
                # the pruning, not just that the glob skips .pyc. One sits inside a kept dependency tree.
                "printf 'cached\\n' > /workspace/tmp/pkg/__pycache__/cached.py",
                "printf 'depcache\\n' > /workspace/tmp/node_modules/dep/__pycache__/dep_cached.py",
                "printf 'junk\\n' > /workspace/tmp/.ruff_cache/x.py",
                "printf 'depsrc\\n' > /workspace/tmp/node_modules/dep/index.py",
            ]
        },
    )
    assert run.status_code == 200, run.text

    gl = client.post(f"/session/{sid}/fs/glob", json={"path": "/workspace/tmp", "pattern": "**/*.py"})
    assert gl.status_code == 200, gl.text
    paths = {m["path"] for m in gl.json()["matches"]}
    assert "/workspace/tmp/pkg/real.py" in paths  # real source kept
    assert "/workspace/tmp/node_modules/dep/index.py" in paths  # dependency source kept
    assert not any("/__pycache__/" in p for p in paths)  # bytecode dir pruned (incl. inside node_modules)
    assert not any("/.ruff_cache/" in p for p in paths)  # cache dir pruned


def test_glob_request_exclude_prunes_dependency_dir(client, workspace_session):
    """A per-request exclude adds to the defaults (here: prune node_modules too)."""
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/",
        json={
            "commands": [
                "mkdir -p /workspace/tmp/node_modules/dep",
                "printf 'a\\n' > /workspace/tmp/keep.py",
                "printf 'b\\n' > /workspace/tmp/node_modules/dep/index.py",
            ]
        },
    )
    assert run.status_code == 200, run.text

    gl = client.post(
        f"/session/{sid}/fs/glob", json={"path": "/workspace/tmp", "pattern": "**/*.py", "exclude": ["node_modules"]}
    )
    assert gl.status_code == 200, gl.text
    paths = {m["path"] for m in gl.json()["matches"]}
    assert "/workspace/tmp/keep.py" in paths
    assert not any("/node_modules/" in p for p in paths)


def test_grep_prunes_caches_but_keeps_dependency_source(client, workspace_session):
    """grep skips reading default-pruned dirs (no NEEDLE from .ruff_cache) but still searches
    node_modules dependency source; a request exclude can prune node_modules too."""
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/",
        json={
            "commands": [
                "mkdir -p /workspace/tmp/.ruff_cache /workspace/tmp/node_modules/dep",
                "printf 'NEEDLE\\n' > /workspace/tmp/real.txt",
                "printf 'NEEDLE\\n' > /workspace/tmp/.ruff_cache/c.txt",
                "printf 'NEEDLE\\n' > /workspace/tmp/node_modules/dep/d.txt",
            ]
        },
    )
    assert run.status_code == 200, run.text

    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/workspace/tmp", "glob": None})
    assert g.status_code == 200, g.text
    paths = {m["path"] for m in g.json()["matches"]}
    assert "/workspace/tmp/real.txt" in paths
    assert "/workspace/tmp/node_modules/dep/d.txt" in paths  # dependency source searched
    assert not any("/.ruff_cache/" in p for p in paths)  # cache dir not read

    g2 = client.post(
        f"/session/{sid}/fs/grep",
        json={"pattern": "NEEDLE", "path": "/workspace/tmp", "glob": None, "exclude": ["node_modules"]},
    )
    assert g2.status_code == 200, g2.text
    paths2 = {m["path"] for m in g2.json()["matches"]}
    assert "/workspace/tmp/real.txt" in paths2
    assert not any("/node_modules/" in p for p in paths2)


def test_grep_no_match_in_pruned_tree_returns_empty_not_error(client, workspace_session):
    """A search whose only candidate files are under pruned dirs returns [] cleanly (the xargs
    exit-code normalization must not surface a no-match as exec_failed)."""
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/",
        json={
            "commands": [
                "mkdir -p /workspace/tmp/.ruff_cache",
                "printf 'NEEDLE\\n' > /workspace/tmp/.ruff_cache/only.txt",
            ]
        },
    )
    assert run.status_code == 200, run.text

    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/workspace/tmp", "glob": None})
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["error"] is None, body
    assert body["matches"] == [], body


def test_grep_surfaces_unreadable_file_as_error(client, workspace_session):
    """A file grep cannot read must surface as an error, not be silently dropped or returned as a
    partial result. The old `grep -r` exited 2 on a per-file read error; the find|xargs pipeline
    must preserve that contract (xargs collapses grep 1/2 into 123, so the read error has to be
    recovered from grep's stderr). Here a readable match coexists with an unreadable file: the whole
    grep must surface exec_failed rather than returning only the readable match as if complete."""
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/",
        json={
            "commands": [
                "printf 'NEEDLE readable\\n' > /workspace/tmp/ok.txt",
                "printf 'NEEDLE locked\\n' > /workspace/tmp/locked.txt",
                "chmod 000 /workspace/tmp/locked.txt",
            ]
        },
    )
    assert run.status_code == 200, run.text

    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/workspace/tmp", "glob": None})
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["error"] is not None, body
    assert body["error"]["code"] == "exec_failed", body


def test_glob_and_grep_prune_egg_info_glob_default_at_depth(client, workspace_session):
    """The default prune list includes the *glob* entry `*.egg-info` (not a literal name). busybox
    `find -name '*.egg-info' -prune` must match it at any depth — for both glob and grep — which a
    mis-quoted `*` (escaped so the shell never expands it) would silently break."""
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/",
        json={
            "commands": [
                "mkdir -p /workspace/tmp/src/mypkg.egg-info",
                "printf 'NEEDLE\\n' > /workspace/tmp/src/keep.txt",
                "printf 'NEEDLE\\n' > /workspace/tmp/src/mypkg.egg-info/PKG-INFO.txt",
            ]
        },
    )
    assert run.status_code == 200, run.text

    gl = client.post(f"/session/{sid}/fs/glob", json={"path": "/workspace/tmp", "pattern": "**/*.txt"})
    assert gl.status_code == 200, gl.text
    gl_paths = {m["path"] for m in gl.json()["matches"]}
    assert "/workspace/tmp/src/keep.txt" in gl_paths
    assert not any(".egg-info/" in p for p in gl_paths)  # glob-form default pruned at depth

    g = client.post(f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/workspace/tmp", "glob": None})
    assert g.status_code == 200, g.text
    g_paths = {m["path"] for m in g.json()["matches"]}
    assert "/workspace/tmp/src/keep.txt" in g_paths
    assert not any(".egg-info/" in p for p in g_paths)  # grep never opens files inside it either


def test_grep_single_file_target(client, workspace_session):
    """grep against a file path (not a directory) takes the single-file branch and returns matches
    with the same path:line:text shape as the directory branch."""
    sid = workspace_session
    run = client.post(
        f"/session/{sid}/", json={"commands": ["printf 'alpha\\nNEEDLE here\\nomega\\n' > /workspace/tmp/single.txt"]}
    )
    assert run.status_code == 200, run.text

    g = client.post(
        f"/session/{sid}/fs/grep", json={"pattern": "NEEDLE", "path": "/workspace/tmp/single.txt", "glob": None}
    )
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["error"] is None, body
    assert body["matches"] == [{"path": "/workspace/tmp/single.txt", "line": 2, "text": "NEEDLE here"}], body
