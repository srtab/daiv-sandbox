# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Overview

`daiv-sandbox` is the code/commands executor for [DAIV](https://github.com/srtab/daiv) agents — a
FastAPI service that runs untrusted commands and file operations inside per-session Docker containers,
optionally hardened with gVisor (`runsc`). Stack: Python 3.14, FastAPI, Docker SDK, Pydantic, `uv`.

## Commands (verified in `Makefile`)

```bash
make test                # uv run --all-extras pytest -s tests  (unit + integration)
make lint                # ruff check + format + sidecar 3.13 parse check  (CI gate)
make lint-typing         # mypy on daiv_sandbox/  (not a CI gate)
make run                 # fastapi dev on :8888 --reload
make build-egress-proxy  # builds the mitmproxy sidecar image
```

- Single test: `uv run --all-extras pytest -s tests/unit_tests/test_main.py -k "test_name"`
- Unit only (no Docker): `uv run --all-extras pytest -s tests/unit_tests`
- `tests/integration_tests/` needs a Docker daemon. `pytest-env` sets `DAIV_SANDBOX_API_KEY=notsosecret`.
- See `README.md` for the full settings table; `DAIV_SANDBOX_API_KEY` is required.

## Architecture (high-leverage only)

- **Two Docker contexts, do not conflate them.** The FastAPI service container (`Dockerfile`, runs as
  `app`, owns the host Docker socket) is separate from the **sandbox** container it manages per
  session. `settings.RUN_UID/RUN_GID` and the gVisor runtime apply to the sandbox, not the service.
- **One session = one long-lived sandbox container.** The `session_id` returned to clients **is the
  container id**. `_get_container()` restarts a stopped container on access (warm reuse); `DELETE`
  stops it (preserves the writable layer) unless `?force=true`; the **reaper** (`reaper.py`) later
  removes stopped containers. See `daiv_sandbox/sessions.py` and `daiv_sandbox/reaper.py`.
- **`/workspace/{repo,skills,tmp}` is the single source of truth in the sandbox** (constants
  `SANDBOX_ROOT`, `SKILLS_ROOT`, `SCRATCH_ROOT` in `sessions.py`; `SANDBOX_HOME` lives outside
  `/workspace` and holds the one-shot seed-guard marker). There is no server-side patch/diff — recover
  changes by running git inside `/workspace/repo`.
- **Egress is mandatory for network access, never direct.** Sessions without an `egress` block run
  with `network_mode=none`; sessions with one route through a per-session mitmproxy sidecar triad
  (network + sidecar + CA), managed by `daiv_sandbox/egress/manager.py`. Missing CA on the server →
  `400`; deny-default empty policy → `422`.

## Invariants / footguns

- **`egress/{addon,policy,constants}.py` run under Python 3.13 in the sidecar image, not 3.14.**
  They are copied verbatim by `egress_proxy/Dockerfile`. They must stay stdlib-only and free of
  3.14-only syntax (e.g. unparenthesized `except` tuples — PEP 758 is 3.14+). A 3.14-only construct
  is a `SyntaxError` under 3.13 that crashes the addon at import — and mitmproxy fails OPEN on a
  broken addon. `make lint-egress-sidecar` (part of `make lint`) byte-compiles them under 3.13 to
  catch this. This is the highest-leverage rule in the repo.
- **Sandbox containers always run non-root** (`RUN_UID:RUN_GID`); passing `user` to `_start_container`
  is rejected on purpose. `fs/*` defends a container trust boundary (gVisor / non-root), not a path
  prefix — `_validate_sandbox_path` is lexical and does not resolve symlinks.
- **Error-status contract is deliberate.** Missing session → `404`; Docker fault on
  restart/stop → `503` (`SessionUnavailableError`, retryable, **not** masked as 404); lock contention
  → `409`; auth → `403`; `egress` block without server-side CA → `400`; empty deny-default policy
  → `422`. Per-op `fs/*` outcomes (e.g. `not_found`, `is_a_directory`) live in the 200 body, not
  the HTTP status.
- **Run commands pipe through a portable `pipefail` wrapper** so pipeline exits aren't masked; per-
  command timeout is enforced via `asyncio.wait_for` (timed-out → exit `124`, remaining skipped).
- **Uploaded archives are sanitised** (`_sanitize_archive_stream` in `sessions.py`): symlinks,
  hardlinks, devices, FIFOs, absolute paths, and `..` are rejected; ownership is normalised to
  `RUN_UID/RUN_GID`; the stream spools through a `SpooledTemporaryFile`.
- **Ruff:** line length 120, target Python 3.14, `preview = true`. Update `CHANGELOG.md` (Keep a
  Changelog) for any user-facing/functional change. `make lint-fix` before committing.
