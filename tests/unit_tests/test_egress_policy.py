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
