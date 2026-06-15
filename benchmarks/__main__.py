from __future__ import annotations

# ruff: noqa: T201
import argparse
import os
import sys
from pathlib import Path

from benchmarks import report
from benchmarks.client import BenchClient
from benchmarks.corpus import PROBE_SIZES, fetch_repo_corpus
from benchmarks.repos import REPOS
from benchmarks.runner import run_fs_suite, run_seed_suite

_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
_DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmarks", description="daiv-sandbox latency benchmark")
    parser.add_argument("--base-url", default="http://localhost:8888")
    parser.add_argument("--root-path", default="", help="Prefix when behind a proxy (e.g. /api/v1)")
    parser.add_argument("--api-key", default=os.environ.get("DAIV_SANDBOX_API_KEY", ""))
    parser.add_argument("--base-image", default="python:3.14-slim")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--ops", nargs="+", choices=["seed", "fs"], default=["seed", "fs"])
    parser.add_argument(
        "--corpora", nargs="+", default=[r.name for r in REPOS], help="Subset of corpus names to run (default: all)"
    )
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.api_key:
        print("error: no API key (set DAIV_SANDBOX_API_KEY or pass --api-key)", file=sys.stderr)
        return 2

    selected_repos = [r for r in REPOS if r.name in args.corpora]
    if not selected_repos:
        print(f"error: no corpora matched {args.corpora}", file=sys.stderr)
        return 2

    with BenchClient(args.base_url, args.api_key, root_path=args.root_path) as client:
        service_version = client.version()  # connectivity smoke check (before downloading corpora)
        print(f"connected to {args.base_url} (service version {service_version})")

        corpora = [fetch_repo_corpus(r.owner, r.repo, r.sha, r.name, _CACHE_DIR) for r in selected_repos]
        for c in corpora:
            print(f"corpus {c.name}: {c.file_count} files")

        groups: dict[str, dict[str, list[float]]] = {}
        if "seed" in args.ops:
            print("running seed suite ...")
            groups["seed"] = run_seed_suite(
                client, args.base_image, corpora, warmup=args.warmup, iterations=args.iterations
            )
        if "fs" in args.ops:
            fs_results: dict[str, list[float]] = {}
            for c in corpora:
                print(f"running fs suite on corpus {c.name} ...")
                for label, samples in run_fs_suite(
                    client, args.base_image, c, sizes=PROBE_SIZES, warmup=args.warmup, iterations=args.iterations
                ).items():
                    fs_results[f"{c.name}/{label}"] = samples
            groups["fs"] = fs_results

        meta = {
            "service_version": service_version,
            "base_url": args.base_url,
            "base_image": args.base_image,
            "warmup": args.warmup,
            "iterations": args.iterations,
            "corpora": {c.name: c.file_count for c in corpora},
            "repos": {r.name: f"{r.owner}/{r.repo}@{r.sha}" for r in selected_repos},
        }
        md_path, json_path = report.write(args.output_dir, meta, groups)
        print(f"wrote {md_path}")
        print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
