#########################################################################################################
# Python compile image
#########################################################################################################
FROM python:3.14.2-slim-bookworm AS app-compiler

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
# Python build image
#########################################################################################################
FROM python:3.14.2-slim-bookworm AS python-builder

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

# Copy application code
COPY --chown=app:app ./daiv_sandbox /home/app/daiv_sandbox

USER app
WORKDIR /home/app

RUN python -m compileall daiv_sandbox

HEALTHCHECK --interval=10s \
  CMD curl --fail http://127.0.0.1:8000/-/health/ || exit 1

EXPOSE 8000

CMD ["python", "daiv_sandbox/main.py"]
