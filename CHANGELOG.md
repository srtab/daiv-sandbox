# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added `__version__` to the project.
- Added `health` endpoint to check if the service is healthy.
- Added `version` endpoint to get the version of the service.
- Added API Key authentication to command run endpoint.
- Added more metadata to the OpenAPI schema.
- Added support to pass a `workdir` to the command run endpoint.

### Changed

- Changed `Pydantic` models to specific `schemas.py` file.

### Removed

- Removed `--workers` from the `CMD` in the `Dockerfile` to allow scaling using docker replicas.
- Removed `PROJECT_NAME` from the configuration.

### Fixed

- Fixed issue on command quoting using `shlex.quote`, which was causing double quoting of the command.

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
