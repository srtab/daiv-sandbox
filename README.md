# DAIV Sandbox

![Python Version](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsrtab%2Fdaiv-sandbox%2Fmain%2Fpyproject.toml)
[![GitHub License](https://img.shields.io/github/license/srtab/daiv-sandbox)](https://github.com/srtab/daiv-sandbox/blob/main/LICENSE)
[![Actions Status](https://github.com/srtab/daiv-sandbox/actions/workflows/ci.yml/badge.svg)](https://github.com/srtab/daiv-sandbox/actions)

## What is `daiv-sandbox`?

`daiv-sandbox` is a FastAPI application designed to securely execute arbitrary commands and untrusted code within a controlled environment. Each execution is isolated in a transient Docker container, which is automatically created and destroyed with every request, ensuring a clean and secure execution space. It is designed to be used as a code/commands executor for [DAIV](https://github.com/srtab/daiv).

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

> **Note:** If you're getting an error `"overlay flag is incompatible with shared file access for rootfs"`, you need to add `--overlay2=none` flag to the `gVisor` configuration.

After installing `gVisor`, you need to define the `DAIV_SANDBOX_RUNTIME` environment variable to use `runsc` when running the container:

```sh
$ docker run --rm -d -p 8000:8000 -e DAIV_SANDBOX_API_KEY=my-secret-api-key -e DAIV_SANDBOX_RUNTIME=runsc ghcr.io/srtab/daiv-sandbox:latest
```

### Configuration

All settings are configurable via environment variables. The available settings are:

| Environment Variable                   | Description                                                 | Options/Default                                                             |
| -------------------------------------- | ----------------------------------------------------------- | --------------------------------------------------------------------------- |
| **DAIV_SANDBOX_API_KEY**               | The API key required to access the sandbox API.             |                                                                             |
| **DAIV_SANDBOX_ENVIRONMENT**           | The deployment environment.                                 | Options: `local`, `production`<br>Default: "production"                     |
| **DAIV_SANDBOX_SENTRY_DSN**            | The DSN for Sentry error tracking.                          | Optional                                                                    |
| **DAIV_SANDBOX_SENTRY_ENABLE_TRACING** | Whether to enable tracing for Sentry error tracking.        | Default: False                                                              |
| **DAIV_SANDBOX_MAX_EXECUTION_TIME**    | The maximum allowed execution time for commands in seconds. | Default: 600                                                                |
| **DAIV_SANDBOX_RUNTIME**               | The container runtime to use.                               | Options: `runc`, `runsc`<br>Default: "runc"                                 |
| **DAIV_SANDBOX_KEEP_TEMPLATE**         | Whether to keep the execution template after finishing.     | Default: False                                                              |
| **DAIV_SANDBOX_HOST**                  | The host to bind the service to.                            | Default: "0.0.0.0"                                                          |
| **DAIV_SANDBOX_PORT**                  | The port to bind the service to.                            | Default: 8000                                                               |
| **DAIV_SANDBOX_LOG_LEVEL**             | The log level to use.                                       | Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`<br>Default: "INFO" |

## Usage

### Running Commands

`daiv-sandbox` provides a REST API that can be used to execute arbitrary commands on a provided archive, for instance, a `tar.gz` archive containing a repository codebase. The archive is extracted to a temporary directory and the commands are executed in the root of the extracted directory. The output of the commands is returned in the response along with a list of changed files by the last command.

By default, all commands are executed sequentially regardless of their exit codes. However, you can enable fail-fast behavior by setting the `fail_fast` parameter to `true`, which will stop execution immediately if any command fails (returns a non-zero exit code).

This is very useful to increase the capabilities of an AI agent with code editing capabilities. For instance, you can use it to apply formatting changes to a repository codebase like running `black`, `isort`, `ruff`, `prettier`, etc...

The following table describes the parameters for the `run/commands` endpoint:

| Parameter    | Description                                         | Required | Valid Values                    |
| ------------ | --------------------------------------------------- | -------- | ------------------------------- |
| `run_id`     | The unique identifier for the run.                  | Yes      | Any UUID4                       |
| `base_image` | The base image to use for the container.            | Yes      | Any valid Docker image          |
| `archive`    | The archive to extract and execute the commands on. | Yes      | Base64 encoded `tar.gz` archive |
| `commands`   | The commands to execute.                            | Yes      | List of strings                 |
| `fail_fast`  | Stop execution if any command fails.               | No       | `true` or `false` (default: `false`) |

> [!WARNING]
> The `base_image` need to be a ditro image. Distroless images will not work as there is no shell available in the container to maintain the image running indefinitely.

Here is an example using `curl`:

```sh
$ curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: notsosecret" \
  -d "{\"run_id\": \"550e8400-e29b-41d4-a716-446655440000\", \"base_image\": \"python:3.12\", \"archive\": \"$(base64 -w 0 django-webhooks-master.tar.gz)\", \"commands\": [\"ls -la\"], \"fail_fast\": true}" \
  http://localhost:8888/api/v1/run/commands/

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
  "archive": null // If no file changes were made, the archive is not returned
}
```

Here is an example using `python`:

```python
import httpx
import tarfile


with tarfile.open(fileobj=tarstream, mode="r:*") as tar:
    response = httpx.post(
        "http://localhost:8888/api/v1/run/commands/",
        headers={"X-API-KEY": "notsosecret"},
        json={
            "run_id": "550e8400-e29b-41d4-a716-446655440000",
            "base_image": "python:3.12",
            "archive": base64.b64encode(tarstream.getvalue()).decode(),
            "commands": ["ls -la"],
            "fail_fast": True,
        },
    )
    response.raise_for_status()
    resp = response.json()
```

### Running Code

`daiv-sandbox` also provides a REST API that can be used to execute arbitrary code. The code is executed in a temporary directory and the output of the code is returned in the response.

The following table describes the parameters for the `run/code` endpoint:

| Parameter      | Description                                   | Required | Valid Values    |
| -------------- | --------------------------------------------- | -------- | --------------- |
| `run_id`       | The unique identifier for the run.            | Yes      | Any UUID4       |
| `language`     | The language to use for the code execution.   | Yes      | `python`        |
| `dependencies` | The dependencies to install in the container. | No       | List of strings |
| `code`         | The code to execute.                          | Yes      | String          |

> [!NOTE]
> Currently, only `python` language is supported. But it's planned to support more languages in the future. Reach out to us or open a PR if you need a specific language.

Here is an example using `curl`:

```sh
$ curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: notsosecret" \
  -d "{\"run_id\": \"550e8400-e29b-41d4-a716-446655440000\", \"language\": \"python\", \"dependencies\": [\"requests\"], \"code\": \"print('Hello, World!')\"}" \
  http://localhost:8888/api/v1/run/code/
```

The response will be a JSON object with the following structure:

```json
{
  "output": "Hello, World!"
}
```

Here is an example using `python`:

```python
import httpx

response = httpx.post(
    "http://localhost:8888/api/v1/run/code/",
    headers={"X-API-KEY": "notsosecret"},
    json={"run_id": "550e8400-e29b-41d4-a716-446655440000", "language": "python", "dependencies": ["requests"], "code": "print('Hello, World!')"},
)
response.raise_for_status()
resp = response.json()
```

## Contributing

We welcome contributions! Whether you want to fix a bug, add a new feature, or improve documentation, please refer to the [CONTRIBUTING.md](CONTRIBUTING.md) file for more information.

## License

This project is licensed under the [Apache 2.0 License](LICENSE).

## Support & Community

For questions or support, please open an issue in the GitHub repository. Contributions, suggestions, and feedback are greatly appreciated!

**Happy Coding!**
