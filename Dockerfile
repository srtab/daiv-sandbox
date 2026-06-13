#########################################################################################################
# Python compile image
#########################################################################################################
FROM python:3.14.5-slim-bookworm AS app-compiler

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  # dependencies for building Python packages
  build-essential

# Install uv
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#installing-uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Create a virtual environment and make it relocatable
RUN uv venv .venv --relocatable

# Install uv
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-editable --no-group dev --no-install-project

#########################################################################################################
# ripgrep fetch image
#########################################################################################################
# Static (musl) ripgrep binaries for BOTH supported task-image architectures. These are injected at
# session-start into the (arbitrary, possibly Python-free, possibly musl/alpine) task container so
# fs/grep can run a real regex engine there; a static musl build runs on glibc *and* musl images of
# the matching arch (a glibc-linked aarch64 build would not run on alpine arm64).
#
# Source: microsoft/ripgrep-prebuilt — the static-musl ripgrep distribution VS Code ships. We use it
# (rather than BurntSushi/ripgrep's own releases) because upstream does NOT publish an
# aarch64-unknown-linux-musl asset; it only ships aarch64 as glibc-linked. Version + per-tarball
# sha256 are pinned below and VERIFIED before extraction. To bump: download the new
# `ripgrep-vX-<arch>-unknown-linux-musl.tar.gz`, `sha256sum` each tarball, and update the pins here.
FROM debian:bookworm-slim AS ripgrep-fetch

ARG RIPGREP_VERSION=15.0.1
# sha256 of the *.tar.gz release assets (verified by downloading the assets and hashing them).
ARG RIPGREP_X86_64_SHA256=4499958bfd5252df3d9e7504127fd448e4a14fbf2805ef4f14baaa1bcf775188
ARG RIPGREP_AARCH64_SHA256=dd3738a4b6e8df0fb3bc3edc5af352c4c39e0d97ad118a23e5176bdc5d48ba08

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
  base="https://github.com/microsoft/ripgrep-prebuilt/releases/download/v${RIPGREP_VERSION}"; \
  mkdir -p /opt/rg/x86_64 /opt/rg/aarch64; \
  # x86_64 (x86_64-unknown-linux-musl)
  curl -fSL -o /tmp/rg-x86_64.tar.gz "${base}/ripgrep-v${RIPGREP_VERSION}-x86_64-unknown-linux-musl.tar.gz"; \
  echo "${RIPGREP_X86_64_SHA256}  /tmp/rg-x86_64.tar.gz" | sha256sum -c -; \
  tar -xzf /tmp/rg-x86_64.tar.gz -C /opt/rg/x86_64 rg; \
  # aarch64 (aarch64-unknown-linux-musl)
  curl -fSL -o /tmp/rg-aarch64.tar.gz "${base}/ripgrep-v${RIPGREP_VERSION}-aarch64-unknown-linux-musl.tar.gz"; \
  echo "${RIPGREP_AARCH64_SHA256}  /tmp/rg-aarch64.tar.gz" | sha256sum -c -; \
  tar -xzf /tmp/rg-aarch64.tar.gz -C /opt/rg/aarch64 rg; \
  chmod 0755 /opt/rg/x86_64/rg /opt/rg/aarch64/rg; \
  rm -f /tmp/rg-x86_64.tar.gz /tmp/rg-aarch64.tar.gz

#########################################################################################################
# Python build image
#########################################################################################################
FROM python:3.14.5-slim-bookworm AS python-builder

LABEL maintainer="srtabs@gmail.com"

ARG APP_UID=1001  # Default application UID, override during build or run
ARG APP_GID=1001  # Default application GID, override during build or run
ARG DOCKER_GID=991  # Default docker GID, override during build or run

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  # Used on healthcheckers
  curl \
  # Cleaning up unused files
  && apt-get purge -y --auto-remove \
  -o APT::AutoRemove::RecommendsImportant=0 \
  -o APT::Autoremove::SuggestsImportant=0 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* /var/cache/* \
  # Create docker group to allow docker socket access
  && addgroup --gid $DOCKER_GID docker \
  # Create aplication specific user
  && addgroup --system --gid $APP_GID app \
  && adduser --system --ingroup app --uid $APP_UID --home /home/app app \
  && adduser app docker

ENV PATH="/home/app/.venv/bin:$PATH"
ENV PYTHONPATH="$PYTHONPATH:/home/app/daiv_sandbox/"
ENV PYTHONUNBUFFERED=1

# Copy python compiled requirements
COPY --chown=app:app --from=app-compiler /.venv /home/app/.venv

# Static ripgrep binaries, one per task-image arch, injected into task containers at session start
# (see sessions.py::_inject_ripgrep). Kept root-owned and world-readable/executable (0755).
COPY --from=ripgrep-fetch /opt/rg /opt/rg

# Copy application code
COPY --chown=app:app ./daiv_sandbox /home/app/daiv_sandbox

USER app
WORKDIR /home/app

RUN python -m compileall daiv_sandbox

HEALTHCHECK --interval=10s \
  CMD curl --fail http://127.0.0.1:8000/-/health/ || exit 1

EXPOSE 8000

CMD ["python", "daiv_sandbox/main.py"]
