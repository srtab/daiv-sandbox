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

| Environment Variable                | Description                                     | Options/Default                                                             |
| ----------------------------------- | ----------------------------------------------- | --------------------------------------------------------------------------- |
| **DAIV_SANDBOX_API_KEY**            | The API key required to access the sandbox API. |                                                                             |
| **DAIV_SANDBOX_ENVIRONMENT**        | The deployment environment.                     | Options: `local`, `production`<br>Default: "production"                     |
| **DAIV_SANDBOX_SENTRY_DSN**         | The DSN for Sentry error tracking.              | Optional                                                                    |
| **DAIV_SANDBOX_SENTRY_ENABLE_LOGS** | Whether to enable Sentry log forwarding.        | Default: False                                                              |
| **DAIV_SANDBOX_RUNTIME**            | The container runtime to use.                   | Options: `runc`, `runsc`<br>Default: "runc"                                 |
| **DAIV_SANDBOX_HOST**               | The host to bind the service to.                | Default: "0.0.0.0"                                                          |
| **DAIV_SANDBOX_PORT**               | The port to bind the service to.                | Default: 8000                                                               |
| **DAIV_SANDBOX_LOG_LEVEL**          | The log level to use.                           | Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`<br>Default: "INFO" |

## Usage

`daiv-sandbox` provides a REST API to interact with. Here is a quick overview of the available endpoints:

- `POST /session/`: Start a sandbox session.
- `POST /session/{session_id}/`: Run commands on the sandbox session.
- `DELETE /session/{session_id}/`: Close the sandbox session.

### Starting a Sandbox Session

To start a sandbox session, you need to call the `POST /session/` endpoint. A Docker image is pulled from the registry and used to create the sandbox container.

After image is pulled or built, the container will be created and started, along with an helper container that will be used to extract the patch of the changed files.

Here is an example using `curl`:

```sh
$ curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: notsosecret" \
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

| Parameter       | Description                                                     | Required | Valid Values                         |
| --------------- | --------------------------------------------------------------- | -------- | ------------------------------------ |
| `base_image`    | The base image to use for the container.                        | Yes      | Any valid Docker image               |
| `extract_patch` | Extract a patch with the changes made by the executed commands. | No       | `true` or `false` (default: `false`) |

> [!NOTE]
> For security reasons, building images from arbitrary Dockerfiles is not supported by this service. Provide a `base_image`.

> [!WARNING]
> The `base_image` need to be a distro image. Distroless images will not work as there is no shell available in the container to maintain the image running indefinitely.

Here is an example using `python`:

```python
import httpx

response = httpx.post(
    "http://localhost:8000/api/v1/session/",
    headers={"X-API-KEY": "notsosecret"},
    json={"base_image": "python:3.12"},
)

response.raise_for_status()
resp = response.json()

print(resp["session_id"])
```

### Running Commands

To run commands on a sandbox session, you need to call the `POST /session/{session_id}/` endpoint using the session ID returned when starting the session.

This endpoint can be used to execute arbitrary commands on a provided archive, for instance, a `tar.gz` archive containing a repository codebase. The archive is extracted to a temporary directory and the commands are executed in the root of the extracted directory. The output of each command is returned in the response along with a patch of the changed files if the `extract_patch` parameter is set to `true` in the request to start the session and there were changes made by the commands.

By default, all commands are executed sequentially regardless of their exit codes. However, you can enable fail-fast behavior by setting the `fail_fast` parameter to `true`, which will stop execution immediately if any command fails (returns a non-zero exit code).

The following table describes the parameters for the `POST /session/{session_id}/` endpoint:

| Parameter   | Description                                         | Required | Valid Values                         |
| ----------- | --------------------------------------------------- | -------- | ------------------------------------ |
| `archive`   | The archive to extract and execute the commands on. | Yes      | Base64 encoded `tar.gz` archive      |
| `commands`  | The commands to execute.                            | Yes      | List of strings                      |
| `fail_fast` | Stop execution if any command fails.                | No       | `true` or `false` (default: `false`) |
| `workdir`   | The working directory to use for the commands.      | No       | Any valid directory                  |

Here is an example using `curl`:

```sh
$ curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: notsosecret" \
  -d "{\"archive\": \"$(base64 -w 0 django-webhooks-master.tar.gz)\", \"commands\": [\"ls -la\"], \"fail_fast\": true}" \
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
  "patch": null // If no file changes were made, the patch is not returned
}
```

Here is an example using `python`:

```python
import httpx
import tarfile
import base64
import io

# Create tar archive first
tarstream = io.BytesIO()
with tarfile.open(fileobj=tarstream, mode="w:gz") as tar:
    # Add files to tar...
    pass

tarstream.seek(0)
response = httpx.post(
    "http://localhost:8000/api/v1/session/550e8400-e29b-41d4-a716-446655440000/",
    headers={"X-API-KEY": "notsosecret"},
    json={
        "archive": base64.b64encode(tarstream.getvalue()).decode(),
        "commands": ["ls -la"],
        "fail_fast": True,
    },
)

response.raise_for_status()
resp = response.json()

print(resp["results"][0]["output"])
print(resp["patch"])
```

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
  -H "X-API-KEY: notsosecret" \
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
