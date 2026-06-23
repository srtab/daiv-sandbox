"""Egress policy evaluator and mtime-reloading config store.

This module is copied verbatim into the mitmproxy sidecar image, which runs Python 3.13 (the repo
targets 3.14). Keep it free of 3.14-only syntax — e.g. an unparenthesized `except A, B:` (PEP 758)
is a SyntaxError on 3.13 and would crash the addon at import (failing OPEN). See egress_proxy/Dockerfile.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("daiv_sandbox.egress")


@dataclass(frozen=True)
class Decision:
    allow: bool
    intercept: bool
    inject: tuple[str, str] | None  # (header_name, header_value) or None


@dataclass(frozen=True)
class _Rule:
    host: str
    methods: tuple[str, ...]
    inject: str | None


class PolicyEvaluator:
    """Pure evaluator over a parsed config dict ({"policy": {...}, "secrets": {...}}).

    Named distinctly from the ``EgressPolicy`` *wire schema* (daiv_sandbox.schemas): this is the
    runtime evaluator that lives inside the sidecar; that one is the operator-facing request model.
    """

    def __init__(self, default: str, intercept: str, rules: list[_Rule], secrets: dict[str, tuple[str, str]]):
        self._default = default
        self._intercept = intercept
        self._rules = rules
        self._secrets = secrets  # name -> (header, value)

    @classmethod
    def from_config(cls, data: dict) -> PolicyEvaluator:
        policy = data.get("policy", {})
        secrets = {name: (s["header"], s["value"]) for name, s in (data.get("secrets") or {}).items()}
        rules = []
        for r in policy.get("rules", []):
            # Hostnames are case-insensitive (RFC 4343): normalise the rule host to lower-case so host
            # matching cannot be bypassed by varying the case of the requested host (see _match/evaluate).
            methods = tuple(m.upper() for m in r.get("methods", ["*"]))
            # Re-enforce the wire schema's invariant (EgressRule._upper) independently: an empty methods
            # list yields a host reachable via CONNECT but blocking every request. The config file is the
            # trust boundary the sidecar actually reads (PolicyStore reloads it on mtime change), so the
            # parser must fail closed on its own — raising here makes PolicyStore collapse to deny-all.
            if not methods:
                raise ValueError(f"egress: rule for host {r.get('host')!r} has empty methods; use ['*'] for any")
            rules.append(_Rule(host=r["host"].lower(), methods=methods, inject=r.get("inject")))
        # Unknown default/intercept values are a misconfiguration: fail closed (deny / full MITM) and warn
        # rather than relying on `== "allow"`/`== "all"` to accidentally do the safe thing for a typo.
        default = policy.get("default", "deny")
        if default not in ("deny", "allow"):
            logger.warning("egress: unknown policy default %r; failing closed to 'deny'", default)
            default = "deny"
        intercept = policy.get("intercept", "all")
        if intercept not in ("all", "credentialed"):
            logger.warning("egress: unknown intercept mode %r; failing closed to 'all'", intercept)
            intercept = "all"
        return cls(default, intercept, rules, secrets)

    def _match(self, host: str, method: str) -> tuple[_Rule | None, bool]:
        """Return (rule allowing host+method, whether any rule's host matched at all).

        `host` is already lower-cased and `method` already upper-cased by evaluate(); rule hosts are
        lower-cased in from_config, so fnmatchcase (which skips the redundant os.path.normcase that
        fnmatch.fnmatch does on both args) is the case-insensitive match. The host_listed flag lets
        evaluate() distinguish 'host not listed' from 'host listed but this method isn't permitted'
        in a single pass over the rules.
        """
        host_listed = False
        for rule in self._rules:
            if fnmatch.fnmatchcase(host, rule.host):
                host_listed = True
                # CONNECT is a host-reachability check: the TLS tunnel hasn't been established yet, so
                # the real HTTP method is unknown. Match any host-listed rule; the specific method
                # restriction is enforced later at the request (post-interception) phase.
                if method == "CONNECT" or "*" in rule.methods or method in rule.methods:
                    return rule, True
        return None, host_listed

    def evaluate(self, host: str, method: str) -> Decision:
        host = host.lower()  # case-insensitive host matching; rule hosts are lower-cased in from_config
        method = method.upper()
        rule, host_listed = self._match(host, method)
        if rule is not None:
            inject = self._secrets.get(rule.inject) if rule.inject else None
            method_restricted = "*" not in rule.methods
            intercept = self._intercept == "all" or inject is not None or method_restricted
            return Decision(allow=True, intercept=intercept, inject=inject)
        # No rule allows host+method. If the host IS listed in a rule but this (non-CONNECT) method
        # isn't permitted, the operator constrained that host -> deny regardless of `default`
        # (otherwise a per-host method limit would silently no-op under default="allow"). CONNECT
        # stays a reachability-only check, so the TLS tunnel still opens and the method is enforced
        # at the request phase.
        if method != "CONNECT" and host_listed:
            return Decision(allow=False, intercept=False, inject=None)
        if self._default == "allow":
            return Decision(allow=True, intercept=self._intercept == "all", inject=None)
        return Decision(allow=False, intercept=False, inject=None)


_DENY_ALL = PolicyEvaluator("deny", "all", [], {})


class PolicyStore:
    """Loads the config JSON, caching the parsed PolicyEvaluator and reloading on mtime change.

    A missing/unreadable/invalid file yields a deny-all policy — fail closed.
    """

    def __init__(self, path: str):
        self._path = path
        self._mtime: float | None = None
        self._policy = _DENY_ALL

    def current(self) -> PolicyEvaluator:
        try:
            mtime = os.path.getmtime(self._path)  # noqa: PTH204
        except OSError:
            self._mtime, self._policy = None, _DENY_ALL
            return self._policy
        if mtime != self._mtime:
            try:
                with open(self._path, encoding="utf-8") as fh:  # noqa: PTH123
                    self._policy = PolicyEvaluator.from_config(json.load(fh))
            except Exception:
                # Catch broadly ON PURPOSE: this evaluator backs a security boundary, and the addon
                # hooks that call it run inside mitmproxy, which FAILS OPEN on an unhandled hook
                # exception (it logs and lets the flow through). Any parse/structure error here
                # (OSError, JSON ValueError, KeyError, or a TypeError/AttributeError from a
                # structurally-bad config) must therefore collapse to deny-all, never propagate.
                logger.exception("egress: failed to load policy from %s; failing closed (deny-all)", self._path)
                self._policy = _DENY_ALL
            self._mtime = mtime
        return self._policy
