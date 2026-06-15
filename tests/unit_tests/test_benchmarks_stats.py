import pytest

from benchmarks.stats import summarize


def test_summarize_ten_samples():
    s = summarize([float(x) for x in range(1, 11)])  # 1.0 .. 10.0
    assert s.n == 10
    assert s.min_ms == 1.0
    assert s.max_ms == 10.0
    assert s.mean_ms == pytest.approx(5.5)
    assert s.p50_ms == 5.0  # nearest-rank: ceil(0.5*10)=5 -> index 4
    assert s.p95_ms == 10.0  # nearest-rank: ceil(0.95*10)=10 -> index 9
    assert s.stddev_ms == pytest.approx(3.0276503540974917)


def test_summarize_single_sample_has_zero_stddev():
    s = summarize([42.0])
    assert s.n == 1
    assert s.min_ms == s.max_ms == s.mean_ms == s.p50_ms == s.p95_ms == 42.0
    assert s.stddev_ms == 0.0


def test_summarize_unsorted_input():
    s = summarize([5.0, 1.0, 3.0, 2.0, 4.0])
    assert s.min_ms == 1.0 and s.max_ms == 5.0
    assert s.p50_ms == 3.0  # ceil(0.5*5)=3 -> index 2


def test_summarize_empty_raises():
    with pytest.raises(ValueError):
        summarize([])
