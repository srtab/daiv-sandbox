import hashlib
import json
import logging
import os

import pytest

from daiv_sandbox.egress.policy import REASON_HOST_NOT_LISTED, REASON_METHOD_NOT_ALLOWED, PolicyEvaluator, PolicyStore


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


def test_from_config_rejects_dangling_inject():
    """The sidecar parser must re-enforce EgressConfigRequest._injects_resolve independently: a rule whose
    `inject` names a secret that isn't present would allow the host but inject nothing (evaluate's
    _secrets.get returns None silently). from_config raises so PolicyStore collapses to deny-all rather
    than letting a request meant to carry a credential go out unauthenticated."""
    with pytest.raises(ValueError, match="unknown secret"):
        PolicyEvaluator.from_config(_cfg(rules=[{"host": "github.com", "methods": ["*"], "inject": "ghx"}]))


def test_allowed_decision_carries_no_block_reason():
    """An allowed request has nothing to transpose into a rule — block must be None."""
    p = PolicyEvaluator.from_config(_cfg(rules=[{"host": "github.com", "methods": ["*"]}]))
    assert p.evaluate("github.com", "GET").block is None


def test_unlisted_host_block_reason_is_host_not_listed():
    """A host that matches no rule (caught by default=deny) reports host-not-listed with no rule detail."""
    p = PolicyEvaluator.from_config(_cfg())
    block = p.evaluate("evil.example", "GET").block
    assert block is not None
    assert block.code == REASON_HOST_NOT_LISTED
    assert block.host is None and block.methods is None


def test_connect_to_unlisted_host_block_reason_is_host_not_listed():
    """A blocked CONNECT to an unlisted host reports host-not-listed. (A CONNECT can never be
    method-not-allowed: evaluate() guards that branch with `method != "CONNECT"`, so once a host is listed
    CONNECT is reachability-only — that invariant lives in evaluate(), not this test.)"""
    p = PolicyEvaluator.from_config(_cfg(rules=[{"host": "api.github.com", "methods": ["GET"]}]))
    block = p.evaluate("evil.example", "CONNECT").block
    assert block is not None and block.code == REASON_HOST_NOT_LISTED


def test_method_mismatch_block_reason_carries_matched_rule():
    """A method blocked on a listed host reports method-not-allowed AND the matched rule's host glob
    and currently-allowed methods, so an operator can see whether to extend that rule."""
    p = PolicyEvaluator.from_config(_cfg(rules=[{"host": "api.github.com", "methods": ["GET", "POST"]}]))
    block = p.evaluate("api.github.com", "DELETE").block
    assert block is not None
    assert block.code == REASON_METHOD_NOT_ALLOWED
    assert block.host == "api.github.com"
    assert block.methods == ("GET", "POST")


def test_method_mismatch_block_reason_reports_glob_host():
    """The matched-rule detail echoes the rule's glob host (not the concrete requested host), since that
    is the value an operator would edit in the policy."""
    p = PolicyEvaluator.from_config(_cfg(rules=[{"host": "*.githubusercontent.com", "methods": ["GET"]}]))
    block = p.evaluate("raw.githubusercontent.com", "POST").block
    assert block is not None
    assert block.code == REASON_METHOD_NOT_ALLOWED
    assert block.host == "*.githubusercontent.com"
    assert block.methods == ("GET",)


def test_method_mismatch_reports_first_matching_rule_when_multiple_match():
    """When several rules match the same host and none permits the method, the block cites the FIRST
    host-matching rule's glob + methods (deterministic first-match in _match). Pins that operator-facing
    choice so a future _match refactor can't silently change which rule operators are told to edit."""
    p = PolicyEvaluator.from_config(
        _cfg(rules=[{"host": "*.github.com", "methods": ["GET"]}, {"host": "api.github.com", "methods": ["POST"]}])
    )
    block = p.evaluate("api.github.com", "DELETE").block
    assert block is not None
    assert block.code == REASON_METHOD_NOT_ALLOWED
    assert block.host == "*.github.com"  # the first matching rule, not the more specific second
    assert block.methods == ("GET",)


