from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from docker.errors import APIError, NotFound

from daiv_sandbox.config import settings
from daiv_sandbox.egress.constants import CA_PATH, CONFIG_DIR
from daiv_sandbox.sessions import (
    DAIV_SANDBOX_TYPE_LABEL,
    EGRESS_SESSION_LABEL,
    SANDBOX_CA_BUNDLE,
    TYPE_EGRESS_NETWORK,
    TYPE_EGRESS_PROXY,
    SessionUnavailableError,
    _build_single_file_tar_stream,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from docker import DockerClient
    from docker.models.containers import Container

logger = logging.getLogger("daiv_sandbox.egress")


def exec_proxy_env(proxy_ip: str, port: int) -> dict[str, str]:
    """Env injected into sandbox execs so clients route through the proxy and trust the CA bundle."""
    url = f"http://{proxy_ip}:{port}"
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "ALL_PROXY": url,
        "NODE_EXTRA_CA_CERTS": SANDBOX_CA_BUNDLE,
        "GIT_SSL_CAINFO": SANDBOX_CA_BUNDLE,
        "REQUESTS_CA_BUNDLE": SANDBOX_CA_BUNDLE,
        "CURL_CA_BUNDLE": SANDBOX_CA_BUNDLE,
        "SSL_CERT_FILE": SANDBOX_CA_BUNDLE,
        "PIP_CERT": SANDBOX_CA_BUNDLE,
    }


def _egress_network_name() -> str:
    return settings.EGRESS_PROXY_NETWORK or settings.NETWORK or "bridge"


