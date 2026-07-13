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

| Environment Variable                          | Description                                                                                                                                                                                                                                                                                                                                                | Options/Default                                                             |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| **DAIV_SANDBOX_API_KEY**                      | The API key required to access the sandbox API.                                                                                                                                                                                                                                                                                                            |                                                                             |
| **DAIV_SANDBOX_API_V1_STR**                   | Base path for API routes.                                                                                                                                                                                                                                                                                                                                  | Default: "/api/v1"                                                          |
| **DAIV_SANDBOX_ENVIRONMENT**                  | The deployment environment.                                                                                                                                                                                                                                                                                                                                | Options: `local`, `production`<br>Default: "production"                     |
| **DAIV_SANDBOX_SENTRY_DSN**                   | The DSN for Sentry error tracking.                                                                                                                                                                                                                                                                                                                         | Optional                                                                    |
| **DAIV_SANDBOX_SENTRY_ENABLE_LOGS**           | Whether to enable Sentry log forwarding.                                                                                                                                                                                                                                                                                                                   | Default: False                                                              |
| **DAIV_SANDBOX_SENTRY_TRACES_SAMPLE_RATE**    | Sentry traces sampling rate.                                                                                                                                                                                                                                                                                                                               | Default: 0.0                                                                |
| **DAIV_SANDBOX_SENTRY_PROFILES_SAMPLE_RATE**  | Sentry profiles sampling rate.                                                                                                                                                                                                                                                                                                                             | Default: 0.0                                                                |
| **DAIV_SANDBOX_SENTRY_SEND_DEFAULT_PII**      | Send default PII to Sentry.                                                                                                                                                                                                                                                                                                                                | Default: False                                                              |
| **DAIV_SANDBOX_RUNTIME**                      | The container runtime to use.                                                                                                                                                                                                                                                                                                                              | Options: `runc`, `runsc`<br>Default: "runc"                                 |
| **DAIV_SANDBOX_RUN_UID**                      | UID for sandbox command execution.                                                                                                                                                                                                                                                                                                                         | Default: 1000                                                               |
| **DAIV_SANDBOX_RUN_GID**                      | GID for sandbox command execution.                                                                                                                                                                                                                                                                                                                         | Default: 1000                                                               |
| **DAIV_SANDBOX_NETWORK**                      | Upstream Docker network the egress sidecar's NIC joins for outbound connectivity; falls back to Docker's default bridge when unset. The sandbox is never attached to it directly — a session that carries an `egress` block reaches the internet only through the proxy. A session created without an `egress` block stays isolated (`network_mode=none`). | Optional                                                                    |
| **DAIV_SANDBOX_EGRESS_PROXY_IMAGE**           | Docker image for the mitmproxy sidecar.                                                                                                                                                                                                                                                                                                                    | Default: `ghcr.io/srtab/daiv-sandbox-egress:latest`                         |
| **DAIV_SANDBOX_EGRESS_PROXY_PORT**            | Port the sidecar proxy listens on.                                                                                                                                                                                                                                                                                                                         | Default: 8080                                                               |
| **DAIV_SANDBOX_EGRESS_PROXY_RUNTIME**         | Container runtime for the sidecar. The sidecar runs trusted code and stays on `runc` by default even when sandboxes use `runsc`.                                                                                                                                                                                                                           | Options: `runc`, `runsc`<br>Default: "runc"                                 |
| **DAIV_SANDBOX_EGRESS_PROXY_NETWORK**         | Egress-side Docker network the sidecar's upstream NIC joins. Falls back to `DAIV_SANDBOX_NETWORK`, then Docker's default bridge.                                                                                                                                                                                                                           | Optional                                                                    |
| **DAIV_SANDBOX_EGRESS_PROXY_MEMORY_BYTES**    | Memory limit for the sidecar container (bytes).                                                                                                                                                                                                                                                                                                            | Optional                                                                    |
| **DAIV_SANDBOX_EGRESS_PROXY_CPUS**            | CPU quota for the sidecar container.                                                                                                                                                                                                                                                                                                                       | Optional                                                                    |
| **DAIV_SANDBOX_EGRESS_CA_CERT_FILE**          | Path to the shared egress CA certificate. Installed into every egress sandbox automatically. Required to enable network egress; set both or neither.                                                                                                                                                                                                       | Required to enable network egress (set both, or neither)                    |
| **DAIV_SANDBOX_EGRESS_CA_KEY_FILE**           | Path to the shared egress CA private key. Provided only to sidecars, never to the sandbox. Required to enable network egress; set both or neither.                                                                                                                                                                                                         | Required to enable network egress (set both, or neither)                    |
| **DAIV_SANDBOX_FS_PRUNE_DIRS**                | Comma-separated directory basenames/globs pruned by default from `fs/glob`/`fs/grep` (caches/metadata/build output). Excludes dependency-source dirs so agents can read deps. Setting this replaces the baseline entirely.                                                                                                                                 | Default: `.git,__pycache__,…`                                               |
| **DAIV_SANDBOX_COMMAND_TIMEOUT**              | Default per-command timeout in seconds. `0` disables the default. Overridable per request via `timeout`.                                                                                                                                                                                                                                                   | Default: 0                                                                  |
| **DAIV_SANDBOX_REDIS_URL**                    | Redis URL used for cross-replica per-session locking. When unset, an in-process lock is used.                                                                                                                                                                                                                                                              | Optional                                                                    |
| **DAIV_SANDBOX_SESSION_LOCK_TTL_SECONDS**     | TTL (seconds) of the per-session lock when Redis-backed.                                                                                                                                                                                                                                                                                                   | Default: 900                                                                |
| **DAIV_SANDBOX_SESSION_LOCK_WAIT_SECONDS**    | Max time (seconds) a request waits to acquire a busy session lock before returning `409`. Should comfortably outlast a typical op so a client's concurrently-dispatched ops queue instead of failing, while staying under the client's request timeout.                                                                                                    | Default: 30.0                                                               |
| **DAIV_SANDBOX_SESSION_LOCK_REFRESH_SECONDS** | Interval (seconds) at which a held session lock is refreshed.                                                                                                                                                                                                                                                                                              | Default: 30.0                                                               |
| **DAIV_SANDBOX_REAPER_ENABLED**               | Enable the background reaper that removes stopped session containers.                                                                                                                                                                                                                                                                                      | Default: true                                                               |
| **DAIV_SANDBOX_REAPER_INTERVAL_SECONDS**      | Reaper sweep cadence in seconds.                                                                                                                                                                                                                                                                                                                           | Default: 600                                                                |
| **DAIV_SANDBOX_SESSION_GRACE_SECONDS**        | Age (since stop) after which a stopped session container is removed.                                                                                                                                                                                                                                                                                       | Default: 43200 (12h)                                                        |
| **DAIV_SANDBOX_MAX_STOPPED_SESSIONS**         | LRU cap on retained stopped session containers.                                                                                                                                                                                                                                                                                                            | Default: 50                                                                 |
| **DAIV_SANDBOX_STOP_TIMEOUT_SECONDS**         | `docker stop` grace before SIGKILL when stopping a session.                                                                                                                                                                                                                                                                                                | Default: 2                                                                  |
| **DAIV_SANDBOX_HOST**                         | The host to bind the service to.                                                                                                                                                                                                                                                                                                                           | Default: "0.0.0.0"                                                          |
| **DAIV_SANDBOX_PORT**                         | The port to bind the service to.                                                                                                                                                                                                                                                                                                                           | Default: 8000                                                               |
| **DAIV_SANDBOX_LOG_LEVEL**                    | The log level to use.                                                                                                                                                                                                                                                                                                                                      | Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`<br>Default: "INFO" |

## Usage

`daiv-sandbox` provides a REST API to interact with. Here is a quick overview of the available endpoints:

- `POST /session/`: Start a sandbox session.
- `POST /session/{session_id}/seed/`: Seed the initial `/workspace/repo` and/or `/workspace/skills` state from tar archives (one-shot per session).
- `POST /session/{session_id}/fs/{op}`: Python-free file operations across `/workspace` (`ls`, `read`, `grep`, `glob`, `write`, `edit`, `delete`).
- `POST /session/{session_id}/`: Run commands on the sandbox session.
- `GET /session/{session_id}/`: Check session status — `204` if it exists (restarting it if stopped), else `404`.
- `DELETE /session/{session_id}/`: Close the sandbox session (stops the container by default; `?force=true` removes it).
- `PUT /session/{session_id}/egress/`: Refresh an egress session's policy and secrets without recreating the container (`204` success / `404` no session / `409` session has no egress proxy / `422` invalid policy body / `503` proxy sidecar not running, retryable).
- `GET /-/health/`: Healthcheck endpoint.
- `GET /-/version/`: Current application version.

### Starting a Sandbox Session

To start a sandbox session, you need to call the `POST /session/` endpoint. A Docker image is pulled from the registry and used to create the sandbox container.

After the image is pulled, the container is created and started.

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

| Parameter      | Description                                                                                                                                                              | Required | Valid Values                                                             |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------- | ------------------------------------------------------------------------ |
| `base_image`   | The base image to use for the container.                                                                                                                                 | Yes      | Any valid Docker image                                                   |
| `egress`       | Egress policy block. When present, the session is routed through the per-session egress proxy with the given policy; omit for an isolated sandbox (`network_mode=none`). | No       | Egress policy object (see [Network Egress Proxy](#network-egress-proxy)) |
| `environment`  | Environment variables to set at container start.                                                                                                                         | No       | Object of string pairs                                                   |
| `memory_bytes` | Memory limit for the container (bytes).                                                                                                                                  | No       | Integer (bytes)                                                          |
| `cpus`         | CPU quota for the container.                                                                                                                                             | No       | Float (e.g. `0.5`, `1.0`)                                                |

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

A freshly-started session has empty `/workspace/repo` and `/workspace/skills` directories. Before running commands or operating on files, seed the workspace by calling `POST /session/{session_id}/seed/` with one or both archives as `multipart/form-data` fields. `repo_archive` is extracted into `/workspace/repo` (e.g. a repository snapshot) and `skills_archive` is extracted into `/workspace/skills` (auxiliary tooling, prompts, etc.).

Both fields are optional individually, but **at least one must be provided** — a request with neither returns `422`. Seeding is **one-shot per session** — a second call returns `409 Conflict`.

Archives may be plain `tar` or compressed with gzip, bzip2, xz, or zstd; the compression is auto-detected. They are sanitised and streamed into the container without being fully buffered in memory.

| Parameter        | Description                                                        | Required          | Valid Values                     |
| ---------------- | ------------------------------------------------------------------ | ----------------- | -------------------------------- |
| `repo_archive`   | Tar archive that becomes the initial state of `/workspace/repo`.   | At least one of   | `tar` (optionally gz/bz2/xz/zst) |
| `skills_archive` | Tar archive that becomes the initial state of `/workspace/skills`. | `repo_archive` or | `tar` (optionally gz/bz2/xz/zst) |
|                  |                                                                    | `skills_archive`  |                                  |

```sh
$ curl -X POST \
  -H "X-API-Key: notsosecret" \
  -F "repo_archive=@django-webhooks-master.tar.gz" \
  -F "skills_archive=@skills.tar.gz" \
  http://localhost:8000/api/v1/session/550e8400-e29b-41d4-a716-446655440000/seed/
