import contextlib
import json
import logging
import types

import pytest

from daiv_sandbox.egress import addon as addon_mod
from daiv_sandbox.egress.addon import EgressAddon
from daiv_sandbox.egress.policy import REASON_METHOD_NOT_ALLOWED, BlockReason


def _write_cfg(tmp_path, **kw):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({
            "policy": {
                "default": kw.get("default", "deny"),
                "intercept": kw.get("intercept", "all"),
                "rules": kw.get("rules", []),
            },
            "secrets": kw.get("secrets", {}),
        })
    )
    return str(path)


def _flow(host="github.com", method="GET"):
    headers = {}
    request = types.SimpleNamespace(pretty_host=host, host=host, method=method, headers=headers)
    return types.SimpleNamespace(request=request, response=None)


@pytest.fixture(autouse=True)
def _stub_forbidden(monkeypatch):
    # Avoid importing mitmproxy in unit tests: _forbidden returns a sentinel marker.
    monkeypatch.setattr(addon_mod, "_forbidden", lambda flow: setattr(flow, "response", "FORBIDDEN"))


def test_connect_to_denied_host_is_blocked(tmp_path):
    a = EgressAddon(_write_cfg(tmp_path))
    flow = _flow(host="evil.example")
    a.http_connect(flow)
    assert flow.response == "FORBIDDEN"


def test_connect_to_allowed_host_is_not_blocked(tmp_path):
    a = EgressAddon(_write_cfg(tmp_path, rules=[{"host": "github.com"}]))
    flow = _flow(host="github.com")
    a.http_connect(flow)
    assert flow.response is None


def test_request_injects_configured_header(tmp_path):
    a = EgressAddon(
        _write_cfg(
            tmp_path,
            rules=[{"host": "github.com", "inject": "gh"}],
            secrets={"gh": {"header": "Authorization", "value": "Bearer t"}},
        )
    )
    flow = _flow(host="github.com")
    flow.request.headers["Authorization"] = "attacker-supplied"
    a.request(flow)
    assert flow.request.headers["Authorization"] == "Bearer t"  # stripped + replaced


def test_tls_clienthello_passthrough_for_allowed_noncred(tmp_path):
    a = EgressAddon(_write_cfg(tmp_path, default="allow", intercept="credentialed"))
    data = types.SimpleNamespace(client_hello=types.SimpleNamespace(sni="pypi.org"), ignore_connection=False)
    a.tls_clienthello(data)
    assert data.ignore_connection is True  # not intercepted -> tunnel untouched


