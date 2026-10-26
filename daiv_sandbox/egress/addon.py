"""mitmproxy addon enforcing the egress policy and injecting credentials.

Loaded by mitmdump inside the sidecar (`mitmdump -s addon.py`). Deliberately avoids a top-level
mitmproxy import so the module is importable (and unit-testable) without mitmproxy installed; the
only mitmproxy touch point is `_forbidden`, which imports lazily.
"""

from __future__ import annotations

import logging
import os
import re

try:  # works both as `daiv_sandbox.egress.policy` (tests) and flat `policy` (sidecar image, see Dockerfile)
    from daiv_sandbox.egress.constants import CONFIG_PATH, CONFIG_PATH_ENV
    from daiv_sandbox.egress.policy import (
        REASON_HOST_HEADER_MISMATCH,
        REASON_HOST_NOT_LISTED,
        REASON_METHOD_NOT_ALLOWED,
        REASON_PATH_NOT_LISTED,
        BlockReason,
        PolicyStore,
    )
except ImportError:  # pragma: no cover - exercised only inside the sidecar image
    from constants import CONFIG_PATH, CONFIG_PATH_ENV  # type: ignore[no-redef]
    from policy import (  # type: ignore[no-redef]
        REASON_HOST_HEADER_MISMATCH,
        REASON_HOST_NOT_LISTED,
        REASON_METHOD_NOT_ALLOWED,
        REASON_PATH_NOT_LISTED,
        BlockReason,
        PolicyStore,
    )

logger = logging.getLogger("daiv_sandbox.egress")

_AUTHORITY = re.compile(r"(?:\[(?P<ipv6>[0-9A-Fa-f:.]+)\]|(?P<name>[A-Za-z0-9._-]+))(?::[0-9]{1,5})?")


def _forbidden(flow) -> None:  # pragma: no cover - thin mitmproxy glue, stubbed in tests
    if flow.request.stream:
        # mitmproxy raises on a response set for a request whose body it streams; drop the connection.
        flow.kill()
        return
    from mitmproxy.http import Response

    flow.response = Response.make(403, b"egress: destination not allowed\n", {"Content-Type": "text/plain"})


def _describe_block(block) -> str:
    """Render a deny Decision's BlockReason into a single, greppable log suffix.

    Carries the data an operator needs to transpose the block into a rule: for method-not-allowed, the
    matched rule's host glob and the methods it currently permits (so they can see whether to extend it).
    """
    if block is None:  # defensive: a deny should always carry a reason
        return "reason=blocked"
    if block.code == REASON_METHOD_NOT_ALLOWED:
        methods = ", ".join(block.methods or ())
        return f"reason=method-not-allowed (matched rule host={block.host!r}, allows methods=[{methods}])"
    if block.code == REASON_HOST_NOT_LISTED:
        return "reason=host-not-listed (no rule matched; default=deny)"
    if block.code == REASON_PATH_NOT_LISTED:
        return f"reason=path-not-listed (no path_prefix rule covers path={block.path!r}; default=deny)"
    return f"reason={block.code}"


def _deny(flow, log_prefix: str, block) -> None:
    """Deny a flow fail-closed: commit the 403 BEFORE formatting the reason. mitmproxy fails OPEN on an
    un-denied hook, so _forbidden must run before any code (reason formatting) that could conceivably
    raise. Both the CONNECT and request deny paths route through here, so that ordering lives in one place.
    """
    _forbidden(flow)
    logger.warning("egress: blocked %s — %s", log_prefix, _describe_block(block))


def _host_header_matches(request) -> bool:
    """True when the request has no Host/:authority header, or one that is exactly ``host[:port]`` (IPv6
    bracketed) for the host the request is sent to."""
    authority = request.host_header
    if authority is None:
        return True
    match = _AUTHORITY.fullmatch(authority)
    return match is not None and (match["ipv6"] or match["name"]).lower() == request.host.lower()


class EgressAddon:
    def __init__(self, config_path: str | None = None):
        self._store = PolicyStore(config_path or os.environ.get(CONFIG_PATH_ENV, CONFIG_PATH))

    def http_connect(self, flow) -> None:
        """CONNECT fires before TLS — enforce reachability here so deny works even in passthrough.

        Wrapped fail-closed: mitmproxy fails OPEN on an unhandled hook exception, so any unexpected
        error must still deny the CONNECT rather than let the tunnel open un-checked.
        """
        host = flow.request.host
        try:
            decision = self._store.current().evaluate(host, "CONNECT")
        except Exception:
            logger.exception("egress: error evaluating CONNECT to %s; failing closed (deny)", host)
            _forbidden(flow)
            return
        if not decision.allow:
            _deny(flow, f"CONNECT {host}", decision.block)

    def tls_clienthello(self, data) -> None:
        """For allowed-but-not-intercepted hosts, tunnel TLS untouched (no MITM).

        Fail-closed: leaving ``ignore_connection`` False forces interception, so any error (including a
        ``client_hello`` that failed to parse and is None) safely falls back to MITM rather than a
        passthrough mitmproxy would otherwise grant on an unhandled hook exception.
        """
        try:
            client_hello = data.client_hello
            sni = client_hello.sni if client_hello else None
            if not sni:
                # No usable SNI (e.g. a ClientHello that failed to parse): we can't identify the host,
                # so never tunnel it untouched — force interception, where http_connect/request re-gate it.
                return
            decision = self._store.current().evaluate(sni, "CONNECT")
        except Exception:
            logger.exception("egress: error in tls_clienthello; failing closed (intercept)")
            return
        if decision.allow and not decision.intercept:
            data.ignore_connection = True

    def requestheaders(self, flow) -> None:
        """Gate and inject on headers, not in ``request``: mitmproxy forwards a body over
        ``stream_large_bodies`` (see egress_proxy/Dockerfile) before ``request`` fires."""
        # `host` is where the connection goes (the CONNECT target inside a tunnel); `pretty_host` is the
        # client-controlled Host header, so judging by it would hand one host's secret to another.
        host = flow.request.host
        try:
            decision = self._store.current().evaluate(host, flow.request.method, flow.request.path)
            header_matches = decision.inject is None or _host_header_matches(flow.request)
        except Exception:
            # Fail closed: deny and never fall through to the credential-injection branch.
            logger.exception("egress: error evaluating %s %s; failing closed (deny)", flow.request.method, host)
            _forbidden(flow)
            return
        if not decision.allow:
            _deny(flow, f"{flow.request.method} {host}", decision.block)
            return
        if not header_matches:
            prefix = f"{flow.request.method} {host} (Host header {flow.request.host_header!r})"
            _deny(flow, prefix, BlockReason(REASON_HOST_HEADER_MISMATCH))
            return
        if decision.inject is not None:
            name, value = decision.inject
            if name in flow.request.headers:
                del flow.request.headers[name]
            flow.request.headers[name] = value
            logger.info("egress: injected %s for %s", name, host)


addons = [EgressAddon()]