```

The response is an empty body with status `204`.

### Workspace File Operations

To inspect or modify files without spawning a shell, use the `POST /session/{session_id}/fs/{op}` endpoints. They operate anywhere under `/workspace` (`repo/`, `skills/`, `tmp/`) and are Python-free — content moves via the Docker archive API and search/listing uses POSIX `grep`/`find`/`ls`/`rm`, so they work even on base images without a Python interpreter (e.g. `alpine`).

| Op       | Body                               | Returns                                |
| -------- | ---------------------------------- | -------------------------------------- |
| `ls`     | `{path}`                           | directory entries + `error?`           |
| `read`   | `{path, offset?, limit?}`          | utf-8 text or base64 binary + `error?` |
| `grep`   | `{pattern, path, glob?, exclude?}` | literal-substring matches + `error?`   |
| `glob`   | `{pattern, path, exclude?}`        | paths matching the glob + `error?`     |
| `write`  | `{path, content, mode?}`           | `{ok, error?}`                         |
| `edit`   | `{path, old, new, replace_all?}`   | `{occurrences, error?}`                |
| `delete` | `{path}`                           | `{ok, removed, error?}`                |

`path` must be absolute and under `/workspace`; the file ops (`write`/`edit`/`read`/`delete`) target a file, while the directory ops (`ls`/`grep`/`glob`) may also target the `/workspace` root itself. `content` is base64-encoded.

`fs/grep` and `fs/glob` prune common cache/build directories by default (configurable via
`DAIV_SANDBOX_FS_PRUNE_DIRS`) so results aren't swamped by `.git`, `__pycache__`, `.ruff_cache`,
`target`, `obj`, and similar tooling artifacts. Dependency _source_ directories (`node_modules`,
`.venv`, `vendor`, `packages`) are **not** pruned by default so an agent can still read dependency
implementations; pass `exclude` (e.g. `["node_modules"]`) to prune them per request. Prune matching
is by directory basename at any depth, so a glob pattern that descends into a pruned directory
returns nothing.

Every response carries an `error` field with the shape `{code, message}` when the operation fails — `null` on success. `code` is one of the stable `FsErrorCode` values:

| Code                   | Meaning                                                              |
| ---------------------- | -------------------------------------------------------------------- |
| `invalid_path`         | Path is malformed (`..`, NUL, newline) or outside `/workspace`.      |
| `not_found`            | Path does not exist (distinct from an empty directory / no-match).   |
| `not_a_directory`      | `ls` or `glob` was called on a file path.                            |
| `is_a_directory`       | `read`, `edit`, or `delete` was called on a directory path.          |
| `not_a_text_file`      | `edit` target is not valid UTF-8 text.                               |
| `string_not_found`     | `edit` found no occurrence of `old`.                                 |
| `multiple_occurrences` | `edit` found more than one occurrence and `replace_all` was not set. |
| `already_exists`       | `write` target already exists (create-only).                         |
| `too_large`            | Content exceeds the 512 KB read cap.                                 |
| `invalid_offset`       | `read` `offset` is beyond the file length.                           |
| `permission_denied`    | Filesystem permission error inside the container.                    |
| `exec_failed`          | The underlying shell command failed unexpectedly.                    |

`fs/delete` additionally returns a `removed` boolean: `true` if the file was deleted, `false` if it was already absent.

`fs/ls`, `fs/grep`, and `fs/glob` return HTTP `200` with `error.code=invalid_path` for malformed paths (no longer `400`), keeping all fs ops consistent: HTTP status codes are reserved for session/transport concerns (404 missing session, 409 lock, 503 infra, 403 auth).

`read` has a few additional behaviors: an empty file returns a human-readable sentinel string (with `encoding: "utf-8"`); an `offset` beyond the file length returns an `error`; a single response is capped at 512000 bytes — a larger text page is truncated with a marker (continue with a larger `offset`/smaller `limit`), and a binary file over the cap returns an `error` rather than a base64 blob.

The sandbox is the single source of truth: edits under `/workspace/repo` (via bash or `fs/*`) land directly on the container's workspace, while `skills/` and `tmp/` stay container-local.

> **Path confinement is lexical.** The `path` validator rejects `..` traversal, NUL, and newlines and requires the path to be under `/workspace`, but it does not resolve symlinks. Code executed in the container (via `POST /session/{id}/`) can create a symlink under `/workspace` pointing elsewhere, which `fs/read` would then follow. This grants no capability beyond running commands in the container — the **container** (gVisor / non-root) is the trust boundary, not the `/workspace` prefix.

```sh
$ curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-Key: notsosecret" \
  -d '{"path": "/workspace/repo/hello.txt", "content": "aGVsbG8=", "mode": 420}' \
  http://localhost:8000/api/v1/session/550e8400-e29b-41d4-a716-446655440000/fs/write
```

```json
{ "ok": true, "error": null }
```

### Running Commands

To run commands on a sandbox session, call `POST /session/{session_id}/` using the session ID returned when starting the session. Commands execute against the container's persistent `/workspace/repo` workspace, which carries state across calls in the same session (seed → file ops → commands → more commands…).

By default, all commands are executed sequentially regardless of their exit codes. Set `fail_fast` to `true` to stop on the first non-zero exit code. Pipelines run with `pipefail`, so a failing stage in `cmd1 | cmd2` correctly propagates a non-zero exit code.

Set `timeout` (seconds) to cap each command's wall-clock time. A command that exceeds the timeout is terminated with exit code `124` and any remaining commands in the request are skipped. Omitting `timeout` falls back to the server default (`DAIV_SANDBOX_COMMAND_TIMEOUT`, `0` = no timeout).

Commands mutate the container workspace in place. To recover the changes made during a session, run git (or any diff tool) inside `/workspace/repo` — e.g. `git diff` / `git status` — through this same endpoint; the workspace is the single source of truth.

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
  ]
}
```

Here is an example using `python` that seeds a session and then runs commands:

```python
import io
import tarfile

import httpx

API = "http://localhost:8000/api/v1"
HEADERS = {"X-API-Key": "notsosecret"}

# 1. Start a session.
session_id = (
    httpx
    .post(f"{API}/session/", headers=HEADERS, json={"base_image": "python:3.12"})
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

# 3. Run commands. Changes mutate the container workspace in place.
resp = (
    httpx
    .post(
        f"{API}/session/{session_id}/", headers=HEADERS, json={"commands": ["ls -la"], "fail_fast": True, "timeout": 60}
    )
    .raise_for_status()
    .json()
)

print(resp["results"][0]["output"])
```

> [!NOTE]
> When `DAIV_SANDBOX_REDIS_URL` is set, requests against the same session are serialised across replicas. A request that cannot acquire the per-session lock within `DAIV_SANDBOX_SESSION_LOCK_WAIT_SECONDS` returns `409 Conflict`.

### Closing a Sandbox Session

To close a sandbox session, you need to call the `DELETE /session/{session_id}/` endpoint using the session ID returned when starting the session.

`DELETE /session/{id}/` stops the container (kept warm for reuse); pass `?force=true` to remove it immediately. A background reaper removes stopped containers after `DAIV_SANDBOX_SESSION_GRACE_SECONDS` (default 12h) or once the `DAIV_SANDBOX_MAX_STOPPED_SESSIONS` LRU cap is exceeded.

```sh
$ curl -X DELETE \
  -H "X-API-Key: notsosecret" \
  http://localhost:8000/api/v1/session/550e8400-e29b-41d4-a716-446655440000/
```

The response will be an empty body with a status code of 204.

> [!TIP]
> Why closing a sandbox session is important? By default it stops the container so the next turn can reuse it warm; the background reaper later removes it to free resources. Pass `?force=true` to remove it right away.

### Network Egress Proxy

Sessions that carry an `egress` block on `POST /session/` are built as a **triad**. Egress is enabled by configuring the shared CA (`DAIV_SANDBOX_EGRESS_CA_CERT_FILE` + `DAIV_SANDBOX_EGRESS_CA_KEY_FILE`); without it, a `POST /session/` carrying an `egress` block is rejected with `400`. There is no direct-network attach that bypasses the proxy.

1. An `internal` Docker network with no gateway (the sandbox can only talk to the sidecar).
2. A `mitmproxy` sidecar dual-homed on the internal network and an egress-side network — it is the sole gateway to the internet.
3. The sandbox attached only to the internal network, with its `HTTP_PROXY`/`HTTPS_PROXY` environment variables pointing at the sidecar.

Credentials (API tokens, bearer headers) live in the sidecar and **never enter the sandbox container**.

#### Configuring egress at session create time

Pass an `egress` block in the `POST /session/` body to route the session through the per-session egress proxy. Example:

```json
{
  "base_image": "python:3.12",
  "egress": {
    "policy": {
      "default": "deny",
      "intercept": "credentialed",
      "rules": [
        {
          "host": "*.github.com",
          "methods": ["GET", "POST"],
          "inject": "github-token"
        },
        {
          "host": "pypi.org",
          "methods": ["*"]
        }
      ]
    },
    "secrets": {
      "github-token": { "header": "Authorization", "value": "Bearer ghp_…" }
    }
  }
}
```

- **`policy.default`** — `"deny"` (allowlist; only hosts with a matching rule are reachable) or `"allow"` (denylist; all hosts are reachable unless blocked).
- **`policy.intercept`** — `"all"` (MITM every connection) or `"credentialed"` (MITM only hosts that inject credentials or carry a `methods` restriction; tunnel the rest untouched).
- **`policy.rules`** — list of per-host rules. `host` is a glob (e.g. `*.github.com`), matched case-insensitively. `methods` restricts which HTTP methods are permitted (`["*"]` allows all). `inject` names a secret (from `secrets`) whose configured header is set on requests to this host.
- **`secrets`** — named `{ "header", "value" }` pairs injected by the sidecar. Keys are referenced from `inject` in rules; the header name is arbitrary (e.g. `Authorization`, `PRIVATE-TOKEN`), and values are redacted in logs and never forwarded to the sandbox.

> [!NOTE]
> A rule with `methods` set to anything other than `["*"]` causes that host to be intercepted (MITM'd) regardless of the `intercept` mode, so the method can be enforced after TLS termination. Such hosts require the shared CA, which is installed into every egress sandbox automatically.

#### Refreshing egress policy on a live session

`PUT /session/{id}/egress/` accepts the same body as the `egress` field on `POST /session/` and rewrites the sidecar's `config.json` atomically; the proxy hot-reloads it on the next request — no container restart needed. Returns `204` on success, `404` if the session is gone, or `409` if the session has no egress proxy (a network-isolated session has nothing to refresh). Use this to rotate credentials (e.g. a short-lived git token) without rebuilding the session.

#### CA generation (operator one-time step)

TLS interception requires a shared CA. Generate it once and mount the files into the service:

```sh
# One-time: generate the shared egress CA (operator step). Mount the files and point the settings at them.
openssl req -x509 -newkey rsa:4096 -nodes -keyout egress-ca.key -out egress-ca.crt -days 3650 -subj "/CN=daiv-sandbox-egress"
```

Then set:

- `DAIV_SANDBOX_EGRESS_CA_CERT_FILE` — path to `egress-ca.crt` (installed into every egress sandbox).
- `DAIV_SANDBOX_EGRESS_CA_KEY_FILE` — path to `egress-ca.key` (provided only to sidecars).

#### Limitations

- **Cert-pinned clients** will fail — the proxy terminates and re-signs TLS, so any client that pins the upstream certificate will reject it.
- **SSH-based git** and other non-HTTP(S) protocols are not proxied and are blocked by the internal network isolation.
- **Non-HTTP egress** (raw TCP, UDP, etc.) is out of scope.

## Contributing

We welcome contributions! Whether you want to fix a bug, add a new feature, or improve documentation, please refer to the [CONTRIBUTING.md](CONTRIBUTING.md) file for more information.

## License

This project is licensed under the [Apache 2.0 License](LICENSE).

## Support & Community

For questions or support, please open an issue in the GitHub repository. Contributions, suggestions, and feedback are greatly appreciated!

**Happy Coding!**
