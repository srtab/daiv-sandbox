# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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

[Unreleased]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.4...HEAD
[0.1.0-rc.4]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.3...v0.1.0-rc.4
[0.1.0-rc.3]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.2...v0.1.0-rc.3
[0.1.0-rc.2]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-rc.1...v0.1.0-rc.2
[0.1.0-rc.1]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-alpha.2...v0.1.0-rc.1
[0.1.0-alpha.2]: https://github.com/srtab/daiv-sandbox/compare/v0.1.0-alpha...v0.1.0-alpha.2
[0.1.0-alpha]: https://github.com/srtab/daiv-sandbox/releases/tag/v0.1.0-alpha
