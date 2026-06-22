import warnings
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator  # noqa: TC002
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

warnings.filterwarnings(
    "ignore", message=r'directory "/run/secrets" does not exist', module="pydantic_settings.sources.providers.secrets"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="DAIV_SANDBOX_", env_ignore_empty=True)

    # Server
    HOST: str = "0.0.0.0"  # noqa: S104
    PORT: int = 8000

    # Environment
    ENVIRONMENT: Literal["local", "production"] = "production"

    # Logging
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # API
    API_V1_STR: str = "/api/v1"
    API_KEY: SecretStr

    # Sentry
    SENTRY_DSN: HttpUrl | None = None
    SENTRY_ENABLE_LOGS: bool = False
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    SENTRY_PROFILES_SAMPLE_RATE: float = 0.0
    SENTRY_SEND_DEFAULT_PII: bool = False

    # Execution
    RUNTIME: Literal["runc", "runsc"] = "runc"
    RUN_UID: int = 1000
    RUN_GID: int = 1000
    COMMAND_TIMEOUT: int = Field(default=0, ge=0)  # per-command timeout in seconds; 0 = no timeout
    # Network attached to cmd-executor containers when a session is network-enabled. None -> Docker's
    # default bridge (no compose-service DNS). Set to a compose/user-defined network (e.g.
    # "daiv_default") so containers can resolve & reach sibling services like "gitlab:8929".
    NETWORK: str | None = None
    # DNS resolvers written into a network-enabled cmd-executor's /etc/resolv.conf under the gVisor
    # (runsc) runtime. gVisor's netstack can't reach the embedded Docker resolver (127.0.0.11) that a
    # user-defined NETWORK injects, so name resolution is pointed at real upstreams instead. Ignored
    # under runc (its embedded resolver works). Comma-separated env value, e.g. "1.1.1.1,8.8.8.8".
    DNS: Annotated[list[str], NoDecode] = ["1.1.1.1", "8.8.8.8"]
    # Sibling service hostnames (comma-separated, e.g. "gitlab") resolved at session start and injected
    # as static /etc/hosts entries in network-enabled gVisor cmd-executors. This restores the
    # compose-service name resolution that overriding resolv.conf (see DNS) would otherwise drop.
    # Ignored under runc. Names that fail to resolve are skipped with a warning.
    EXTRA_HOSTS: Annotated[list[str], NoDecode] = []
    # Egress proxy (per-session MITM sidecar). When EGRESS_PROXY_ENABLED, a network-enabled session
    # is created as a triad (internal network + mitmdump sidecar + sandbox) instead of attaching the
    # sandbox directly to a network. Credentials/rules are provisioned into the sidecar, never the
    # sandbox. See the "Network Egress Proxy" section of the README.
    EGRESS_PROXY_ENABLED: bool = False
    EGRESS_PROXY_IMAGE: str = "ghcr.io/srtab/daiv-sandbox-egress:latest"
    EGRESS_PROXY_PORT: int = 8080
    # The sidecar runs trusted code (not untrusted), so it stays on runc even when sandboxes use runsc;
    # runc also avoids gVisor's embedded-DNS quirks for the proxy's own upstream resolution.
    EGRESS_PROXY_RUNTIME: Literal["runc", "runsc"] = "runc"
    # Egress-side network the sidecar's second NIC joins for upstream. None -> fall back to NETWORK,
    # then Docker's default bridge.
    EGRESS_PROXY_NETWORK: str | None = None
    EGRESS_PROXY_MEMORY_BYTES: int | None = None
    EGRESS_PROXY_CPUS: float | None = None
    # Shared CA used for MITM: cert is installed into every sandbox; key is given to sidecars only.
    # Paths are read at use time (typically files under /run/secrets).
    EGRESS_CA_CERT_FILE: str | None = None
    EGRESS_CA_KEY_FILE: str | None = None
    # Directory basenames/globs pruned by default from fs/glob and fs/grep traversals (comma-separated
    # env, e.g. ".git,__pycache__"). These are caches, IDE/VCS metadata, and build output that is never
    # hand-authored source and never dependency *source* — matching is basename-based via `find -name`,
    # so each entry prunes that dir at any depth (including inside dependency trees, e.g. a `__pycache__`
    # nested in `.venv`). Dependency-source dirs (node_modules, .venv, vendor, packages, …) are
    # deliberately NOT listed so an agent can still read dependency implementations; callers prune those
    # per-request via the `exclude` field. Setting this env REPLACES the baseline below.
    FS_PRUNE_DIRS: Annotated[list[str], NoDecode] = [
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vs",
        "__pycache__",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
        ".pyre",
        ".pytype",
        ".hypothesis",
        ".ipynb_checkpoints",
        "*.egg-info",
        ".eggs",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".parcel-cache",
        ".angular",
        ".vite",
        ".astro",
        ".docusaurus",
        ".cache",
        ".phpunit.cache",
        ".gradle",
        # `target`/`obj` are the only ambiguous bare names here (build output for Maven/Gradle and
        # .NET MSBuild respectively); included by design despite the small chance of colliding with a
        # user-named source dir. Everything else above is unambiguous by name.
        "target",
        "obj",
    ]

    @field_validator("DNS", "EXTRA_HOSTS", "FS_PRUNE_DIRS", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept comma-separated env strings (e.g. "1.1.1.1,8.8.8.8") for list settings."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    # Session locking
    REDIS_URL: str | None = None
    SESSION_LOCK_TTL_SECONDS: int = 900
    # How long a request blocks for the per-session lock before returning 409 "Session is busy".
    # A single client's batched file/exec ops are dispatched concurrently but the session can only
    # serve one at a time, so the wait must comfortably outlast a typical op (e.g. a recursive grep
    # over /workspace on a cold container) to let the loser queue instead of failing. Kept well under
    # the client's request timeout so the waiter never outlives the caller.
    SESSION_LOCK_WAIT_SECONDS: float = 30.0
    SESSION_LOCK_REFRESH_SECONDS: float = 30.0

    # Session reaper / lifecycle
    REAPER_ENABLED: bool = True
    REAPER_INTERVAL_SECONDS: int = Field(default=600, gt=0)  # sweep cadence in seconds
    SESSION_GRACE_SECONDS: int = Field(default=43200, ge=0)  # stopped -> removed age (12h)
    MAX_STOPPED_SESSIONS: int = Field(default=50, ge=0)  # LRU cap on retained stopped containers
    STOP_TIMEOUT_SECONDS: int = Field(default=2, ge=0)  # docker stop grace before SIGKILL


settings = Settings()  # type: ignore
