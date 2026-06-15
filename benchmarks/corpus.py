from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pathlib import Path

# Content-sensitive probe-file sizes. "512KB" is exactly READ_MAX_OUTPUT_BYTES (512_000),
# so a text read at this size returns full content (no truncation, no error).
PROBE_SIZES: dict[str, int] = {"1KB": 1024, "64KB": 65536, "512KB": 512_000}

_CODELOAD = "https://codeload.github.com/{owner}/{repo}/tar.gz/{sha}"


@dataclass(frozen=True)
class Corpus:
    name: str
    archive_bytes: bytes
    file_count: int


def make_probe_content(size_bytes: int, marker: str | None = None) -> bytes:
    """Deterministic payload of exactly *size_bytes*; optional *marker* placed at the start."""
    if size_bytes < 0:
        raise ValueError("size_bytes must be non-negative")
    body = b"" if marker is None else marker.encode("utf-8")
    if len(body) > size_bytes:
        raise ValueError("marker is longer than the requested size")
    return body + b"x" * (size_bytes - len(body))


def _count_tar_files(archive: bytes) -> int:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return sum(1 for m in tar.getmembers() if m.isfile())


def fetch_repo_corpus(
    owner: str, repo: str, sha: str, name: str, cache_dir: Path, *, client: httpx.Client | None = None
) -> Corpus:
    """Return a Corpus from a GitHub codeload tarball, caching the download under *cache_dir*."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{owner}-{repo}-{sha}.tar.gz"
    if cached.exists():
        archive = cached.read_bytes()
    else:
        owns_client = client is None
        http = client or httpx.Client(timeout=120.0, follow_redirects=True)
        try:
            resp = http.get(_CODELOAD.format(owner=owner, repo=repo, sha=sha))
            resp.raise_for_status()
            archive = resp.content
        finally:
            if owns_client:
                http.close()
        cached.write_bytes(archive)
    return Corpus(name=name, archive_bytes=archive, file_count=_count_tar_files(archive))
