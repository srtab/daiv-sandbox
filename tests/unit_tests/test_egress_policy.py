import json

import pytest

from daiv_sandbox.egress.policy import PolicyEvaluator, PolicyStore


def _cfg(default="deny", intercept="all", rules=None, secrets=None):
    return {"policy": {"default": default, "intercept": intercept, "rules": rules or []}, "secrets": secrets or {}}


def test_default_deny_blocks_unlisted_host():
    p = PolicyEvaluator.from_config(_cfg())
    d = p.evaluate("evil.example", "GET")
    assert d.allow is False


def test_allow_lists_host_and_injects_header():
    p = PolicyEvaluator.from_config(
        _cfg(
            rules=[{"host": "github.com", "methods": ["*"], "inject": "gh"}],
            secrets={"gh": {"header": "Authorization", "value": "Bearer t"}},
        )
    )
    d = p.evaluate("github.com", "POST")
    assert d.allow is True and d.intercept is True
    assert d.inject == ("Authorization", "Bearer t")


def test_host_glob_matches_subdomains():
    p = PolicyEvaluator.from_config(_cfg(rules=[{"host": "*.githubusercontent.com", "methods": ["GET"]}]))
    assert p.evaluate("raw.githubusercontent.com", "GET").allow is True
    assert p.evaluate("raw.githubusercontent.com", "POST").allow is False  # method not allowed


