import io
import tarfile

import pytest

from benchmarks.corpus import PROBE_SIZES, fetch_repo_corpus, make_probe_content


def _tar_gz_with_files(prefix: str, count: int) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for i in range(count):
            data = b"x"
            info = tarfile.TarInfo(name=f"{prefix}/file_{i:03d}.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_probe_sizes_present():
    assert PROBE_SIZES == {"1KB": 1024, "64KB": 65536, "512KB": 512_000}


def test_make_probe_content_exact_size_no_marker():
    data = make_probe_content(1024)
    assert len(data) == 1024
    assert set(data) == {ord("x")}


def test_make_probe_content_with_marker_prefix():
    data = make_probe_content(100, marker="OLD")
    assert len(data) == 100
    assert data.startswith(b"OLD")


def test_make_probe_content_marker_too_long_raises():
    with pytest.raises(ValueError):
        make_probe_content(2, marker="OLD")


def test_fetch_repo_corpus_uses_cache_without_network(tmp_path):
    # Pre-seed the cache with a tarball at the expected filename; client=None
    # must NOT trigger a network call because the cache hit short-circuits.
    cache_file = tmp_path / "psf-requests-deadbeef.tar.gz"
    cache_file.write_bytes(_tar_gz_with_files("requests-deadbeef", 7))

    corpus = fetch_repo_corpus("psf", "requests", "deadbeef", name="small", cache_dir=tmp_path, client=None)
    assert corpus.name == "small"
    assert corpus.file_count == 7
