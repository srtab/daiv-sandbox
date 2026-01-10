# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Added `memory_bytes` parameter to start session request to allow specifying the memory limit for the session.
- Added `cpus` parameter to start session request to allow specifying the CPU limit for the session.
- Added `network_enabled` parameter to start session request to allow specifying whether to enable network for the session.
- Added integration tests to the project.

### Changed

- Improved patch extraction performance by using shared Docker volumes instead of copying full workspace trees between containers.
- Changed run container command `sleep` time to 1 hour to avoid containers being removed too early.
- Changed `persist_workdir` parameter to `ephemeral` in start session request. **Breaking change**

### Removed

- Removed `workdir` parameter from start session request. **Breaking change**

## [0.3.1] - 2025-12-13

### Changed

- Standardized sandbox filesystem root to `/archives` for all containers, removing dependency on base image WORKDIR.
- Removed image inspection logic (`_inspect_image` method and `ImageAttrs` usage in sessions).
- Updated permission normalization to use `chmod` instead of `chown`, improving compatibility with non-root users.
- Fixed patch extraction logic to correctly handle `None` workdir by treating it as "." relative to archive root.

## [0.3.0] - 2025-12-12

### Added

- Added `AGENTS.md` file to the project.
- Added `SENTRY_ENABLE_LOGS`, `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE`, and `SENTRY_SEND_DEFAULT_PII` settings to configure Sentry.

### Changed

- Moved `extract_patch` to start session request to avoid starting a patch extractor side-car container if not needed. **Breaking change**
- `extract_patch` parameter now forces the archive to be a git repository. **Breaking change**
- Migrated project from Python 3.12 to Python 3.14. **Breaking change**
- Updated dependencies.
- Replaced `SENTRY_ENABLE_TRACING` setting with `SENTRY_TRACES_SAMPLE_RATE` and `SENTRY_PROFILES_SAMPLE_RATE`. **Breaking change**

### Fixed

- Fixed `.daiv.yml` schema to the newer version of `daiv`.

## [0.2.0] - 2025-08-31

### Added

- Added `extract_patch` parameter to allow extracting a patch with the changes made by the executed commands.
- Added `fail_fast` parameter to allow stopping execution immediately if any command fails.
- Added build and push the Docker image to GitHub Container Registry on push to main branch.

### Changed

- Changed run commands endpoint to use session like endpoints. Now it's possible to run multiple commands through multiple requests using the same session. **Breaking change.**
- The response of the run commands endpoint now contains the patch of the changes made by the executed commands, instead of the full archive with the changed files. **Breaking change.**
- Updated documentation in `README.md` to reflect the new changes.
- Ignored `directory "/run/secrets" does not exist` warning.
- Updated dependencies.

### Fixed

- Fixed logging configuration to avoid logging health check requests to the console.

### Removed

- Removed support to run arbitrary code. **Breaking change.**

## [0.1.1] - 2025-05-13

### Changed

- Migrated `pyproject.toml` to use standard dev dependencies group declaration.
- Updated urls declared in `pyproject.toml` to use standard labels.
- Updated sensible `pydantic` settings to use `SecretStr` to avoid exposing sensitive information.

## [0.1.0] - 2025-04-29

### Added

- Added `CONTRIBUTING.md` file to provide guidelines for contributing to the project.

### Changed

- Changed Python base image from `python:3.12-slim` to `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`.
- Migrated from pip to uv for Python package management.
- Removed separate dependency installation step and integrated it directly into the code execution process.
- Updated `run_code` method in `PythonLanguageManager` to accept dependencies as a parameter and incorporate them directly into the code file using uv's script dependencies format.

### Fixed

- Sometimes the container was not running even when open was called, causing issues when trying to do operations on it. Now the container is ensured to be running before any operation is done.

## [0.1.0-rc.10] - 2024-12-30

### Changed

- Moved `LANGUAGE_BASE_IMAGES` from `daiv_sandbox/main.py` to `daiv_sandbox/languages.py`.

### Fixed

- Changed strategy to determine where the run will execute inside the container. Now the default user and working directory are considered to avoid privileges issues.

## [0.1.0-rc.9] - 2024-12-27

### Fixed

- Fixed issue when images have limited privileges.

### Chore:

- Updated dependencies:
  - `ipython` from 8.30 to 8.31
  - `pydantic` from 2.10.3 to 2.10.4
  - `pydantic-settings` from 2.6.1 to 2.7.0
  - `ruff` from 0.8.2 to 0.8.4
  - `mypy` from 1.13.0 to 1.14.0

## [0.1.0-rc.8] - 2024-12-27

### Added

- Added `HOST` and `PORT` settings to allow overriding the host and port of the service.
- Added `LOG_LEVEL` setting to allow overriding the log level of the service.

### Fixed

- Fixed logging configuration for `daiv_sandbox` logger, no logs where being written to the console.
- Fixed `SENTRY_ENABLE_TRACING` setting to be a boolean or an integer.

## [0.1.0-rc.7] - 2024-12-16

### Added

- Added `ping` method to `SandboxDockerSession` to check if the Docker client is responding.

### Changed

- Changed `health` endpoint to check if the Docker client is responding and avoid starting the service if it is not responding.
- Changed default `DOCKER_GID` to `991`.

## [0.1.0-rc.6] - 2024-12-12

### Added

- Added `SENTRY_ENABLE_TRACING` configuration to enable Sentry tracing.
- Added `EXPOSE 8000` to the `Dockerfile` to explicitly expose the port.

