# DAIV Sandbox

![Python Version](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsrtab%2Fdaiv-sandbox%2Fmain%2Fpyproject.toml)
[![GitHub License](https://img.shields.io/github/license/srtab/daiv-sandbox)](https://github.com/srtab/daiv-sandbox/blob/main/LICENSE)
[![Actions Status](https://github.com/srtab/daiv-sandbox/actions/workflows/ci.yml/badge.svg)](https://github.com/srtab/daiv-sandbox/actions)

## What is `daiv-sandbox`?

`daiv-sandbox` is a FastAPI application designed to securely execute arbitrary commands within a controlled environment. Each execution is isolated in a transient Docker container ensuring a clean and secure execution space. It is designed to be used as a bash commands executor for [DAIV](https://github.com/srtab/daiv).

This is very useful to increase the capabilities of an AI agent with bash commands execution capabilities. For instance, you can use it to apply formatting changes to a repository codebase like running `black`, `isort`, `ruff`, `prettier`, or update dependencies, lock files, etc...

To enhance security, `daiv-sandbox` leverages [`gVisor`](https://github.com/google/gvisor) as its container runtime. This provides an additional layer of protection by restricting the running code's ability to interact with the host system, thereby minimizing the risk of sandbox escape.

While `gVisor` significantly improves security, it may introduce some performance overhead due to its additional isolation mechanisms. This trade-off is generally acceptable for applications prioritizing security over raw execution speed.

## Getting Started

`daiv-sandbox` is available as a Docker image on [GitHub Container Registry](ghcr.io/srtab/daiv-sandbox).

You can run the container using the following command:

```sh
$ docker run --rm -d -p 8000:8000 -e DAIV_SANDBOX_API_KEY=my-secret-api-key ghcr.io/srtab/daiv-sandbox:latest
```

You can also configure the container using environment variables. See the [Configuration](#configuration) section for more details.

For usage examples, see the [Usage](#usage) section.

### Security (Optional)

To enhance security, `daiv-sandbox` can be used with `gVisor` as its container runtime. This provides an additional layer of protection by restricting the running code's ability to interact with the host system, thereby minimizing the risk of sandbox escape.

To benefit from this additional layer of protection, you need to install `gVisor` on the host machine. Follow the instructions [here](https://gvisor.dev/docs/user_guide/install/) and [here](https://gvisor.dev/docs/user_guide/quick_start/docker/). You will need to configure `gVisor` with a shared root filesystem to allow `daiv-sandbox` copy files to the container, see the [gVisor documentation](https://gvisor.dev/docs/user_guide/filesystem/#shared-root-filesystem) for more details.

> [!TIP]
> If you're getting an error `"overlay flag is incompatible with shared file access for rootfs"`, you need to add `--overlay2=none` flag to the `gVisor` configuration.

After installing `gVisor`, you need to define the `DAIV_SANDBOX_RUNTIME` environment variable to use `runsc` when running the container:

```sh
$ docker run --rm -d -p 8000:8000 -e DAIV_SANDBOX_API_KEY=my-secret-api-key -e DAIV_SANDBOX_RUNTIME=runsc ghcr.io/srtab/daiv-sandbox:latest
```

### Configuration

All settings are configurable via environment variables. The available settings are:

| Environment Variable                          | Description                                                                                              | Options/Default                                                             |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| **DAIV_SANDBOX_API_KEY**                      | The API key required to access the sandbox API.                                                          |                                                                             |
| **DAIV_SANDBOX_API_V1_STR**                   | Base path for API routes.                                                                                | Default: "/api/v1"                                                          |
| **DAIV_SANDBOX_ENVIRONMENT**                  | The deployment environment.                                                                              | Options: `local`, `production`<br>Default: "production"                     |
| **DAIV_SANDBOX_SENTRY_DSN**                   | The DSN for Sentry error tracking.                                                                       | Optional                                                                    |
| **DAIV_SANDBOX_SENTRY_ENABLE_LOGS**           | Whether to enable Sentry log forwarding.                                                                 | Default: False                                                              |
| **DAIV_SANDBOX_SENTRY_TRACES_SAMPLE_RATE**    | Sentry traces sampling rate.                                                                             | Default: 0.0                                                                |
| **DAIV_SANDBOX_SENTRY_PROFILES_SAMPLE_RATE**  | Sentry profiles sampling rate.                                                                           | Default: 0.0                                                                |
| **DAIV_SANDBOX_SENTRY_SEND_DEFAULT_PII**      | Send default PII to Sentry.                                                                              | Default: False                                                              |
| **DAIV_SANDBOX_RUNTIME**                      | The container runtime to use.                                                                            | Options: `runc`, `runsc`<br>Default: "runc"                                 |
| **DAIV_SANDBOX_RUN_UID**                      | UID for sandbox command execution.                                                                       | Default: 1000                                                               |
| **DAIV_SANDBOX_RUN_GID**                      | GID for sandbox command execution.                                                                       | Default: 1000                                                               |
| **DAIV_SANDBOX_COMMAND_TIMEOUT**              | Default per-command timeout in seconds. `0` disables the default. Overridable per request via `timeout`. | Default: 0                                                                  |
| **DAIV_SANDBOX_REDIS_URL**                    | Redis URL used for cross-replica per-session locking. When unset, an in-process lock is used.            | Optional                                                                    |
| **DAIV_SANDBOX_SESSION_LOCK_TTL_SECONDS**     | TTL (seconds) of the per-session lock when Redis-backed.                                                 | Default: 900                                                                |
| **DAIV_SANDBOX_SESSION_LOCK_WAIT_SECONDS**    | Max time (seconds) a request waits to acquire a busy session lock before returning `409`.                | Default: 1.0                                                                |
| **DAIV_SANDBOX_SESSION_LOCK_REFRESH_SECONDS** | Interval (seconds) at which a held session lock is refreshed.                                            | Default: 30.0                                                               |
| **DAIV_SANDBOX_GIT_IMAGE**                    | Image used to extract patches.                                                                           | Default: "alpine/git:2.52.0"                                                |
| **DAIV_SANDBOX_HOST**                         | The host to bind the service to.                                                                         | Default: "0.0.0.0"                                                          |
| **DAIV_SANDBOX_PORT**                         | The port to bind the service to.                                                                         | Default: 8000                                                               |
| **DAIV_SANDBOX_LOG_LEVEL**                    | The log level to use.                                                                                    | Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`<br>Default: "INFO" |

## Usage

`daiv-sandbox` provides a REST API to interact with. Here is a quick overview of the available endpoints:

- `POST /session/`: Start a sandbox session.
- `POST /session/{session_id}/seed/`: Seed the initial `/workspace/repo` and/or `/workspace/skills` state from tar archives (one-shot per session).
- `POST /session/{session_id}/files/`: Apply a batch of file mutations to `/workspace/repo`.
- `POST /session/{session_id}/`: Run commands on the sandbox session.
- `DELETE /session/{session_id}/`: Close the sandbox session.
- `GET /-/health/`: Healthcheck endpoint.
- `GET /-/version/`: Current application version.

### Starting a Sandbox Session

To start a sandbox session, you need to call the `POST /session/` endpoint. A Docker image is pulled from the registry and used to create the sandbox container.

After image is pulled or built, the container will be created and started, along with an helper container that will be used to extract the patch of the changed files.

Here is an example using `curl`:

```sh
$ curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: notsosecret" \
  -d "{\"base_image\": \"python:3.12\"}" \
  http://localhost:8000/api/v1/session/
```

The response will be a JSON object with the following structure containing the session ID:

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

The following table describes the parameters for the `session/` endpoint:

| Parameter         | Description                                                     | Required | Valid Values                         |
| ----------------- | --------------------------------------------------------------- | -------- | ------------------------------------ |
| `base_image`      | The base image to use for the container.                        | Yes      | Any valid Docker image               |
| `extract_patch`   | Extract a patch with the changes made by the executed commands. | No       | `true` or `false` (default: `false`) |
| `network_enabled` | Enable network access inside the container.                     | No       | `true` or `false` (default: `false`) |
| `environment`     | Environment variables to set at container start.                | No       | Object of string pairs               |
| `memory_bytes`    | Memory limit for the container (bytes).                         | No       | Integer (bytes)                      |
| `cpus`            | CPU quota for the container.                                    | No       | Float (e.g. `0.5`, `1.0`)            |

> [!NOTE]
> For security reasons, building images from arbitrary Dockerfiles is not supported by this service. Provide a `base_image`.

> [!WARNING]
> The `base_image` need to be a distro image. Distroless images will not work as there is no shell available in the container to maintain the image running indefinitely.

Here is an example using `python`:

```python
import httpx

response = httpx.post(
    "http://localhost:8000/api/v1/session/", headers={"X-API-Key": "notsosecret"}, json={"base_image": "python:3.12"}
)

response.raise_for_status()
resp = response.json()

print(resp["session_id"])
```

### Seeding a Session

A freshly-started session has empty `/workspace/repo` and `/workspace/skills` directories. Before running commands or applying file mutations, seed the workspace by calling `POST /session/{session_id}/seed/` with one or both archives as `multipart/form-data` fields. `repo_archive` is extracted into `/workspace/repo` (e.g. a repository snapshot) and `skills_archive` is extracted into `/workspace/skills` (auxiliary tooling, prompts, etc.). When `extract_patch` was enabled at session start and `repo_archive` is provided, the patch-extractor's meta repo is initialised against this state.

Both fields are optional individually, but **at least one must be provided** — a request with neither returns `422`. Seeding is **one-shot per session** — a second call returns `409 Conflict`.

Archives may be plain `tar` or gzip-compressed (`tar.gz`); they are sanitised and streamed into the container without being fully buffered in memory.

| Parameter        | Description                                                        | Required          | Valid Values               |
| ---------------- | ------------------------------------------------------------------ | ----------------- | -------------------------- |
| `repo_archive`   | Tar archive that becomes the initial state of `/workspace/repo`.   | At least one of   | `tar` or `tar.gz` (binary) |
| `skills_archive` | Tar archive that becomes the initial state of `/workspace/skills`. | `repo_archive` or | `tar` or `tar.gz` (binary) |
|                  |                                                                    | `skills_archive`  |                            |

```sh
$ curl -X POST \
  -H "X-API-Key: notsosecret" \
  -F "repo_archive=@django-webhooks-master.tar.gz" \
  -F "skills_archive=@skills.tar.gz" \
  http://localhost:8000/api/v1/session/550e8400-e29b-41d4-a716-446655440000/seed/
```

The response is an empty body with status `204`.

### Applying File Mutations

To write or overwrite files in `/workspace/repo` without spawning a shell, use `POST /session/{session_id}/files/`. Each mutation specifies an absolute path under `/workspace/repo`, base64-encoded content, and POSIX mode bits. When `extract_patch` was enabled at session start, the meta-repo HEAD is advanced after a successful batch so the next `POST /session/{session_id}/` returns a patch that includes these changes.

Per-item errors (e.g. invalid path) are returned in `results[]` with `ok=false`; request-level errors (auth, schema, unknown session) return a 4xx status.

| Parameter   | Description                              | Required | Valid Values                     |
| ----------- | ---------------------------------------- | -------- | -------------------------------- |
| `mutations` | Batch of file mutations to apply (1–64). | Yes      | Array of `{path, content, mode}` |

`mutations[].path` must be absolute and under `/workspace/repo`; `mutations[].content` is base64-encoded full file content; `mutations[].mode` is an integer in the POSIX mode range (e.g. `0o644`).

```sh
$ curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: notsosecret" \
  -d '{"mutations": [{"path": "/workspace/repo/hello.txt", "content": "aGVsbG8=", "mode": 420}]}' \
  http://localhost:8000/api/v1/session/550e8400-e29b-41d4-a716-446655440000/files/
```

```json
{
  "results": [
    { "path": "/workspace/repo/hello.txt", "ok": true, "error": null }
  ]
}
```

### Running Commands

To run commands on a sandbox session, call `POST /session/{session_id}/` using the session ID returned when starting the session. Commands execute against the container's persistent `/workspace/repo` workspace, which carries state across calls in the same session (seed → mutations → commands → more commands…).

By default, all commands are executed sequentially regardless of their exit codes. Set `fail_fast` to `true` to stop on the first non-zero exit code. Pipelines run with `pipefail`, so a failing stage in `cmd1 | cmd2` correctly propagates a non-zero exit code.

Set `timeout` (seconds) to cap each command's wall-clock time. A command that exceeds the timeout is terminated with exit code `124` and any remaining commands in the request are skipped. Omitting `timeout` falls back to the server default (`DAIV_SANDBOX_COMMAND_TIMEOUT`, `0` = no timeout).

When `extract_patch` was enabled on session creation, the `patch` field in the response is a base64-encoded unified diff covering the changes since the previous turn (`HEAD~1..HEAD` against the meta repo). It is `null` when no changes were detected.

| Parameter   | Description                                                                                 | Required | Valid Values                         |
| ----------- | ------------------------------------------------------------------------------------------- | -------- | ------------------------------------ |
| `commands`  | The commands to execute.                                                                    | Yes      | List of strings                      |
| `fail_fast` | Stop execution if any command fails.                                                        | No       | `true` or `false` (default: `false`) |
| `timeout`   | Per-command timeout in seconds. Overrides `DAIV_SANDBOX_COMMAND_TIMEOUT`. `0` = no timeout. | No       | Integer ≥ 0                          |

Here is an example using `curl`:

```sh
$ curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: notsosecret" \
  -d '{"commands": ["ls -la"], "fail_fast": true, "timeout": 60}' \
  http://localhost:8000/api/v1/session/550e8400-e29b-41d4-a716-446655440000/
```

The response will be a JSON object with the following structure:

```json
{
  "results": [
    {
      "command": "ls -la",
      "output": "total 12\ndrwxr-xr-x 3 root root 4096 Nov 20 20:28 .\ndrwxrwxrwt 1 root root 4096 Nov 20 20:28 ..\ndrwxrwxr-x 3 root root 4096 Nov 14 14:39 django-webhooks-master\n",
      "exit_code": 0
    }
  ],
  "patch": null // Base64-encoded diff; null when no changes were detected
}
```

Here is an example using `python` that seeds a session and then runs commands:

```python
import base64
import io
import tarfile

import httpx

API = "http://localhost:8000/api/v1"
HEADERS = {"X-API-Key": "notsosecret"}

# 1. Start a session with extract_patch enabled.
session_id = (
    httpx
    .post(f"{API}/session/", headers=HEADERS, json={"base_image": "python:3.12", "extract_patch": True})
    .raise_for_status()
    .json()["session_id"]
)

# 2. Seed /workspace/repo (and optionally /workspace/skills) from tar archives via multipart upload.
tarstream = io.BytesIO()
with tarfile.open(fileobj=tarstream, mode="w:gz") as tar:
    # Add files to tar...
    pass
tarstream.seek(0)
httpx.post(
    f"{API}/session/{session_id}/seed/",
    headers=HEADERS,
    files={"repo_archive": ("repo.tar.gz", tarstream, "application/gzip")},
).raise_for_status()

# 3. Run commands. Returns a patch covering this turn's changes.
resp = (
    httpx
    .post(
        f"{API}/session/{session_id}/", headers=HEADERS, json={"commands": ["ls -la"], "fail_fast": True, "timeout": 60}
    )
    .raise_for_status()
    .json()
)

print(resp["results"][0]["output"])
if resp["patch"]:
    print(base64.b64decode(resp["patch"]).decode())
```

> [!NOTE]
> When `DAIV_SANDBOX_REDIS_URL` is set, requests against the same session are serialised across replicas. A request that cannot acquire the per-session lock within `DAIV_SANDBOX_SESSION_LOCK_WAIT_SECONDS` returns `409 Conflict`.

> [!TIP]
> To apply the patch to the original repository/directory, you can use the `git apply` command. Don't need to be a git repository, it can be any directory.
>
> ```sh
> $ git apply --whitespace=nowarn --reject < patch.diff
> ```

### Closing a Sandbox Session

To close a sandbox session, you need to call the `DELETE /session/{session_id}/` endpoint using the session ID returned when starting the session.

```sh
$ curl -X DELETE \
  -H "X-API-Key: notsosecret" \
  http://localhost:8000/api/v1/session/550e8400-e29b-41d4-a716-446655440000/
```

The response will be an empty body with a status code of 204.

> [!TIP]
> Why closing a sandbox session is important? Because it will remove the container from the host machine, freeing up resources.

## Contributing

We welcome contributions! Whether you want to fix a bug, add a new feature, or improve documentation, please refer to the [CONTRIBUTING.md](CONTRIBUTING.md) file for more information.

## License

This project is licensed under the [Apache 2.0 License](LICENSE).

## Support & Community

For questions or support, please open an issue in the GitHub repository. Contributions, suggestions, and feedback are greatly appreciated!

**Happy Coding!**
