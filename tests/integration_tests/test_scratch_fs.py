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
