# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project Overview

`daiv-sandbox` is a FastAPI application for securely executing arbitrary commands and untrusted code
in transient Docker containers, optionally using gVisor (`runsc`) for enhanced isolation. It is
designed as a code/commands executor for DAIV agents. The stack includes Python 3.14, FastAPI,
Docker SDK, Pydantic, and the `uv` package manager.

## Commands (verified)

```bash
make test          # pytest with coverage (uv run --all-extras pytest -s tests)
make lint          # ruff check + format check + pyproject-fmt check
make lint-fix      # auto-fix ruff + format + pyproject-fmt
make lint-typing   # mypy on daiv_sandbox/
make run           # fastapi dev on port 8888
```

Single test: `uv run --all-extras pytest -s tests/unit_tests/test_main.py -k "test_name"`

`DAIV_SANDBOX_API_KEY=notsosecret` is set automatically by `pytest-env` — no `.env` needed for tests.

## Repo map

- `daiv_sandbox/main.py` — FastAPI app; all endpoints defined here
- `daiv_sandbox/sessions.py` — `SandboxDockerSession`; Docker container lifecycle and `fs/*` primitives
- `daiv_sandbox/reaper.py` — background reaper that removes stopped session containers
- `daiv_sandbox/locks.py` — optional Redis-backed per-session locking
- `daiv_sandbox/schemas.py` — Pydantic request/response models
- `daiv_sandbox/config.py` — settings (env vars, secrets from `/run/secrets`)
- `daiv_sandbox/logs.py` — logging configuration
- `tests/unit_tests/` — fast unit tests (mock Docker); **run these first**
- `tests/integration_tests/` — require a live Docker daemon; skipped in standard CI
- `scripts/dump_schemas.py` — exports JSON schemas for all request/response models

## Architecture

```mermaid
flowchart LR
    Client -->|POST /session/| FastAPI
    Client -->|POST /session/{id}/seed/| FastAPI
    Client -->|POST /session/{id}/fs/op| FastAPI
    Client -->|POST /session/{id}/| FastAPI
    Client -->|GET /session/{id}/| FastAPI
    Client -->|DELETE /session/{id}/ stop| FastAPI
    FastAPI --> SandboxDockerSession
    SandboxDockerSession -->|create / exec / copy archive| Container
    FastAPI -->|results| Client
```

The application is session-based. A session owns a single long-lived `cmd_executor` container:

- **`POST /session/`** — start a session from a `base_image`; returns a `session_id`.
- **`POST /session/{id}/seed/`** — one-shot: extract `repo_archive` into `/workspace/repo` and/or
  `skills_archive` into `/workspace/skills`.
- **`POST /session/{id}/fs/{op}`** — Python-free file operations (`ls`, `read`, `grep`, `glob`,
  `write`, `edit`, `delete`) anywhere under `/workspace`.
- **`POST /session/{id}/`** — run commands sequentially in `/workspace/repo`; returns each command's
  output. The container workspace is mutated in place; recover changes by running git (`git diff`,
  `git status`) inside `/workspace/repo` through the same endpoint.
- **`DELETE /session/{id}/`** — stop the container (preserved for warm reuse); `?force=true`
  removes it immediately. A background reaper removes stopped containers
  `DAIV_SANDBOX_SESSION_GRACE_SECONDS` after they stopped (default 12h), with an LRU cap of
  `DAIV_SANDBOX_MAX_STOPPED_SESSIONS`.
- **`GET /session/{id}/`** — 204 if the session exists (restarting it if stopped), else 404.

The container filesystem is unified under `/workspace` (`repo/`, `skills/`, `tmp/`). The sandbox is the
single source of truth: edits under `/workspace/repo` (via bash or `fs/*`) land directly on the
container's workspace, while `skills/` and `tmp/` stay container-local.

`SandboxDockerSession` (in `sessions.py`) pulls the image, creates the container as a non-root user,
copies sanitised archives in (`copy_to_container`), executes commands (`execute_command`), and exposes
the `fs/*` primitives. Per-command timeouts are enforced with `asyncio.wait_for`
(`DAIV_SANDBOX_COMMAND_TIMEOUT`, default `0` = no timeout); a timed-out command returns exit code `124`.

## Invariants / footguns

- **API is session-based (v0.5+).** The old `/run/commands/` and `/run/code/` endpoints are gone.
  Current flow: `POST /session/` → `POST /session/{id}/seed/` → `POST /session/{id}/` →
  `DELETE /session/{id}/`. Probe/warm a session with `GET /session/{id}/`.
- **`/session/{id}/seed/` is one-shot per session** and uses `multipart/form-data` (`repo_archive`,
  `skills_archive`); at least one field is required, and a second call returns `409`.
- **The sandbox is the single source of truth.** There is no server-computed patch and no
  `files/` mutation endpoint — write through the `fs/*` endpoints (or bash) and recover changes with
  git inside `/workspace/repo`.
- **`DELETE` stops, it does not remove** (the container is kept for warm reuse and reaped later);
  pass `?force=true` for immediate removal.
- **Commands run with `pipefail` enabled** — pipeline exit codes are not masked.
- **Per-command timeout** is controlled by `DAIV_SANDBOX_COMMAND_TIMEOUT` (default `0` = no timeout)
  and overridable per-request via the `timeout` field; a timed-out command returns exit code `124`.
- **`/workspace/skills` is reserved** inside containers for seeded skills — do not write there from
  session code.
- **Ruff line length is 120**, Python target `3.14`. Do not adjust these.
- **`CHANGELOG.md` must be updated** following Keep a Changelog conventions when shipping any
  functional change.

## Where changes usually go

- New endpoints → `daiv_sandbox/main.py` + `daiv_sandbox/schemas.py`
- Container behaviour and `fs/*` primitives → `daiv_sandbox/sessions.py`
- Session lifecycle / reaping → `daiv_sandbox/reaper.py` + `daiv_sandbox/config.py`
- Settings/env vars → `daiv_sandbox/config.py`

## Security & Compliance

- **API key authentication** via `X-API-Key` header (env var `DAIV_SANDBOX_API_KEY`)
- **Optional gVisor (`runsc`) runtime** for enhanced container isolation
- **Session-scoped containers** _stopped_ on `DELETE /session/{id}/` and removed by a background
  reaper after `DAIV_SANDBOX_SESSION_GRACE_SECONDS` (default 12h) or when the
  `DAIV_SANDBOX_MAX_STOPPED_SESSIONS` LRU cap is exceeded; `?force=true` removes immediately.
  Untrusted artifacts therefore persist for at most the grace window after the last use.
- **Per-command timeout** enforced via `asyncio.wait_for` (`DAIV_SANDBOX_COMMAND_TIMEOUT`, default `0` = no timeout); timed-out commands return exit code `124`
- **Secrets** loaded from `/run/secrets` or environment variables
- **License:** Apache 2.0
- **Sentry integration** for error tracking (optional, via `DAIV_SANDBOX_SENTRY_DSN`)

## Plan expectations (for agents proposing changes)

When presenting a plan, include the following:

- validate plan by running `make test` and confirming that all tests pass
- update `CHANGELOG.md` based on the present changelog conventions

## Maintenance Notes

- Update this file when:
  - `Makefile` targets change
  - the repo structure changes (new key packages/dirs)
  - tooling changes (ruff/mypy/pytest)
