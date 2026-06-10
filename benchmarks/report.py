from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from benchmarks.stats import Summary, summarize

if TYPE_CHECKING:
    from pathlib import Path


def _summarized(groups: dict[str, dict[str, list[float]]]) -> dict[str, dict[str, tuple[Summary, list[float]]]]:
    return {
        group: {label: (summarize(samples), samples) for label, samples in rows.items()}
        for group, rows in groups.items()
    }


def render(meta: dict, groups: dict[str, dict[str, list[float]]]) -> tuple[str, dict]:
    summarized = _summarized(groups)

    lines = ["# daiv-sandbox latency benchmark", ""]
    for key in ("generated_at", "service_version", "base_url", "base_image", "warmup", "iterations"):
        if key in meta:
            lines.append(f"- **{key}**: {meta[key]}")
    lines.append("")

    for group, rows in summarized.items():
        lines.append(f"## {group}")
        lines.append("")
        lines.append("| operation | N | min | mean | p50 | p95 | max | stddev |")
        lines.append("| --- | --: | --: | --: | --: | --: | --: | --: |")
        for label, (summary, _samples) in rows.items():
            lines.append(
                f"| {label} | {summary.n} | {summary.min_ms:.2f} | {summary.mean_ms:.2f} | "
                f"{summary.p50_ms:.2f} | {summary.p95_ms:.2f} | {summary.max_ms:.2f} | {summary.stddev_ms:.2f} |"
            )
        lines.append("")
    markdown = "\n".join(lines)

    obj = {
        "meta": meta,
        "groups": {
            group: {
                label: {"summary": asdict(summary), "samples_ms": samples} for label, (summary, samples) in rows.items()
            }
            for group, rows in summarized.items()
        },
    }
    return markdown, obj


def write(out_dir: Path, meta: dict, groups: dict[str, dict[str, list[float]]]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    meta = {"generated_at": stamp, **meta}
    markdown, obj = render(meta, groups)
    md_path = out_dir / f"{stamp}.md"
    json_path = out_dir / f"{stamp}.json"
    md_path.write_text(markdown)
    json_path.write_text(json.dumps(obj, indent=2))
    return md_path, json_path
