from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING

from benchmarks.corpus import Corpus, make_probe_content

if TYPE_CHECKING:
    from collections.abc import Callable
from daiv_sandbox.schemas import (
    FsDeleteRequest,
    FsEditRequest,
    FsGlobRequest,
    FsGrepRequest,
    FsLsRequest,
    FsReadRequest,
    FsWriteRequest,
)

# Mirror of daiv_sandbox.sessions.SANDBOX_ROOT / SCRATCH_ROOT (hardcoded to keep the harness
# free of the heavy Docker-SDK import chain that sessions.py pulls in).
SANDBOX_REPO = "/workspace/repo"
SCRATCH = "/workspace/tmp"

_EDIT_MARKER = "OLD_MARKER"


def _b64(raw: bytes) -> bytes:
    return base64.b64encode(raw)


def measure(call: Callable[[], object], *, warmup: int, iterations: int) -> list[float]:
    """Invoke *call* warmup+iterations times; return ms timings for the post-warmup runs only."""
    samples: list[float] = []
    for i in range(warmup + iterations):
        start = time.perf_counter()
        call()
        elapsed_ms = (time.perf_counter() - start) * 1000
        if i >= warmup:
            samples.append(elapsed_ms)
    return samples


def measure_seed(client, base_image: str, archive_bytes: bytes, *, warmup: int, iterations: int) -> list[float]:
    """seed is one-shot per session, so each sample creates (untimed) a fresh session, times the
    seed POST, then force-deletes (untimed)."""
    samples: list[float] = []
    for i in range(warmup + iterations):
        session_id = client.create_session(base_image)
        try:
            start = time.perf_counter()
            client.seed(session_id, archive_bytes)
            elapsed_ms = (time.perf_counter() - start) * 1000
        finally:
            client.delete_session(session_id, force=True)
        if i >= warmup:
            samples.append(elapsed_ms)
    return samples


def run_seed_suite(client, base_image: str, corpora, *, warmup: int, iterations: int) -> dict[str, list[float]]:
    return {
        c.name: measure_seed(client, base_image, c.archive_bytes, warmup=warmup, iterations=iterations) for c in corpora
    }


def run_fs_suite(
    client, base_image: str, corpus: Corpus, *, sizes: dict[str, int], warmup: int, iterations: int
) -> dict[str, list[float]]:
    """Seed one session for *corpus*, then measure each fs op against it. All probe/scratch files
    live under /workspace/tmp so the seeded /workspace/repo tree stays stable for ls/grep/glob."""
    results: dict[str, list[float]] = {}
    session_id = client.create_session(base_image)
    try:
        client.seed(session_id, corpus.archive_bytes)

        # Discover the codeload wrapper dir so `ls` targets a realistically populated directory.
        ls_root = client.fs(session_id, "ls", FsLsRequest(path=SANDBOX_REPO))
        entries = ls_root.get("entries", [])
        ls_target = entries[0]["path"] if entries else SANDBOX_REPO

        results["ls"] = measure(
            lambda: client.fs(session_id, "ls", FsLsRequest(path=ls_target)), warmup=warmup, iterations=iterations
        )
        results["grep"] = measure(
            lambda: client.fs(session_id, "grep", FsGrepRequest(pattern="import", path=SANDBOX_REPO)),
            warmup=warmup,
            iterations=iterations,
        )
        results["glob"] = measure(
            lambda: client.fs(session_id, "glob", FsGlobRequest(pattern="**/*.py", path=SANDBOX_REPO)),
            warmup=warmup,
            iterations=iterations,
        )

        for label, size in sizes.items():
            results[f"read:{label}"] = _measure_read(
                client, session_id, label, size, warmup=warmup, iterations=iterations
            )
            results[f"write:{label}"] = _measure_write(
                client, session_id, label, size, warmup=warmup, iterations=iterations
            )
            results[f"edit:{label}"] = _measure_edit(
                client, session_id, label, size, warmup=warmup, iterations=iterations
            )

        results["delete"] = _measure_delete(client, session_id, warmup=warmup, iterations=iterations)
        return results
    finally:
        client.delete_session(session_id, force=True)


def _measure_read(client, session_id, label, size, *, warmup, iterations) -> list[float]:
    path = f"{SCRATCH}/read_{label}.bin"
    client.fs(session_id, "write", FsWriteRequest(path=path, content=_b64(make_probe_content(size))))  # untimed
    return measure(
        lambda: client.fs(session_id, "read", FsReadRequest(path=path)), warmup=warmup, iterations=iterations
    )


def _measure_write(client, session_id, label, size, *, warmup, iterations) -> list[float]:
    content = _b64(make_probe_content(size))
    counter = {"i": 0}

    def call():
        counter["i"] += 1
        path = f"{SCRATCH}/write_{label}_{counter['i']}.bin"  # unique path: never hits already_exists
        client.fs(session_id, "write", FsWriteRequest(path=path, content=content))

    return measure(call, warmup=warmup, iterations=iterations)


def _measure_edit(client, session_id, label, size, *, warmup, iterations) -> list[float]:
    content = _b64(make_probe_content(size, marker=_EDIT_MARKER))
    counter = {"i": 0}
    samples: list[float] = []
    for i in range(warmup + iterations):
        counter["i"] += 1
        path = f"{SCRATCH}/edit_{label}_{counter['i']}.txt"
        client.fs(session_id, "write", FsWriteRequest(path=path, content=content))  # untimed setup
        start = time.perf_counter()
        client.fs(session_id, "edit", FsEditRequest(path=path, old=_EDIT_MARKER, new="NEW_MARKER"))
        elapsed_ms = (time.perf_counter() - start) * 1000
        if i >= warmup:
            samples.append(elapsed_ms)
    return samples


def _measure_delete(client, session_id, *, warmup, iterations) -> list[float]:
    content = _b64(make_probe_content(1024))
    samples: list[float] = []
    for i in range(warmup + iterations):
        path = f"{SCRATCH}/del_{i}.bin"
        client.fs(session_id, "write", FsWriteRequest(path=path, content=content))  # untimed setup
        start = time.perf_counter()
        client.fs(session_id, "delete", FsDeleteRequest(path=path))
        elapsed_ms = (time.perf_counter() - start) * 1000
        if i >= warmup:
            samples.append(elapsed_ms)
    return samples