class EgressProxyManager:
    def __init__(self, client: DockerClient):
        self.client = client

    @staticmethod
    def _internal_network_name(token: str) -> str:
        """The per-session internal network name. Single source of truth so callers that need to
        resolve the proxy's IP on it (see proxy_internal_ip) can't drift from create_network."""
        return f"daiv-egress-{token}"

    def create_network(self, token: str) -> str:
        name = self._internal_network_name(token)
        self.client.networks.create(
            name=name,
            driver="bridge",
            internal=True,  # no gateway: the sandbox's only route out is the proxy
            labels={DAIV_SANDBOX_TYPE_LABEL: TYPE_EGRESS_NETWORK, EGRESS_SESSION_LABEL: token},
        )
        return name

    def start_proxy(self, token: str, network_name: str, ca_pem: bytes) -> Container:
        labels = {DAIV_SANDBOX_TYPE_LABEL: TYPE_EGRESS_PROXY, EGRESS_SESSION_LABEL: token}
        # The mitmproxy image's docker-entrypoint.sh must start as root: it runs `usermod` to align the
        # `mitmproxy` user to the confdir owner, then `gosu mitmproxy` to drop privileges. The proxy
        # process therefore runs non-root as the image's `mitmproxy` user (uid 1000 = RUN_UID default).
        # Pinning `user=RUN_UID:RUN_GID` here would defeat the entrypoint's own privilege drop (gosu
        # needs root) and the container exits with "failed switching to mitmproxy: operation not
        # permitted". The non-root guarantee is met by the entrypoint, not by a create-time `user=`.
        create_kwargs: dict = {
            "image": settings.EGRESS_PROXY_IMAGE,
            "detach": True,
            "labels": labels,
            "network": network_name,  # internal NIC
            "runtime": settings.EGRESS_PROXY_RUNTIME,
            # on-failure (not always/unless-stopped): recovers a crashed/OOM'd sidecar without letting one
            # that OOMs every boot thrash. ensure_proxy_running covers daemon restarts and exhausted retries.
            "restart_policy": {"Name": "on-failure", "MaximumRetryCount": 5},
        }
        if settings.EGRESS_PROXY_MEMORY_BYTES:
            create_kwargs["mem_limit"] = settings.EGRESS_PROXY_MEMORY_BYTES
        if settings.EGRESS_PROXY_CPUS:
            create_kwargs["nano_cpus"] = int(settings.EGRESS_PROXY_CPUS * 1e9)

        proxy = self.client.containers.create(**create_kwargs)
        # Self-cleaning: any failure between create and a successful start must remove the just-created
        # container, so this never leaks even when a caller forgot to wrap it in teardown (the caller's
        # teardown then becomes belt-and-suspenders rather than the only safety net).
        try:
            # Second NIC for upstream egress.
            self.client.networks.get(_egress_network_name()).connect(proxy)
            # Inject the combined CA PEM into the confdir before the process starts (created, not running,
            # so this lands in the writable layer mitmdump reads at boot). The proxy runs as RUN_UID, so the
            # member is owned by that user — otherwise a root-owned file would be unreadable to mitmdump.
            parent, _, filename = CA_PATH.rpartition("/")
            with _build_single_file_tar_stream(
                filename, ca_pem, mode=0o600, uid=settings.RUN_UID, gid=settings.RUN_GID
            ) as tar:
                # put_archive returns False (it does not always raise) when the daemon rejects the copy.
                # Fail closed: a proxy booted without the configured CA would silently fall back to its own
                # self-generated CA, which the sandbox does not trust.
                if not proxy.put_archive(parent, tar):
                    raise RuntimeError(f"egress: failed to inject CA into sidecar confdir for {token}")
            proxy.start()
        except Exception:
            try:
                proxy.remove(force=True, v=True)
            except NotFound:
                pass
            except Exception:
                logger.exception("egress: failed to clean up partially-created proxy for token %s", token)
            raise
        logger.info("egress: started proxy %s for token %s", proxy.short_id, token)
        return proxy

    def ensure_proxy_running(self, token: str) -> Container:
        """Return the session's proxy, warm-restarting it if it is not running.

        A warm close stops the sidecar (see stop_proxy) and an OOM/crash or daemon restart can too; the
        confdir (/run/egress: injected CA + provisioned config.json) is on the writable layer, so a
        restarted proxy comes back fully configured. Restart-on-access, like
        SandboxDockerSession._get_container. Any APIError (NotFound included) → retryable 503, not 500.
        """
        try:
            proxy = self._proxy(token)
            proxy.reload()
            if proxy.status != "running":
                logger.warning("egress: proxy for %s is %s; restarting", token, proxy.status)
                proxy.restart()
                proxy.reload()  # restart() does not refresh attrs; reload() surfaces the fresh IP/state
        except APIError as exc:
            raise SessionUnavailableError(token, f"egress proxy unavailable ({exc})") from exc
        return proxy

    def proxy_internal_ip(self, token: str) -> str:
        """Resolve the proxy's IP on the session's internal network, warm-restarting it if stopped.

        The IP is read after ensure_proxy_running, so a just-restarted proxy reports its fresh endpoint
        IP (Docker releases the endpoint IP while stopped)."""
        network_name = self._internal_network_name(token)
        proxy = self.ensure_proxy_running(token)
        nets = proxy.attrs["NetworkSettings"]["Networks"]
        ip = nets.get(network_name, {}).get("IPAddress")
        if not ip:
            raise RuntimeError(f"egress: proxy for {token} has no IP on {network_name}")
        return ip

    def provision(self, token: str, config_bytes: bytes) -> None:
        """Warm-restart the proxy if needed, then write the config JSON into it ATOMICALLY; the addon
        reloads on mtime change.

        put_archive extracts in place and is not atomic: a request landing mid-write could read a
        truncated config.json, and PolicyStore caches that failed parse against the new mtime (deny-all
        until the next write) — and since mtime only advances when the file is replaced, a re-write with
        identical bytes would not clear that cached deny-all. So stage to a temp file, then rename it
        over config.json (atomic on the same filesystem): a reader sees the old or new file whole and
        the mtime flips exactly once.

        The rename uses exec_run, which requires the proxy RUNNING (unlike put_archive), so readiness
        goes through ensure_proxy_running and both callers get a ready proxy without pre-sequencing. A
        restart fault surfaces as a retryable 503 before anything is written; so does the proxy stopping
        or vanishing mid-write. A failed rename leaves a benign config.json.tmp behind (PolicyStore
        watches only config.json); the next provision overwrites it.
        """
        proxy = self.ensure_proxy_running(token)
        try:
            # config.json holds secrets, so keep mode 0o600 and own it by the proxy user (RUN_UID) so the
            # non-root mitmdump process can read it; a root-owned 0o600 file would deny-fail closed.
            with _build_single_file_tar_stream(
                "config.json.tmp", config_bytes, mode=0o600, uid=settings.RUN_UID, gid=settings.RUN_GID
            ) as tar:
                if not proxy.put_archive(CONFIG_DIR, tar):
                    raise RuntimeError(f"egress: failed to stage config for {token}")
            # rename() is atomic within a filesystem: config.json flips content+mtime in one step, so a
            # concurrent PolicyStore read never observes a partial file. rename() keeps the temp file's
            # inode (incl. RUN_UID ownership) — no chown needed. Run as root to avoid any confdir
            # permission edge case.
            result = proxy.exec_run(
                ["mv", "-f", f"{CONFIG_DIR}/config.json.tmp", f"{CONFIG_DIR}/config.json"], user="root"
            )
            if result.exit_code != 0:
                raise RuntimeError(
                    f"egress: failed to install config for {token}: [{result.exit_code}] {result.output!r}"
                )
        except APIError as exc:
            # The proxy stopped between the restart and the mv, or was removed (NotFound is an APIError
            # subclass): a retryable infra fault, not a config bug — map to 503, not 500.
            raise SessionUnavailableError(token, f"refreshed: egress proxy unavailable ({exc})") from exc

    def teardown(self, token: str) -> None:
        # v=True: the mitmproxy base image declares VOLUME on the confdir, so without it each proxy
        # leaks one anonymous volume.
        proxies = self._list(TYPE_EGRESS_PROXY, token)
        self._best_effort(proxies, lambda proxy: proxy.remove(force=True, v=True), "remove")
        networks = self.client.networks.list(filters={"label": f"{EGRESS_SESSION_LABEL}={token}"})
        self._best_effort(networks, lambda net: net.remove(), "remove")

    def stop_proxy(self, token: str) -> None:
        """Stop — but do NOT remove — the session's proxy sidecar, freeing its memory while the session
        is warm-stopped (a non-force close). A resumed session warm-restarts it via
        ``ensure_proxy_running``; ``teardown`` (force close or reaper) still owns final removal. A manual
        ``stop`` also suppresses the on-failure restart_policy, so the daemon does not resurrect it."""
        proxies = self._list(TYPE_EGRESS_PROXY, token)
        self._best_effort(proxies, lambda proxy: proxy.stop(timeout=settings.STOP_TIMEOUT_SECONDS), "stop")

    @staticmethod
    def _best_effort(resources: list, op: Callable[..., object], what: str) -> None:
        """Apply *op* to each resource, tolerating faults: an already-gone resource (NotFound) is
        success, and any other failure is logged and swallowed so one stuck resource never masks the
        caller's original error or skips the rest of the cleanup (the reaper retries next sweep).

        The listing itself is deliberately not wrapped: a daemon fault there propagates, which degrades
        to "the resource lives until the reaper" rather than to a new leak."""
        for resource in resources:
            try:
                op(resource)
            except NotFound:
                pass
            except Exception:
                name = getattr(resource, "short_id", None) or getattr(resource, "name", "?")
                logger.exception("egress: failed to %s %s", what, name)

    def _list(self, type_label: str, token: str) -> list:
        return self.client.containers.list(
            all=True, filters={"label": [f"{DAIV_SANDBOX_TYPE_LABEL}={type_label}", f"{EGRESS_SESSION_LABEL}={token}"]}
        )

    def _proxy(self, token: str) -> Container:
        proxies = self._list(TYPE_EGRESS_PROXY, token)
        if not proxies:
            raise NotFound(f"egress: no proxy for token {token}")
        return proxies[0]
