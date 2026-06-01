# Project Overview

`daiv-sandbox` is a FastAPI application for securely executing arbitrary commands and untrusted code in transient Docker containers, optionally using gVisor (`runsc`) for enhanced isolation. It is designed as a code/commands executor for DAIV agents. The stack includes Python 3.14, FastAPI, Docker SDK, Pydantic, and `uv` package manager.

## Repository Structure

```
- daiv_sandbox/       Core FastAPI application (main.py, config.py, sessions.py, etc.)
- tests/              Pytest test suite for unit tests.
- .github/workflows/  GitHub Actions for CI (lint, test) and Docker image publishing.
- Dockerfile          Multi-stage Docker build for production deployment.
```

## Build & Development Commands

All commands should be invoked via `make` (see `Makefile`).

### Linting & formatting

```bash
# Run all lint checks (ruff check + format check + pyproject-fmt)
make lint

# Auto-fix linting and formatting issues
make lint-fix

# Run type checking with mypy
make lint-typing
```

### Testing

```bash
# Run tests with coverage report
make test
```

## Code Style & Conventions

- **Formatter/linter:** ruff (line length 120, target Python 3.14 — inferred from `requires-python`)
- **Import sorting:** isort rules via ruff
- **pyproject.toml formatting:** pyproject-fmt
- **EditorConfig:** UTF-8, LF endings, 4-space indent for Python
- **Commit messages:** present tense, imperative mood, ≤72 char first line
- **Branch naming:** `feat/`, `fix/`, `chore/`, `security/` prefixes

## Architecture Notes

```mermaid
flowchart LR
    Client -->|POST /session/| FastAPI
    Client -->|POST /session/{id}/seed/| FastAPI
    Client -->|POST /session/{id}/fs/op| FastAPI
    Client -->|POST /session/{id}/| FastAPI
    Client -->|DELETE /session/{id}/| FastAPI
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
- **`DELETE /session/{id}/`** — tear down the container.

The container filesystem is unified under `/workspace` (`repo/`, `skills/`, `tmp/`). The sandbox is the
single source of truth: edits under `/workspace/repo` (via bash or `fs/*`) land directly on the
container's workspace, while `skills/` and `tmp/` stay container-local.

`SandboxDockerSession` (in `sessions.py`) pulls the image, creates the container as a non-root user,
copies sanitised archives in (`copy_to_container`), executes commands (`execute_command`), and exposes
the `fs/*` primitives. Per-command timeouts are enforced with `asyncio.wait_for`
(`DAIV_SANDBOX_COMMAND_TIMEOUT`, default `0` = no timeout); a timed-out command returns exit code `124`.

## Testing Strategy

- **Framework:** `pytest` with `pytest-cov`, `pytest-mock`, `pytest-xdist`
- **Coverage config:** `.coveragerc`, source is `daiv_sandbox/`
- **Test files are in `tests/` directory.**

### Canonical test command

```bash
make test
```

### Common pytest workflows (if running pytest directly)

```bash
# Run all tests
pytest

# Run a specific file
pytest tests/test_<name>.py

# Run a specific test function
pytest tests/test_<name>.py -k "<test_name_substring>"

# Run by keyword
pytest -k "<keyword>"

# Show extra detail on failures
pytest -vv
```

## Security & Compliance

- **API key authentication** via `X-API-Key` header (env var `DAIV_SANDBOX_API_KEY`)
- **Optional gVisor (`runsc`) runtime** for enhanced container isolation
- **Session-scoped containers** torn down on `DELETE /session/{id}/` (along with the shared volume)
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
