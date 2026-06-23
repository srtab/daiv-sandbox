# Makefile

.PHONY: help test lint lint-check lint-format lint-egress-sidecar lint-fix lint-typing run build-egress-proxy

# The egress modules copied verbatim into the mitmproxy sidecar image (see egress_proxy/Dockerfile).
# The repo targets Python 3.14 but the sidecar runs the mitmproxy base image's interpreter (3.13); a
# 3.14-only construct here is a SyntaxError that crashes the addon at import — and mitmproxy fails OPEN
# on a broken addon. lint-egress-sidecar guards that by byte-compiling them under 3.13.
EGRESS_SIDECAR_FILES := daiv_sandbox/egress/addon.py daiv_sandbox/egress/policy.py daiv_sandbox/egress/constants.py

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

test:
	uv run --all-extras pytest -s tests

lint: lint-check lint-format lint-egress-sidecar

lint-egress-sidecar:
	uv run --python 3.13 --no-project -- python -m py_compile $(EGRESS_SIDECAR_FILES)

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
