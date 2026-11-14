# Makefile

.PHONY: help test lint lint-check lint-format lint-egress-sidecar lint-browser-scripts lint-fix lint-typing run build-egress-proxy build-browser test-relay

# The egress modules copied verbatim into the mitmproxy sidecar image (see egress_proxy/Dockerfile).
# The repo targets Python 3.14 but the sidecar runs the mitmproxy base image's interpreter (3.13); a
# 3.14-only construct here is a SyntaxError that crashes the addon at import — and mitmproxy fails OPEN
# on a broken addon. lint-egress-sidecar guards that by byte-compiling them under 3.13.
EGRESS_SIDECAR_FILES := daiv_sandbox/egress/addon.py daiv_sandbox/egress/policy.py daiv_sandbox/egress/constants.py
# The browser image runs these under Debian trixie's python3 (3.13).
BROWSER_SCRIPTS := browser/launch.py browser/save_tabs.py

help:
	@echo "Available commands:"
	@echo "  make test                 - Run tests with coverage report"
	@echo "  make lint                 - Run lint check, format check, and sidecar 3.13 parse check"
	@echo "  make lint-check           - Run lint check only (ruff)"
	@echo "  make lint-format          - Check code formatting"
	@echo "  make lint-egress-sidecar  - Verify sidecar-shared egress modules parse under Python 3.13"
	@echo "  make lint-fix             - Fix linting and formatting issues"
	@echo "  make lint-typing          - Run type checking with mypy"
	@echo "  make run                  - Run the application"
	@echo "  make build-egress-proxy   - Build the per-session egress proxy sidecar image"
	@echo "  make build-browser        - Build the browser image (ghcr.io/srtab/daiv-sandbox-browser:dev)"
	@echo "  make test-relay           - Run daiv-relay's Go tests (in a golang container)"
	@echo "  make lint-browser-scripts - Verify the browser image scripts parse under Python 3.13"

test:
	uv run --all-extras pytest -s tests

lint: lint-check lint-format lint-egress-sidecar lint-browser-scripts

lint-egress-sidecar:
	uv run --python 3.13 --no-project -- python -m py_compile $(EGRESS_SIDECAR_FILES)

lint-browser-scripts:
	uv run --python 3.13 --no-project -- python -m py_compile $(BROWSER_SCRIPTS)

lint-check:
	uv run --only-group=dev ruff check .

lint-format:
	uv run --only-group=dev ruff format . --check
	uv run --only-group=dev pyproject-fmt pyproject.toml --check

lint-fix:
	uv run --only-group=dev ruff check . --fix
	uv run --only-group=dev ruff format .
	uv run --only-group=dev pyproject-fmt pyproject.toml

lint-typing:
	uv run --only-group=dev mypy daiv_sandbox

run:
	uv run fastapi dev daiv_sandbox/main.py --reload --port 8888

build-egress-proxy:  ## Build the per-session egress proxy sidecar image
	docker build -f egress_proxy/Dockerfile -t ghcr.io/srtab/daiv-sandbox-egress:latest .

build-browser:  ## Build the browser image run beside a session's sandbox
	docker build -t ghcr.io/srtab/daiv-sandbox-browser:dev -f browser/Dockerfile .

test-relay:  ## Run daiv-relay's Go tests; Go runs in a container, so the host needs only Docker
	docker run --rm -v "$(CURDIR)/relay:/src" -w /src golang:1.25-trixie sh -c 'test -z "$$(gofmt -l .)" && go vet ./... && go test -race ./...'