def test_policy_store_empty_methods_config_is_deny_all(tmp_path):
    """A config with an empty methods list can only reach the sidecar if it bypassed wire validation
    (hand-edited / corrupt file). It must fail closed to deny-all — including denying CONNECT, so the
    host isn't even reachable — not leave a tunnel that 403s every request."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg(default="allow", rules=[{"host": "api.github.com", "methods": []}])))
    store = PolicyStore(str(path))
    assert store.current().evaluate("api.github.com", "CONNECT").allow is False


def test_policy_store_dangling_inject_config_is_deny_all(tmp_path):
    """A config whose rule references a missing secret can only reach the sidecar if it bypassed wire
    validation (hand-edited / corrupt file). It must fail closed to deny-all — including denying CONNECT —
    rather than allowing the host without the intended credential."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg(rules=[{"host": "github.com", "methods": ["*"], "inject": "ghx"}])))
    store = PolicyStore(str(path))
    assert store.current().evaluate("github.com", "CONNECT").allow is False
    assert store.current().evaluate("unlisted.example", "GET").allow is False  # default-allow also gone


def _replace_preserving_mtime(path, payload):
    """Rewrite *path* the way ``provision`` does — stage a sibling, then rename over the target — but
    with the mtime pinned, so the reload is proven to hold even if the writer's stamp is ever dropped."""
    mtime_ns = path.stat().st_mtime_ns
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.utime(tmp, ns=(mtime_ns, mtime_ns))
    tmp.replace(path)


def test_policy_store_reloads_when_atomic_replace_preserves_mtime(tmp_path):
    """Keying the reload on the mtime alone is not safe: the writer's stamp has whole-second resolution,
    so two refreshes can share one and the sidecar would keep serving the first policy it parsed."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg()))
    store = PolicyStore(str(path))
    assert store.current().evaluate("github.com", "GET").allow is False

    _replace_preserving_mtime(path, _cfg(rules=[{"host": "github.com"}]))

    assert store.current().evaluate("github.com", "GET").allow is True


def test_policy_store_reloads_a_refreshed_secret_at_an_unchanged_mtime(tmp_path):
    """The exact production failure: only the injected credential's value changes. A stale reload
    keeps injecting the superseded token."""
    path = tmp_path / "config.json"
    rules = [{"host": "github.com", "methods": ["*"], "inject": "git"}]
    path.write_text(json.dumps(_cfg(rules=rules, secrets={"git": {"header": "Authorization", "value": "Basic old"}})))
    store = PolicyStore(str(path))
    assert store.current().evaluate("github.com", "GET").inject == ("Authorization", "Basic old")

    new_secrets = {"git": {"header": "Authorization", "value": "Basic new"}}
    _replace_preserving_mtime(path, _cfg(rules=rules, secrets=new_secrets))

    assert store.current().evaluate("github.com", "GET").inject == ("Authorization", "Basic new")


def test_policy_store_clears_cached_deny_all_when_garbage_is_replaced_at_an_unchanged_mtime(tmp_path):
    """A parse failure caches deny-all. Recovering must not require the clock to move, or one bad
    config wedges the proxy closed until the container restarts."""
    path = tmp_path / "config.json"
    path.write_text("{ not json")
    store = PolicyStore(str(path))
    assert store.current().evaluate("github.com", "GET").allow is False

    _replace_preserving_mtime(path, _cfg(rules=[{"host": "github.com"}]))

    assert store.current().evaluate("github.com", "GET").allow is True


def test_policy_store_logs_effective_policy_on_reload(tmp_path, caplog):
    """Every reload announces the policy it installed, so a stale sidecar is greppable."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            _cfg(
                rules=[{"host": "github.com", "methods": ["*"], "inject": "git"}],
                secrets={"git": {"header": "Authorization", "value": "Basic t"}},
            )
        )
    )
    store = PolicyStore(str(path))
    with caplog.at_level(logging.INFO, logger="daiv_sandbox.egress"):
        store.current()
    assert "policy reloaded" in caplog.text
    assert "rules=1" in caplog.text and "secrets=1" in caplog.text
    assert "Basic t" not in caplog.text  # the secret value must never reach the log


