import json
import types

import pytest

from daiv_sandbox.egress import addon as addon_mod
from daiv_sandbox.egress.addon import EgressAddon


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
