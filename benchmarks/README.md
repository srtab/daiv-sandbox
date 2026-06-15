# daiv-sandbox latency benchmark

A **client-only** harness that measures end-to-end HTTP latency of the daiv-sandbox
`seed` endpoint and the `fs/*` primitives, against the default `runc` runtime. It drives a
**separately-running** service over HTTP — it never starts the service or touches the Docker
SDK itself.

## Prerequisites

- A running daiv-sandbox service. For local runs:
  ```bash
  DAIV_SANDBOX_API_KEY=<key> make run        # serves http://localhost:8888
  ```
  (equivalently `uv run fastapi run daiv_sandbox/main.py --port 8888`)
- The Docker daemon must be available to that service (it pulls `--base-image` and runs the
  sandbox containers).
- Network access on the first run to download the pinned repo tarballs from GitHub. They are
  cached afterwards under `benchmarks/.cache/` (gitignored), so later runs are offline and
  byte-identical.

## Usage

```bash
DAIV_SANDBOX_API_KEY=<key> uv run python -m benchmarks
```

Run it from the repo root (so `benchmarks` and `daiv_sandbox` are importable). The API key
must match the running service's `DAIV_SANDBOX_API_KEY` (or pass `--api-key`).

### Options

| Flag           | Default                 | Meaning                                                                                                                                                                   |
| -------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--base-url`   | `http://localhost:8888` | Service URL.                                                                                                                                                              |
| `--root-path`  | `""`                    | Set to `/api/v1` **only** when targeting a deployment behind a prefix-stripping proxy. A direct `make run` serves routes at the root.                                     |
| `--api-key`    | `$DAIV_SANDBOX_API_KEY` | API key sent as `X-API-Key`.                                                                                                                                              |
| `--base-image` | `python:3.14-slim`      | Sandbox image. Any image with `sh`/`grep`/`find`/`ls`/`rm` works — fs ops shell out to POSIX tools and seed extraction is server-side, so the image does not need Python. |
| `--ops`        | `seed fs`               | Which suites to run (`seed`, `fs`, or both).                                                                                                                              |
| `--corpora`    | all                     | Subset of corpus names (`small`, `medium`, `large`).                                                                                                                      |
| `--iterations` | `30`                    | Measured samples per op (after warm-up).                                                                                                                                  |
| `--warmup`     | `3`                     | Discarded warm-up samples per op.                                                                                                                                         |
| `--output-dir` | `benchmarks/results`    | Where reports are written.                                                                                                                                                |

### Examples

```bash
# Quick fs-only pass against the small corpus
DAIV_SANDBOX_API_KEY=<key> uv run python -m benchmarks --ops fs --corpora small --warmup 1 --iterations 5

# Full default run (seed + fs, all three corpora, N=30)
DAIV_SANDBOX_API_KEY=<key> uv run python -m benchmarks
```

## Output

Each run writes a timestamped pair to `--output-dir`:

- `<UTC-timestamp>.md` — a human-readable table per suite (min / mean / p50 / p95 / max / stddev, in ms).
- `<UTC-timestamp>.json` — run metadata (service version, base image, pinned repo SHAs, measured file counts) plus every raw sample, for diffing over time.

`benchmarks/.cache/` (downloaded tarballs) is gitignored; `benchmarks/results/` is tracked, so
you can commit a baseline report when you want to track a number.

## What is measured

- **Corpora.** Tree-walking and `seed` ops run against three real public repos pinned to commit
  SHAs in `repos.py`: `psf/requests` (small), `pallets/flask` (medium), `django/django` (large).
  Content-sensitive ops (`read` / `write` / `edit`) use synthetic fixed-size probe files
  (1 KB, 64 KB, and 512 000 B — exactly `READ_MAX_OUTPUT_BYTES`, the read cap).
- **Methodology.** Each timed call measures only the operation; preconditions are established in
  untimed setup. `seed` uses a fresh session per sample (it is one-shot per session); `fs` ops
  share one seeded session per corpus. Probe/scratch files live under `/workspace/tmp` so the
  seeded `/workspace/repo` tree stays stable for `ls` / `grep` / `glob`. Sessions are always
  force-deleted afterward.

> **Caveats.** For `write`, the timed region includes client-side base64 serialization of the
> payload (~1 ms for the 512 KB probe) — negligible vs write latency, but a small, size-correlated
> client cost folded into absolute large-`write` numbers. `fs/*` operations that return an error
> in a 200 body (rather than failing the HTTP request) are still timed as samples; the runner sets
> up preconditions so ops should succeed, but a misconfigured run could report fast, meaningless
> numbers — sanity-check the report against expected magnitudes.

## Module map

| Module        | Responsibility                                                                       |
| ------------- | ------------------------------------------------------------------------------------ |
| `corpus.py`   | Build/obtain `Corpus` objects — cached real-repo tarballs and synthetic probe files. |
| `repos.py`    | The pinned `RepoSpec` trio.                                                          |
| `client.py`   | `BenchClient` — thin httpx wrapper over the endpoints under test.                    |
| `runner.py`   | Measurement orchestration (warm-up/iteration loops, per-op setup/teardown).          |
| `stats.py`    | Per-sample → min/mean/p50/p95/max/stddev.                                            |
| `report.py`   | Render the markdown table + dump raw-sample JSON.                                    |
| `__main__.py` | The `python -m benchmarks` CLI.                                                      |
