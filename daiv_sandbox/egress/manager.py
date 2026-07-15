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
            # Auto-recover the sidecar after a crash/OOM (non-zero exit), so a still-warm session does
            # not lose egress. A backstop, not the whole story: on-failure reliably covers a crash/OOM
            # but — unlike always/unless-stopped — does not guarantee a restart across a daemon restart;
            # proxy_internal_ip's warm-restart-on-access is the real safety net for that case (and for
            # exhausted retries). on-failure — NOT always/unless-stopped — so a proxy that OOMs on every
            # boot can't thrash forever (bounded by MaximumRetryCount) and a clean exit-0 shutdown is
            # left alone. teardown force-removes the container, which overrides the policy, so this never
            # fights cleanup.
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
                proxy.remove(force=True)
            except NotFound:
                pass
            except Exception:
                logger.exception("egress: failed to clean up partially-created proxy for token %s", token)
            raise
        logger.info("egress: started proxy %s for token %s", proxy.short_id, token)
        return proxy

    def proxy_internal_ip(self, token: str) -> str:
        network_name = self._internal_network_name(token)
        # A sidecar can die (OOM, crash) while its session's sandbox stays warm — Docker then releases
        # its endpoint IP, so a stopped proxy reads back with no IPAddress. The restart policy (see
        # start_proxy) recovers a crash/OOM, but a request can arrive before it fires, its retries can be
        # exhausted, or a daemon restart may have left the proxy down. Warm-restart it here too — the
        # same restart-on-access idea as SandboxDockerSession._get_container — so the session self-heals
        # instead of losing egress until it is explicitly recreated. Safe because the confdir
        # (/run/egress: the injected CA, plus config.json once provisioned) is on the container's
        # writable layer, so a restarted proxy comes back with its CA intact. Like provision(), any
        # Docker APIError (missing proxy, or one stopping/vanishing mid-restart; NotFound is a subclass)
        # becomes a retryable SessionUnavailableError (503) rather than an opaque 500.
        try:
            proxy = self._proxy(token)
            proxy.reload()
            if proxy.status != "running":
                logger.warning("egress: proxy for %s is %s; restarting", token, proxy.status)
                proxy.restart()
                proxy.reload()  # restart() does not refresh attrs; reload() surfaces the fresh IP
        except APIError as exc:
            raise SessionUnavailableError(token, f"egress proxy unavailable ({exc})") from exc
        nets = proxy.attrs["NetworkSettings"]["Networks"]
        ip = nets.get(network_name, {}).get("IPAddress")
        if not ip:
            raise RuntimeError(f"egress: proxy for {token} has no IP on {network_name}")
        return ip

    def provision(self, token: str, config_bytes: bytes) -> None:
        """Write the config JSON into the running proxy ATOMICALLY; the addon reloads on mtime change.

        put_archive extracts in place and is not atomic: a request landing mid-write could read a
        truncated config.json, and PolicyStore caches that failed parse against the new mtime (deny-all
        until the next write) — and since mtime only advances when the file is replaced, a re-write with
        identical bytes would not clear that cached deny-all. So stage to a temp file, then rename it
        over config.json (atomic on the same filesystem): a reader sees the old or new file whole and
        the mtime flips exactly once.

        The rename uses exec_run, which requires the proxy container to be RUNNING (unlike put_archive,
        which the daemon also accepts against a stopped container). A proxy can be stopped or gone while
        its session's sandbox still exists — e.g. after a daemon restart or a reaper sweep. (The proxy
        carries an on-failure restart_policy and proxy_internal_ip warm-restarts it on access, but
        provision deliberately does NOT restart: it only stages+renames config into an already-running
        proxy, so it fails 503 here and leaves recovery to those paths.) We check status up front, and
        also translate any Docker APIError/NotFound raised during the operation (the proxy stopping
        between the check and the mv, or having been removed) into SessionUnavailableError, so the caller
        reports 503 (retryable) rather than an opaque 500. A failed rename leaves a benign
        config.json.tmp behind (PolicyStore watches only config.json); the next provision overwrites it.
        """
        try:
            proxy = self._proxy(token)
            proxy.reload()
            if proxy.status != "running":
                raise SessionUnavailableError(token, f"refreshed: egress proxy is {proxy.status}")
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
            # The proxy stopped between the status check and the mv, or was removed (NotFound is an
            # APIError subclass): a retryable infra fault, not a config bug — map to 503, not 500.
            raise SessionUnavailableError(token, f"refreshed: egress proxy unavailable ({exc})") from exc

    def teardown(self, token: str) -> None:
        # Best-effort: an already-gone resource (NotFound) is success; any other failure is logged and
        # swallowed so one stuck resource never masks the caller's original error or skips the rest of
        # the cleanup (the reaper retries on the next sweep).
        for proxy in self._list(TYPE_EGRESS_PROXY, token):
            try:
                proxy.remove(force=True)
            except NotFound:
                pass
            except Exception:
                logger.exception("egress: failed to remove proxy %s", getattr(proxy, "short_id", "?"))
        for net in self.client.networks.list(filters={"label": f"{EGRESS_SESSION_LABEL}={token}"}):
            try:
                net.remove()
            except NotFound:
                pass
            except Exception:
                logger.exception("egress: failed to remove network %s", getattr(net, "name", "?"))

    def _list(self, type_label: str, token: str) -> list:
        return self.client.containers.list(
            all=True, filters={"label": [f"{DAIV_SANDBOX_TYPE_LABEL}={type_label}", f"{EGRESS_SESSION_LABEL}={token}"]}
        )

    def _proxy(self, token: str) -> Container:
        proxies = self._list(TYPE_EGRESS_PROXY, token)
        if not proxies:
            raise NotFound(f"egress: no proxy for token {token}")
        return proxies[0]