def test_request_denies_disallowed_method_on_method_restricted_host(tmp_path):
    """The real per-host method limit is enforced post-MITM in request(): a host reachable via CONNECT
    (methods:["GET"]) must still have a POST blocked at the request phase. This is the actual
    enforcement point for method limits — the policy-layer test alone does not exercise it."""
    a = EgressAddon(_write_cfg(tmp_path, rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    flow = _flow(host="api.github.com", method="POST")
    a.request(flow)
    assert flow.response == "FORBIDDEN"


def test_blocked_connect_logs_host_not_listed_reason(tmp_path, caplog):
    """A blocked CONNECT logs the host plus a host-not-listed reason, so an operator sees why and what
    host to add a rule for."""
    a = EgressAddon(_write_cfg(tmp_path, rules=[{"host": "github.com"}]))
    with caplog.at_level(logging.WARNING, logger="daiv_sandbox.egress"):
        a.http_connect(_flow(host="evil.example"))
    msg = caplog.text
    assert "evil.example" in msg
    assert "host-not-listed" in msg


def test_blocked_request_logs_method_not_allowed_with_matched_rule(tmp_path, caplog):
    """A method blocked on a listed host logs method-not-allowed plus the matched rule's host glob and
    its currently-allowed methods — the data needed to extend that rule."""
    a = EgressAddon(_write_cfg(tmp_path, rules=[{"host": "*.github.com", "methods": ["GET", "POST"]}]))
    with caplog.at_level(logging.WARNING, logger="daiv_sandbox.egress"):
        a.request(_flow(host="api.github.com", method="DELETE"))
    msg = caplog.text
    assert "DELETE" in msg and "api.github.com" in msg
    assert "method-not-allowed" in msg
    assert "*.github.com" in msg  # matched rule host glob
    assert "GET" in msg and "POST" in msg  # currently-allowed methods


def test_blocked_request_on_unlisted_host_logs_host_not_listed(tmp_path, caplog):
    """A request to a wholly unlisted host (default-deny) logs the host-not-listed reason. The method and
    host come from the log line's prefix, not the reason payload — host-not-listed carries no rule detail
    (there is no rule to cite), so _describe_block emits only the static reason string."""
    a = EgressAddon(_write_cfg(tmp_path))
    with caplog.at_level(logging.WARNING, logger="daiv_sandbox.egress"):
        a.request(_flow(host="evil.example", method="POST"))
    msg = caplog.text
    assert "POST" in msg and "evil.example" in msg
    assert "host-not-listed" in msg


def test_request_allows_permitted_method_on_method_restricted_host(tmp_path):
    a = EgressAddon(_write_cfg(tmp_path, rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    flow = _flow(host="api.github.com", method="GET")
    a.request(flow)
    assert flow.response is None


def test_addon_denies_when_config_is_malformed(tmp_path):
    """A corrupt config.json must make the addon fail closed (deny), not fall open to allow."""
    path = tmp_path / "config.json"
    path.write_text("{ not valid json")
    a = EgressAddon(str(path))
    flow = _flow(host="github.com", method="GET")
    a.http_connect(flow)
    assert flow.response == "FORBIDDEN"


def _raise(*_a, **_k):
    raise RuntimeError("boom")


def test_http_connect_fails_closed_when_evaluation_raises(tmp_path, monkeypatch):
    """mitmproxy FAILS OPEN on an unhandled hook exception. If policy evaluation ever raises (a future
    bug), http_connect must still deny the CONNECT rather than let the tunnel open un-checked."""
    a = EgressAddon(_write_cfg(tmp_path, default="allow", rules=[{"host": "github.com"}]))
    monkeypatch.setattr(a._store, "current", _raise)
    flow = _flow(host="github.com")
    a.http_connect(flow)
    assert flow.response == "FORBIDDEN"


def test_request_fails_closed_when_evaluation_raises(tmp_path, monkeypatch):
    """A raise in request() must deny AND never reach the credential-injection branch — not fall open."""
    a = EgressAddon(
        _write_cfg(
            tmp_path,
            default="allow",
            rules=[{"host": "github.com", "inject": "gh"}],
            secrets={"gh": {"header": "Authorization", "value": "Bearer t"}},
        )
    )
    monkeypatch.setattr(a._store, "current", _raise)
    flow = _flow(host="github.com")
    a.request(flow)
    assert flow.response == "FORBIDDEN"
    assert "Authorization" not in flow.request.headers  # credential never injected on the error path


@pytest.mark.parametrize("hook", [EgressAddon.request, EgressAddon.http_connect], ids=["request", "http_connect"])
def test_hooks_commit_deny_before_formatting_reason(tmp_path, monkeypatch, hook):
    """Hardening: both deny paths route through _deny, which attaches the 403 BEFORE formatting the reason,
    so a formatting bug can never leave the flow un-denied. mitmproxy fails OPEN on an un-denied hook, so
    deny order matters even though _describe_block is total today."""
    a = EgressAddon(_write_cfg(tmp_path))  # default-deny
    monkeypatch.setattr(addon_mod, "_describe_block", _raise)
    flow = _flow(host="evil.example", method="POST")
    with contextlib.suppress(RuntimeError):
        hook(a, flow)
    assert flow.response == "FORBIDDEN"


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (None, "reason=blocked"),
        (BlockReason("future-code"), "reason=future-code"),
        (BlockReason(REASON_METHOD_NOT_ALLOWED, host="x", methods=None), "methods=[]"),
    ],
    ids=["missing-reason", "unknown-code", "method-not-allowed-no-methods"],
)
def test_describe_block_is_total(block, expected):
    """_describe_block never raises inside the fail-open hook: a missing reason renders a static string, an
    unknown code degrades to a bare reason= line, and a method-not-allowed reason with no methods still
    formats. None of these arise via evaluate() — the guards are belt-and-suspenders for the fail-open hook."""
    assert expected in addon_mod._describe_block(block)


def test_tls_clienthello_fails_closed_when_evaluation_raises(tmp_path, monkeypatch):
    """An unexpected error while evaluating the SNI must force interception (ignore_connection stays
    False), never a silent passthrough — mitmproxy fails OPEN on an unhandled hook exception. This
    covers the except branch, distinct from the `client_hello is None` early return below."""
    a = EgressAddon(_write_cfg(tmp_path, default="allow", intercept="credentialed", rules=[{"host": "pypi.org"}]))
    monkeypatch.setattr(a._store, "current", _raise)
    data = types.SimpleNamespace(client_hello=types.SimpleNamespace(sni="pypi.org"), ignore_connection=False)
    a.tls_clienthello(data)
    assert data.ignore_connection is False  # forced interception, not passthrough


def test_tls_clienthello_intercepts_when_clienthello_is_none(tmp_path):
    """A tls_clienthello event can arrive with client_hello=None (the ClientHello failed to parse).
    Accessing `.sni` would AttributeError -> mitmproxy fails open (passthrough). Must force MITM instead."""
    a = EgressAddon(_write_cfg(tmp_path, default="allow", intercept="credentialed", rules=[{"host": "pypi.org"}]))
    data = types.SimpleNamespace(client_hello=None, ignore_connection=False)
    a.tls_clienthello(data)
    assert data.ignore_connection is False  # not tunnelled untouched -> intercepted (safe direction)


def test_tls_clienthello_intercepts_method_restricted_host(tmp_path):
    """A method-restricted host must be intercepted (not passed through) so the method limit is enforced
    post-TLS. The existing passthrough test only covers the allow-and-not-intercept branch."""
    a = EgressAddon(_write_cfg(tmp_path, rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    data = types.SimpleNamespace(client_hello=types.SimpleNamespace(sni="api.github.com"), ignore_connection=False)
    a.tls_clienthello(data)
    assert data.ignore_connection is False  # intercepted, not tunnelled


def test_tls_clienthello_does_not_passthrough_denied_host(tmp_path):
    """A denied host must never be tunnelled untouched (ignore_connection stays False so MITM applies)."""
    a = EgressAddon(_write_cfg(tmp_path, rules=[{"host": "github.com"}]))
    data = types.SimpleNamespace(client_hello=types.SimpleNamespace(sni="evil.example"), ignore_connection=False)
    a.tls_clienthello(data)
    assert data.ignore_connection is False