### Changed

- Updated dependencies:
  - `ipython` from 8.29 to 8.30
  - `pyopenssl` from 24.2.1 to 24.3.0
  - `ruff` from 0.8.0 to 0.8.2

## [0.1.0-rc.5] - 2024-12-11

### Added

- Added `Dockerfile` args to allow overriding the application UID and GID, and docker GID.

### Fixed

- Fixed the `Dockerfile` to create the `app` user with the correct group and user IDs to avoid permission issues.
- Fixed the `Dockerfile` to create the `docker` group with the correct GID to allow the `app` user to access the docker socket.

## [0.1.0-rc.4] - 2024-12-07

### Added

- Added `HEALTHCHECK` to the `Dockerfile`.

### Fixed

- Fixed `Dockerfile` to create the `app` user with the correct home directory defined.

### Changed

- Changed `/health/` endpoint to `/-/health/`.
- Changed `/version/` endpoint to `/-/version/`.

## [0.1.0-rc.3] - 2024-12-07

### Changed

- Improved `Dockerfile` for production use.
- Updated dependencies:
  - `fastapi`;
  - `pydantic`;
  - `sentry-sdk`.

### Fixed

- Fixed issue on `run_id` being passed as an `UUID` to the `SandboxDockerSession` class instead of a `str`.
- Fixed missing `curl` dependency on `Dockerfile` for healthcheck.

## [0.1.0-rc.2] - 2024-11-26

### Added

- Added endpoint to run python code.

### Changed

- Improved `README.md` to include required security configuration options to use `gVisor` as the container runtime.
- Changed folder where runs are stored to `/runs` instead of `/tmp`.
- Changed `execute_command` to extract changed files even if the command fails.
- Changed `execute_command` to allow conditionally extracting changed files.
- Renamed `ForbiddenError` to `ErrorMessage` to be more generic.
- Updated dependencies:
  - `ruff` from 0.7.4 to 0.8.0
  - `pydantic` from 2.10.0 to 2.10.2
  - `sentry-sdk` from 2.18.0 to 2.19.0

### Removed

- Removed `mounts` parameter from `SandboxDockerSession` because it was not being used.

## [0.1.0-rc.1] - 2024-11-20

### Added

- Added logging to the application.
- Added `__version__` to the project.
- Added `health` endpoint to check if the service is healthy.
- Added `version` endpoint to get the version of the service.
- Added API Key authentication to command run endpoint.
- Added more metadata to the OpenAPI schema.
- Added support to pass a `workdir` to the command run endpoint.
- Added to settings `KEEP_TEMPLATE` to allow keeping image templates after command execution.
- Added to settings `RUNTIME` to allow choosing the container runtime.

### Changed

- Changed `Pydantic` models to specific `schemas.py` file.
- Changed way to declare `root_path` of endpoints to be more maintainable.
- Changed the way to extract changed files from the container, now it returns changed files by the executed command.
- Changed `README.md` to include usage examples, security information and configuration options.
- Changed `settings` to support loading secrets from `/run/secrets` directory.
- Changed `settings` to prefix all environment variables with `DAIV_SANDBOX_`.
- Moved `ipython` dependency to `dev-dependencies`.

### Removed

- Removed `--workers` from the `CMD` in the `Dockerfile` to allow scaling using docker replicas.
- Removed `PROJECT_NAME` from the configuration.
- Removed `get-docker-secret` dependency.
- Removed `python-decouple` dependency.
- Removed `python-multipart` dependency.

### Fixed

- Fixed issue on command quoting using `shlex.quote`, which was causing double quoting of the command.
- Fixed issue on extracting changed files from the container, it was returning a `tar` inside another `tar`.
- Fixed Docker image with `latest` tag not being pushed to the repository.

## [0.1.0-alpha.2] - 2024-11-18

### Changed

- Changed `execute_command` method to use `/bin/sh -c` to properly handle shell quoting.

### Fixed

- Fixed issue extracting changed files from the container when command exited with non-zero code.
- Fixed `containers.run` method to use `tty=True` to properly handle interactive sessions.

## [0.1.0-alpha] - 2024-11-16

### Added

- Initial release of the daiv-sandbox project.
- Implemented core functionalities for sandbox sessions using Docker.
- Added API endpoint to run commands in a sandboxed container.

[Unreleased]: https://github.com/srtab/daiv-sandbox/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/srtab/daiv-sandbox/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/srtab/daiv-sandbox/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/srtab/daiv-sandbox/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.10...v0.1.0
[0.1.0-rc.10]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.9...v0.1.0-rc.10
[0.1.0-rc.9]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.8...v0.1.0-rc.9
[0.1.0-rc.8]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.7...v0.1.0-rc.8
[0.1.0-rc.7]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.6...v0.1.0-rc.7
[0.1.0-rc.6]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.5...v0.1.0-rc.6
[0.1.0-rc.5]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.4...v0.1.0-rc.5
[0.1.0-rc.4]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.3...v0.1.0-rc.4
[0.1.0-rc.3]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.2...v0.1.0-rc.3
[0.1.0-rc.2]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.1...v0.1.0-rc.2
[0.1.0-rc.1]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-alpha.2...v0.1.0-rc.1
[0.1.0-alpha.2]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-alpha...v0.1.0-alpha.2
[0.1.0-alpha]: https://github.com/srtab/daiv-sandbox/releases/tag/v0.1.0-alpha
