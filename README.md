## What is `daiv-sandbox`?

`daiv-sandbox` is a FastAPI application designed to securely execute arbitrary commands and untrusted code within a controlled environment. Each execution is isolated in a transient Docker container, which is automatically created and destroyed with every request, ensuring a clean and secure execution space.

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

| Environment Variable                | Description                                                 | Options/Default                                               |
| ----------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------- |
| **DAIV_SANDBOX_API_KEY**            | The API key required to access the sandbox API.             |                                                               |
| **DAIV_SANDBOX_ENVIRONMENT**        | The deployment environment.                                 | Options: `local`, `staging`, `production`<br>Default: "local" |
| **DAIV_SANDBOX_SENTRY_DSN**         | The DSN for Sentry error tracking.                          | Optional                                                      |
| **DAIV_SANDBOX_MAX_EXECUTION_TIME** | The maximum allowed execution time for commands in seconds. | Default: 600                                                  |
| **DAIV_SANDBOX_RUNTIME**            | The container runtime to use.                               | Options: `runc`, `runsc`<br>Default: "runc"                   |
| **DAIV_SANDBOX_KEEP_TEMPLATE**      | Whether to keep the execution template after finishing.     | Default: False                                                |

## Usage

`daiv-sandbox` provides a REST API that can be used to execute arbitrary commands on a provided archive, for instance, a `tar.gz` archive containing a repository codebase. The archive is extracted to a temporary directory and the commands are executed in the root of the extracted directory. The output of the commands is returned in the response along with a list of changed files by the last command.

This is very useful to increase the capabilities of an AI agent with code editing capabilities. For instance, you can use it to apply formatting changes to a repository codebase like running `black`, `isort`, `ruff`, `prettier`, etc...

Here is an example using `curl`:

```sh
$ curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: notsosecret" \
  -d "{\"run_id\": \"550e8400-e29b-41d4-a716-446655440000\", \"base_image\": \"python:3.12\", \"archive\": \"$(base64 -w 0 django-webhooks-master.tar.gz)\", \"commands\": [\"ls -la\"]}" \
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
        },
    )
    response.raise_for_status()
    resp = response.json()
```

## License

`daiv-sandbox` is licensed under the [Apache License 2.0](LICENSE).