def test_policy_store_does_not_relog_when_config_is_unchanged(tmp_path, caplog):
    """The reload log runs on the per-request hot path; it must fire on change only, never per request."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg()))
    store = PolicyStore(str(path))
    store.current()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="daiv_sandbox.egress"):
        store.current()
        store.current()
    assert "policy reloaded" not in caplog.text


def test_stat_stamp_keys_on_ctime_as_well(tmp_path):
    """ctime backstops the other three, which can all repeat: ext4 recycles the inode a rename frees,
    the mtime is writer-supplied, and a re-minted credential keeps the size. Pinned white-box because
    ctime moves on its own — nothing a test can do to the file distinguishes it behaviourally."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg()))
    st = path.stat()

    assert PolicyStore(str(path))._stat_stamp() == (st.st_ino, st.st_mtime_ns, st.st_size, st.st_ctime_ns)


def test_policy_store_drops_a_loaded_policy_when_the_config_disappears(tmp_path):
    """A config that vanishes mid-session must fail closed. Keeping the last good policy would go on
    injecting its credential into a session the server believes has no egress config at all."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg(rules=[{"host": "github.com"}])))
    store = PolicyStore(str(path))
    assert store.current().evaluate("github.com", "GET").allow is True

    path.unlink()
    assert store.current().evaluate("github.com", "GET").allow is False

    path.write_text(json.dumps(_cfg(rules=[{"host": "github.com"}])))
    assert store.current().evaluate("github.com", "GET").allow is True


def test_policy_store_does_not_log_a_reload_when_the_config_fails_to_parse(tmp_path, caplog):
    """A failed parse installs deny-all, not the config. Announcing a reload there would invert the
    signal the reload line exists to give."""
    path = tmp_path / "config.json"
    path.write_text("{ not json")
    store = PolicyStore(str(path))
    with caplog.at_level(logging.INFO, logger="daiv_sandbox.egress"):
        store.current()
    assert "policy reloaded" not in caplog.text
    assert "failed to load policy" in caplog.text


def test_policy_store_logs_a_config_digest_that_matches_the_servers(tmp_path, caplog):
    """The digest is what pairs this line with the server's install line; without a shared id the two
    sides cannot be matched and 'did my refresh land?' stays unanswerable."""
    raw = json.dumps(_cfg()).encode()
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    store = PolicyStore(str(path))
    with caplog.at_level(logging.INFO, logger="daiv_sandbox.egress"):
        store.current()
    assert hashlib.sha256(raw).hexdigest()[:8] in caplog.text


def test_policy_store_warns_once_when_a_loaded_config_becomes_unreadable(tmp_path, caplog):
    """A config that disappears is otherwise indistinguishable from one that legitimately denies — the
    block log reads default=deny either way. Announce the transition, and only the transition."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_cfg(rules=[{"host": "github.com"}])))
    store = PolicyStore(str(path))
    store.current()
    path.unlink()

    with caplog.at_level(logging.WARNING, logger="daiv_sandbox.egress"):
        store.current()
        store.current()

    assert caplog.text.count("became unreadable") == 1


def test_policy_store_missing_from_the_start_does_not_warn(tmp_path, caplog):
    """A never-provisioned store is the normal pre-provision state, not a fault worth a warning."""
    store = PolicyStore(str(tmp_path / "config.json"))
    with caplog.at_level(logging.WARNING, logger="daiv_sandbox.egress"):
        store.current()
    assert caplog.text == ""
