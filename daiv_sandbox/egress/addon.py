"""mitmproxy addon enforcing the egress policy and injecting credentials.

Loaded by mitmdump inside the sidecar (`mitmdump -s addon.py`). Deliberately avoids a top-level
mitmproxy import so the module is importable (and unit-testable) without mitmproxy installed; the
only mitmproxy touch point is `_forbidden`, which imports lazily.
"""

from __future__ import annotations

import logging
import os

try:  # works both as `daiv_sandbox.egress.policy` (tests) and flat `policy` (sidecar image, see Dockerfile)
    from daiv_sandbox.egress.constants import CONFIG_PATH, CONFIG_PATH_ENV
    from daiv_sandbox.egress.policy import PolicyStore
except ImportError:  # pragma: no cover - exercised only inside the sidecar image
    from constants import CONFIG_PATH, CONFIG_PATH_ENV  # type: ignore[no-redef]
    from policy import PolicyStore  # type: ignore[no-redef]

logger = logging.getLogger("daiv_sandbox.egress")


def _forbidden(flow) -> None:  # pragma: no cover - thin mitmproxy glue, stubbed in tests
    from mitmproxy.http import Response

    flow.response = Response.make(403, b"egress: destination not allowed\n", {"Content-Type": "text/plain"})


class EgressAddon:
    def __init__(self, config_path: str | None = None):
        self._store = PolicyStore(config_path or os.environ.get(CONFIG_PATH_ENV, CONFIG_PATH))

    def http_connect(self, flow) -> None:
        """CONNECT fires before TLS — enforce reachability here so deny works even in passthrough."""
        host = flow.request.host
        if not self._store.current().evaluate(host, "CONNECT").allow:
            logger.warning("egress: blocked CONNECT to %s", host)
            _forbidden(flow)

    def tls_clienthello(self, data) -> None:
        """For allowed-but-not-intercepted hosts, tunnel TLS untouched (no MITM)."""
        host = data.client_hello.sni or ""
        decision = self._store.current().evaluate(host, "CONNECT")
        if decision.allow and not decision.intercept:
            data.ignore_connection = True

    def request(self, flow) -> None:
        host = flow.request.pretty_host
        decision = self._store.current().evaluate(host, flow.request.method)
        if not decision.allow:
            logger.warning("egress: blocked %s %s", flow.request.method, host)
            _forbidden(flow)
            return
        if decision.inject is not None:
            name, value = decision.inject
            if name in flow.request.headers:
                del flow.request.headers[name]
            flow.request.headers[name] = value
            logger.info("egress: injected %s for %s", name, host)


addons = [EgressAddon()]
