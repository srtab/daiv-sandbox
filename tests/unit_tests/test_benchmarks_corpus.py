import io
import tarfile

import pytest

from benchmarks.corpus import PROBE_SIZES, Corpus, fetch_repo_corpus, make_probe_content, make_synthetic_corpus


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


def test_make_synthetic_corpus_file_count_and_names():
    corpus = make_synthetic_corpus("syn", file_count=50, depth=3)
    assert isinstance(corpus, Corpus)
    assert corpus.file_count == 50
    with tarfile.open(fileobj=io.BytesIO(corpus.archive_bytes), mode="r:gz") as tar:
        files = [m for m in tar.getmembers() if m.isfile()]
    assert len(files) == 50
    assert all(m.name.startswith("syn/") for m in files)


def test_fetch_repo_corpus_uses_cache_without_network(tmp_path):
    # Pre-seed the cache with a synthetic tarball at the expected filename; client=None
    # must NOT trigger a network call because the cache hit short-circuits.
    cached = make_synthetic_corpus("requests-deadbeef", file_count=7, depth=2)
    cache_file = tmp_path / "psf-requests-deadbeef.tar.gz"
    cache_file.write_bytes(cached.archive_bytes)

    corpus = fetch_repo_corpus("psf", "requests", "deadbeef", name="small", cache_dir=tmp_path, client=None)
    assert corpus.name == "small"
    assert corpus.file_count == 7
