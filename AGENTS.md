# AGENTS.md

This file provides guidance to agents when working with code in this repository.

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
- `daiv_sandbox/sessions.py` — `SandboxDockerSession`; Docker container lifecycle
- `daiv_sandbox/locks.py` — optional Redis-backed per-session locking
- `daiv_sandbox/schemas.py` — Pydantic request/response models
- `daiv_sandbox/config.py` — settings (env vars, secrets from `/run/secrets`)
- `daiv_sandbox/scripts.py` — shell script templates injected into containers
- `tests/unit_tests/` — fast unit tests (mock Docker); **run these first**
- `tests/integration_tests/` — require live Docker daemon; skipped in standard CI
- `scripts/dump_schemas.py` — exports JSON schemas for all request/response models

## Invariants / footguns

- **API is session-based (v0.5+).** The old `/run/commands/` and `/run/code/` endpoints are gone. Current flow: `POST /session/` → `POST /session/{id}/seed/` → `POST /session/{id}/` → `DELETE /session/{id}/`.
- **`/session/{id}/seed/` is one-shot per session** and uses `multipart/form-data` (`repo_archive`, `skills_archive`). If `extract_patch=True` was set at session creation, `seed` must always init the meta repo — even when only `skills_archive` is provided (see recent fix in `main.py`).
- **Commands run with `pipefail` enabled** — pipeline exit codes are not masked.
- **Per-command timeout** is controlled by `DAIV_SANDBOX_COMMAND_TIMEOUT` (default `0` = no timeout) and overridable per-request via the `timeout` field. SIGALRM is no longer used.
- **`/skills` is reserved** inside containers — do not write there from session code.
- **Ruff line length is 120**, Python target `3.14`. Do not adjust these.
- **`CHANGELOG.md` must be updated** following Keep a Changelog conventions when shipping any functional change.

## Where changes usually go

- New endpoints → `daiv_sandbox/main.py` + `daiv_sandbox/schemas.py`
- Container behaviour changes → `daiv_sandbox/sessions.py`
- New shell logic injected at runtime → `daiv_sandbox/scripts.py`
- Settings/env vars → `daiv_sandbox/config.py`
