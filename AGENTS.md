# AGENTS.md

This file provides guidance to agents (Claude Code and others) when working with code in this repository.

## Overview

`daiv-sandbox` is a FastAPI service that executes untrusted commands and file operations inside
transient Docker containers, optionally hardened with the gVisor (`runsc`) runtime. It is the
code/commands executor for [DAIV](https://github.com/srtab/daiv) agents. Stack: Python 3.14, FastAPI,
the Docker SDK, Pydantic, and the `uv` package manager.

## Commands

```bash
make test          # uv run --all-extras pytest -s tests  (unit + integration, with coverage)
make lint-fix      # check lint and auto-fix ruff + format + pyproject-fmt
make lint-typing   # mypy on daiv_sandbox/
make run           # fastapi dev on port 8888 (reload)
```

- **Single test:** `uv run --all-extras pytest -s tests/unit_tests/test_main.py -k "test_name"`
- **Unit only (no Docker needed):** `uv run --all-extras pytest -s tests/unit_tests`
- `tests/integration_tests/` spin up **real containers** and require a running Docker daemon; `make test`
  runs them (CI runs `make test` on a Docker-enabled runner). Run the unit suite alone when no daemon is
  available.
- `pytest-env` sets `DAIV_SANDBOX_API_KEY=notsosecret` automatically — no `.env` needed for tests.
- CI gate is `make lint` then `make test`; `make lint-typing` (mypy) is available locally but is not a CI gate.
  `make lint` also runs `make lint-egress-sidecar`, which byte-compiles the sidecar-shared `egress/` modules
  under Python 3.13 (see the Module map note on the dual runtime — a 3.14-only construct there fails open).

## Architecture

**Two distinct Docker contexts — do not conflate them.** The FastAPI app is itself one container
(`Dockerfile`, runs as user `app`, talks to the host Docker socket). It manages a _separate_ sandbox
container per session, in which untrusted code runs. `settings.RUN_UID/RUN_GID` and the gVisor runtime
apply to the **sandbox** containers, not the service.

**A session is one long-lived sandbox container.** `SandboxDockerSession` (`sessions.py`) wraps it:

- `start()` pulls the image and runs a container with `entrypoint=/bin/sh -lc "sleep infinity"` (PID 1
  never exits on its own), labeled `daiv.sandbox.type=cmd_executor`. The `session_id` returned to clients
  **is the container id**.
- On every later request, `SandboxDockerSession(session_id=...)` looks the container up via
  `_get_container()`, which **restarts a stopped container on access** (warm reuse).
- `DELETE` _stops_ the container by default (`stop_container`), preserving its writable layer; `?force=true`
  removes it. The background **reaper** (`reaper.py`) later removes stopped containers.
- All blocking Docker calls are wrapped in `asyncio.to_thread`, and a single `DockerClient` is shared
  across the process (`_get_shared_client`).

**`/workspace` is the single source of truth.** The container layout is `/workspace/{repo,skills,tmp}`
(constants `SANDBOX_ROOT`, `SKILLS_ROOT`, `SCRATCH_ROOT`) plus `/home/daiv-sandbox` (`SANDBOX_HOME`).
`seed` extracts archives into `repo/` and `skills/`; `run` executes commands in `/workspace/repo`;
`fs/*` operate anywhere under `/workspace`. There is no server-side patch/diff — recover changes by
running git inside `/workspace/repo`. The one-shot seed guard marker lives in `SANDBOX_HOME` (outside
`/workspace`, so it's unreachable through `fs/*`).

**Endpoints** (`main.py`, all under `root_path=/api/v1`, all require the `X-API-Key` header):
`POST /session/` (include an `egress` block to attach the per-session egress proxy at create time) →
`POST /session/{id}/seed/` (multipart, one-shot, 409 if re-seeded) →
`POST /session/{id}/fs/{op}` (`ls`/`read`/`grep`/`glob`/`write`/`edit`/`delete`) and
`POST /session/{id}/` (run commands) → `GET /session/{id}/` (204 exists/warms, 404 missing) →
`DELETE /session/{id}/`. Plus `GET /-/health/` and `GET /-/version/`.

**Concurrency & locking.** Every session-scoped endpoint runs inside
`app.state.session_lock_manager.acquire(session_id)` (`locks.py`). Without `REDIS_URL` this is a no-op
(single replica); with it, a Redis lock serialises requests for the same session across replicas, and a
request that can't acquire it within the wait window raises `SessionBusyError`. The reaper uses a Redis
_leader_ lock so only one replica sweeps per tick.

**The reaper** (`reaper.py`) sweeps on a fixed cadence: it lists non-running cmd-executor containers,
removes those whose `State.FinishedAt` is older than `SESSION_GRACE_SECONDS`, then LRU-evicts any beyond
`MAX_STOPPED_SESSIONS`. Removal re-reads container state **under the per-session lock** and skips a
container that has been warmed again — closing a TOCTOU race against in-flight requests.

**Error-status contract (deliberate — keep these distinct).** Missing session → `404`. A Docker fault
while restarting/stopping an existing container → `SessionUnavailableError` → `503` (retryable infra
fault, **not** masked as 404). Lock contention → `SessionBusyError` → `409`. Missing/invalid API key →
`403`. A `POST /session/` carrying an `egress` block on a deployment without the egress CA configured →
`400` (egress is mandatory for network access; there is no direct-network attach). A permits-nothing
egress policy (deny-default, no rules) → `422`.

**`fs/*` is Python-free and defends a container boundary, not a path prefix.** File content moves via
the Docker archive API; search/listing shells out to POSIX `grep`/`find`/`ls`/`rm`, so the endpoints work
on minimal images (e.g. `alpine`) with no Python interpreter. `_validate_sandbox_path` rejects `..`, NUL,
and newlines and confines paths to `/workspace`, but the check is **lexical** — it does not resolve
symlinks. The real trust boundary is the sandbox container (gVisor / non-root), not the `/workspace`
prefix. `fs/read` caps a single response at `READ_MAX_OUTPUT_BYTES` (512 KB); `fs/write` is create-only.
`fs/*` outcomes are reported in the 200 body via a structured `error` object (`{code, message}`,
codes from `FsErrorCode` in `schemas.py`): absence is `not_found` (distinct from an empty
listing/no-match), a type mismatch is `not_a_directory`/`is_a_directory`, a malformed path is
`invalid_path`, and `fs/delete` reports a `removed` boolean. HTTP status codes remain reserved for
session/transport concerns (404 missing session, 409 lock, 503 infra, 403 auth, 400 network-without-egress, 500 unexpected).

**Untrusted-input handling.** Uploaded archives are sanitised before extraction (`_sanitize_archive_stream`):
symlinks/hardlinks/device nodes/FIFOs and absolute/`..` paths are rejected, ownership is normalised to
`RUN_UID/RUN_GID`, and the stream spools through a `SpooledTemporaryFile` instead of being buffered whole.
Run commands execute through a portable `pipefail` wrapper so pipeline exit codes aren't masked, with a
per-command timeout enforced by `asyncio.wait_for` (timed-out command → exit code `124`, remaining
commands skipped).

## Module map (`daiv_sandbox/`)

- `main.py` — FastAPI app, all endpoints, `lifespan` (wires the lock manager + reaper), Sentry init, exception handlers.
- `sessions.py` — `SandboxDockerSession`: container lifecycle, archive copy/sanitisation, command exec, and the `fs/*` primitives; path constants and `_validate_sandbox_path`.
- `reaper.py` — background loop that removes stopped session containers (age + LRU, leader-locked) and reclaims orphaned egress triads (every tick, ungated).
- `locks.py` — `NoopSessionLockManager` / `RedisSessionLockManager` and `SessionBusyError`.
- `config.py` — `settings` singleton (pydantic-settings).
- `schemas.py` — request/response models.
- `logs.py` — logging configuration.
- `egress/manager.py` — `EgressProxyManager`: triad lifecycle (internal network + sidecar create / discover / provision / idempotent teardown).
- `egress/policy.py` — sidecar-side `PolicyEvaluator` (allow/deny, intercept mode, per-host method limits, credential injection) and the mtime-reloading, fail-closed `PolicyStore`.
- `egress/addon.py` — the mitmproxy addon: enforces reachability at `CONNECT`, passthrough at `tls_clienthello`, blocks/injects at `request`; fail-closed against mitmproxy's fail-open hook behavior.
- `egress/constants.py` — shared paths/labels for the triad.
- **Dual runtime:** `egress/{policy,addon,constants}.py` are copied verbatim into the **Python 3.13** mitmproxy sidecar while the repo targets 3.14. They must stay stdlib-only and free of 3.14-only syntax — a 3.14-only construct is a `SyntaxError` under 3.13 that makes the addon **fail open**. `make lint-egress-sidecar` (folded into `make lint`) byte-compiles them under 3.13 to catch this.

## Conventions

- **Config:** all settings come from `DAIV_SANDBOX_`-prefixed env vars (or files in `/run/secrets`) via the
  `settings` singleton in `config.py`. See the README table for the full list; `DAIV_SANDBOX_API_KEY` is required.
- **Sandbox containers always run as a non-root user** (`RUN_UID:RUN_GID`); passing `user` to
  `_start_container` is rejected on purpose.
- **Ruff:** line length 120, target Python 3.14, `preview = true`. `make lint-fix` before committing.
- **`CHANGELOG.md`** follows Keep a Changelog and must be updated for any user-facing/functional change.
