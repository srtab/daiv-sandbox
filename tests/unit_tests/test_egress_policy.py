import json

from daiv_sandbox.egress.policy import EgressPolicy, PolicyStore


def _cfg(default="deny", intercept="all", rules=None, secrets=None):
    return {"policy": {"default": default, "intercept": intercept, "rules": rules or []}, "secrets": secrets or {}}


def test_default_deny_blocks_unlisted_host():
    p = EgressPolicy.from_config(_cfg())
    d = p.evaluate("evil.example", "GET")
    assert d.allow is False


def test_allow_lists_host_and_injects_header():
    p = EgressPolicy.from_config(
        _cfg(
            rules=[{"host": "github.com", "methods": ["*"], "inject": "gh"}],
            secrets={"gh": {"header": "Authorization", "value": "Bearer t"}},
        )
    )
    d = p.evaluate("github.com", "POST")
    assert d.allow is True and d.intercept is True
    assert d.inject == ("Authorization", "Bearer t")


def test_host_glob_matches_subdomains():
    p = EgressPolicy.from_config(_cfg(rules=[{"host": "*.githubusercontent.com", "methods": ["GET"]}]))
    assert p.evaluate("raw.githubusercontent.com", "GET").allow is True
    assert p.evaluate("raw.githubusercontent.com", "POST").allow is False  # method not allowed


def test_default_allow_with_credentialed_intercept_passthroughs_noncred():
    p = EgressPolicy.from_config(
        _cfg(
            default="allow",
            intercept="credentialed",
            rules=[{"host": "github.com", "inject": "gh"}],
            secrets={"gh": {"header": "Authorization", "value": "x"}},
        )
    )
    cred = p.evaluate("github.com", "GET")
    assert cred.allow is True and cred.intercept is True
    other = p.evaluate("pypi.org", "GET")
    assert other.allow is True and other.intercept is False  # allowed but NOT intercepted


def test_policy_store_reloads_on_mtime_change(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg()))
    store = PolicyStore(str(path))
    assert store.current().evaluate("github.com", "GET").allow is False
    path.write_text(json.dumps(_cfg(rules=[{"host": "github.com"}])))
    # bump mtime explicitly so the change is observable even on coarse-grained clocks
    import os
    import time

    os.utime(path, (time.time() + 1, time.time() + 1))
    assert store.current().evaluate("github.com", "GET").allow is True


def test_policy_store_missing_file_is_deny_all(tmp_path):
    store = PolicyStore(str(tmp_path / "nope.json"))
    assert store.current().evaluate("github.com", "GET").allow is False


def test_connect_reaches_method_restricted_host_and_intercepts():
    """CONNECT (HTTPS tunnel) must be allowed for a host-listed rule and must force interception
    so the real HTTP method can be inspected post-TLS; POST must still be denied at request phase."""
    p = EgressPolicy.from_config(_cfg(rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    connect = p.evaluate("api.github.com", "CONNECT")
    assert connect.allow is True
    assert connect.intercept is True  # method-restricted ⇒ must MITM
    assert p.evaluate("api.github.com", "GET").allow is True
    assert p.evaluate("api.github.com", "POST").allow is False  # enforced at request phase


def test_method_restricted_rule_forces_interception_in_credentialed_mode():
    """In credentialed (non-all) intercept mode a method-restricted rule still forces MITM —
    without it the TLS tunnel is opaque and the method restriction is a fail-open passthrough."""
    p = EgressPolicy.from_config(_cfg(intercept="credentialed", rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    connect = p.evaluate("api.github.com", "CONNECT")
    assert connect.allow is True
    assert connect.intercept is True  # forced because method-restricted, even without inject


def test_wildcard_methods_rule_respects_credentialed_passthrough():
    """A wildcard-methods rule with no inject key should NOT force interception in credentialed
    mode — confirming we did not over-force interception for unrestricted hosts."""
    p = EgressPolicy.from_config(_cfg(intercept="credentialed", rules=[{"host": "pypi.org", "methods": ["*"]}]))
    connect = p.evaluate("pypi.org", "CONNECT")
    assert connect.allow is True
    assert connect.intercept is False  # no restriction, no inject ⇒ passthrough


def test_connect_to_unlisted_host_still_denied():
    """CONNECT reachability is gated by the host allowlist — unlisted hosts must still be denied."""
    p = EgressPolicy.from_config(_cfg(rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    assert p.evaluate("evil.example", "CONNECT").allow is False
