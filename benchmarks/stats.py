from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class Summary:
    n: int
    min_ms: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    stddev_ms: float


def summarize(samples_ms: list[float]) -> Summary:
    if not samples_ms:
        raise ValueError("summarize requires at least one sample")
    ordered = sorted(samples_ms)
    return Summary(
        n=len(ordered),
        min_ms=ordered[0],
        mean_ms=statistics.fmean(ordered),
        p50_ms=_percentile(ordered, 50),
        p95_ms=_percentile(ordered, 95),
        max_ms=ordered[-1],
        stddev_ms=statistics.stdev(ordered) if len(ordered) > 1 else 0.0,
    )


def _percentile(ordered: list[float], pct: float) -> float:
    # Nearest-rank percentile on an already-sorted, non-empty list.
    if not ordered:
        raise ValueError("percentile of an empty sequence")
    rank = math.ceil((pct / 100) * len(ordered))
    index = min(max(rank, 1), len(ordered)) - 1
    return ordered[index]
