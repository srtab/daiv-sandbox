# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Commands

```bash
make test          # uv run --all-extras pytest -s tests  (unit + integration, with coverage)
make lint-fix      # ruff check --fix + ruff format + pyproject-fmt (auto-fix)
make lint-typing   # mypy on daiv_sandbox/  (local only, not a CI gate)
make run           # fastapi dev on port 8888 (reload)
```

- **Single test:** `uv run --all-extras pytest -s tests/unit_tests/test_main.py -k "test_name"`
- **Unit only (no Docker):** `uv run --all-extras pytest -s tests/unit_tests`
- `tests/integration_tests/` spin up **real containers** — need a running Docker daemon. Run the unit suite alone when no daemon is available.
- `pytest-env` sets `DAIV_SANDBOX_API_KEY=notsosecret` — no `.env` needed for tests.
- CI gate: `make lint` then `make test`.

## Architecture

**Two Docker contexts — do not conflate.** The FastAPI app is itself a container (`Dockerfile`, user `app`, talks to host Docker socket) that manages a _separate_ sandbox container per session. `settings.RUN_UID/RUN_GID` and the gVisor runtime apply to **sandbox** containers, not the service.

**A session is one long-lived sandbox container** (`SandboxDockerSession` in `sessions.py`):
- `start()` runs `entrypoint=/bin/sh -lc "sleep infinity"` (PID 1 never exits), labeled `daiv.sandbox.type=cmd_executor`. The `session_id` returned to clients **is the container id**.
- `_get_container()` **restarts a stopped container on access** (warm reuse).
- `DELETE` _stops_ by default (preserves writable layer); `?force=true` removes it. The background **reaper** (`reaper.py`) later removes stopped containers (age + LRU), re-reading state **under the per-session lock** to skip re-warmed containers (TOCTOU fix).
- All blocking Docker calls use `asyncio.to_thread`; one shared `DockerClient` (`_get_shared_client`).

**`/workspace` is the single source of truth** (`/workspace/{repo,skills,tmp}` + `/home/daiv-sandbox`). `seed` extracts archives into `repo/`+`skills/`; `run` executes in `/workspace/repo`; `fs/*` operate anywhere under `/workspace`. No server-side patch/diff — recover changes via git inside `/workspace/repo`. The one-shot seed guard marker lives in `SANDBOX_HOME` (outside `/workspace`, unreachable via `fs/*`).

**Endpoints** (`main.py`, `root_path=/api/v1`, all require `X-API-Key`): `POST /session/` → `POST /session/{id}/seed/` (multipart, one-shot, 409 if re-seeded) → `POST /session/{id}/fs/{op}` (`ls`/`read`/`grep`/`glob`/`write`/`edit`/`delete`) and `POST /session/{id}/` (run) → `GET /session/{id}/` (204 exists/warms, 404 missing) → `DELETE /session/{id}/`. Plus `GET /-/health/` and `GET /-/version/`.

**Locking.** Every session-scoped endpoint runs inside `app.state.session_lock_manager.acquire(session_id)` (`locks.py`): no-op without `REDIS_URL` (single replica); Redis lock serialises same-session requests across replicas (contention → `SessionBusyError`). The reaper uses a Redis _leader_ lock so only one replica sweeps per tick.

**Error-status contract (deliberate — keep distinct).** Missing session → `404`. Docker fault restarting/stopping an existing container → `SessionUnavailableError` → `503` (retryable infra fault, **not** masked as 404). Lock contention → `409`. Bad API key → `403`.

**`fs/*` is Python-free and defends a container boundary, not a path prefix.** Content moves via the Docker archive API; search/listing shells out to POSIX `grep`/`find`/`ls`/`rm` (works on `alpine`, no Python). `_validate_sandbox_path` rejects `..`/NUL/newlines and confines to `/workspace`, but is **lexical** — the real trust boundary is the sandbox container (gVisor/non-root), not the prefix. `fs/read` caps at `READ_MAX_OUTPUT_BYTES` (512 KB); `fs/write` is create-only. Outcomes are reported in the 200 body via a structured `error` (`{code, message}`, codes from `FsErrorCode` in `schemas.py`): `not_found` (distinct from empty listing/no-match), `not_a_directory`/`is_a_directory`, `invalid_path`; `fs/delete` reports `removed`. HTTP codes stay reserved for session/transport concerns (404/409/503/403/500).

**Untrusted-input handling.** Archives are sanitised before extraction (`_sanitize_archive_stream`): symlinks/hardlinks/device nodes/FIFOs and absolute/`..` paths rejected, ownership normalised to `RUN_UID/RUN_GID`, stream spooled via `SpooledTemporaryFile`. Run commands use a portable `pipefail` wrapper (pipeline exit codes not masked); per-command timeout via `asyncio.wait_for` (timed-out → exit `124`, remaining commands skipped).

## Module map (`daiv_sandbox/`)

- `main.py` — FastAPI app, all endpoints, `lifespan` (wires lock manager + reaper), Sentry init, exception handlers.
- `sessions.py` — `SandboxDockerSession`: container lifecycle, archive copy/sanitisation, command exec, `fs/*` primitives; path constants and `_validate_sandbox_path`.
- `reaper.py` — background loop removing stopped session containers (age + LRU, leader-locked).
- `locks.py` — `NoopSessionLockManager` / `RedisSessionLockManager` and `SessionBusyError`.
- `config.py` — `settings` singleton (pydantic-settings).
- `schemas.py` — request/response models, `FsErrorCode`.
- `logs.py` — logging configuration.

## Conventions

- All settings come from `DAIV_SANDBOX_`-prefixed env vars (or files in `/run/secrets`) via the `settings` singleton. `DAIV_SANDBOX_API_KEY` is required.
- Sandbox containers always run as non-root (`RUN_UID:RUN_GID`); passing `user` to `_start_container` is rejected on purpose.
- Ruff: line length 120, target Python 3.14, `preview = true`. `make lint-fix` before committing.
- `CHANGELOG.md` follows Keep a Changelog — update for any user-facing/functional change.