def test_default_allow_with_credentialed_intercept_passthroughs_noncred():
    p = PolicyEvaluator.from_config(
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
    p = PolicyEvaluator.from_config(_cfg(rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    connect = p.evaluate("api.github.com", "CONNECT")
    assert connect.allow is True
    assert connect.intercept is True  # method-restricted ⇒ must MITM
    assert p.evaluate("api.github.com", "GET").allow is True
    assert p.evaluate("api.github.com", "POST").allow is False  # enforced at request phase


def test_method_restricted_rule_forces_interception_in_credentialed_mode():
    """In credentialed (non-all) intercept mode a method-restricted rule still forces MITM —
    without it the TLS tunnel is opaque and the method restriction is a fail-open passthrough."""
    p = PolicyEvaluator.from_config(
        _cfg(intercept="credentialed", rules=[{"host": "api.github.com", "methods": ["GET"]}])
    )
    connect = p.evaluate("api.github.com", "CONNECT")
    assert connect.allow is True
    assert connect.intercept is True  # forced because method-restricted, even without inject


def test_wildcard_methods_rule_respects_credentialed_passthrough():
    """A wildcard-methods rule with no inject key should NOT force interception in credentialed
    mode — confirming we did not over-force interception for unrestricted hosts."""
    p = PolicyEvaluator.from_config(_cfg(intercept="credentialed", rules=[{"host": "pypi.org", "methods": ["*"]}]))
    connect = p.evaluate("pypi.org", "CONNECT")
    assert connect.allow is True
    assert connect.intercept is False  # no restriction, no inject ⇒ passthrough


def test_connect_to_unlisted_host_still_denied():
    """CONNECT reachability is gated by the host allowlist — unlisted hosts must still be denied."""
    p = PolicyEvaluator.from_config(_cfg(rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    assert p.evaluate("evil.example", "CONNECT").allow is False


def test_default_allow_still_enforces_method_limit_on_listed_host():
    """Under default="allow", a host listed with method restrictions must still deny disallowed methods.

    Without this fix a POST to api.github.com (methods:["GET"]) would miss _match and fall through
    to the default-allow branch — silently granting access. CONNECT stays reachability-only so the
    TLS tunnel can open; the method is enforced at the request (post-interception) phase.
    """
    p = PolicyEvaluator.from_config(_cfg(default="allow", rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    # Allowed method on listed host
    assert p.evaluate("api.github.com", "GET").allow is True
    # Disallowed method on listed host — must deny despite default="allow"
    assert p.evaluate("api.github.com", "POST").allow is False
    # CONNECT is still allowed (reachability-only check)
    assert p.evaluate("api.github.com", "CONNECT").allow is True
    # Host with NO rule at all still follows default-allow
    assert p.evaluate("unlisted.example", "POST").allow is True


def test_host_matching_is_case_insensitive_under_default_allow():
    """Hostnames are case-insensitive (DNS/RFC 4343). Untrusted sandbox code controls the requested
    host, so an uppercased host must NOT slip past a per-host method limit under default="allow"."""
    p = PolicyEvaluator.from_config(_cfg(default="allow", rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    # Uppercased host still matches the rule (allowed method)
    assert p.evaluate("API.GITHUB.COM", "GET").allow is True
    # ...and the method limit is still enforced — no bypass via case
    assert p.evaluate("API.GITHUB.COM", "POST").allow is False
    assert p.evaluate("Api.GitHub.Com", "POST").allow is False


def test_host_matching_normalizes_rule_case():
    """A rule whose host is written with mixed case still matches a lowercase request host."""
    p = PolicyEvaluator.from_config(_cfg(rules=[{"host": "API.GitHub.Com", "methods": ["GET"]}]))
    assert p.evaluate("api.github.com", "GET").allow is True


def test_host_glob_is_case_insensitive():
    """Glob rules match regardless of host case (default-deny: an uppercase host must still be allowed
    by a matching wildcard, and an unlisted host still denied)."""
    p = PolicyEvaluator.from_config(_cfg(rules=[{"host": "*.githubusercontent.com", "methods": ["GET"]}]))
    assert p.evaluate("RAW.GithubUserContent.com", "GET").allow is True


def test_policy_store_malformed_json_is_deny_all(tmp_path):
    """A present-but-corrupt config (invalid JSON) must fail closed to deny-all, not crash/fail open."""
    path = tmp_path / "config.json"
    path.write_text("{ not valid json")
    store = PolicyStore(str(path))
    assert store.current().evaluate("github.com", "GET").allow is False


def test_policy_store_structurally_bad_config_is_deny_all(tmp_path):
    """A structurally-bad config (e.g. `secrets` is a list, not a dict) makes from_config raise a
    TypeError/AttributeError. That must still fail closed — if it escaped into the mitmproxy hook the
    flow would be allowed through (mitmproxy fails open on hook exceptions)."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"policy": {"default": "deny", "rules": []}, "secrets": ["not", "a", "dict"]}))
    store = PolicyStore(str(path))
    assert store.current().evaluate("github.com", "GET").allow is False


def test_policy_store_reverts_to_deny_all_when_good_config_replaced_with_garbage(tmp_path):
    """A config that loaded cleanly once and is then replaced with garbage must revert to deny-all,
    not keep serving the previous allow policy."""
    import os
    import time

    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg(default="allow")))
    store = PolicyStore(str(path))
    assert store.current().evaluate("github.com", "GET").allow is True
    path.write_text("garbage{")
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert store.current().evaluate("github.com", "GET").allow is False


def test_unknown_default_value_fails_closed_to_deny():
    """A typo'd `default` (e.g. "allowed") must not be treated as allow — it must fail closed."""
    p = PolicyEvaluator.from_config(_cfg(default="allowed"))  # typo for "allow"
    assert p.evaluate("unlisted.example", "GET").allow is False


def test_unknown_intercept_value_fails_closed_to_full_interception():
    """A typo'd `intercept` mode must not silently downgrade to passthrough; fail closed to full MITM."""
    p = PolicyEvaluator.from_config(_cfg(default="allow", intercept="credentialled"))  # typo for "credentialed"
    d = p.evaluate("pypi.org", "GET")
    assert d.allow is True
    assert d.intercept is True  # coerced to "all" rather than silently passing through un-inspected


def test_from_config_rejects_empty_methods():
    """The sidecar parser must re-enforce the wire schema's "methods not empty" invariant independently:
    an empty list yields a host reachable via CONNECT but blocking every request. from_config raises so
    PolicyStore collapses the whole config to deny-all (fail closed) rather than serving the footgun."""
    with pytest.raises(ValueError, match="methods"):
        PolicyEvaluator.from_config(_cfg(rules=[{"host": "api.github.com", "methods": []}]))


def test_policy_store_empty_methods_config_is_deny_all(tmp_path):
    """A config with an empty methods list can only reach the sidecar if it bypassed wire validation
    (hand-edited / corrupt file). It must fail closed to deny-all — including denying CONNECT, so the
    host isn't even reachable — not leave a tunnel that 403s every request."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg(default="allow", rules=[{"host": "api.github.com", "methods": []}])))
    store = PolicyStore(str(path))
    assert store.current().evaluate("api.github.com", "CONNECT").allow is False
    assert store.current().evaluate("unlisted.example", "GET").allow is False  # default-allow also gone
