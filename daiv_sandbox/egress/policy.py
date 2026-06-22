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


class EgressPolicy:
    """Pure evaluator over a parsed config dict ({"policy": {...}, "secrets": {...}})."""

    def __init__(self, default: str, intercept: str, rules: list[_Rule], secrets: dict[str, tuple[str, str]]):
        self._default = default
        self._intercept = intercept
        self._rules = rules
        self._secrets = secrets  # name -> (header, value)

    @classmethod
    def from_config(cls, data: dict) -> EgressPolicy:
        policy = data.get("policy", {})
        secrets = {name: (s["header"], s["value"]) for name, s in (data.get("secrets") or {}).items()}
        rules = [
            _Rule(host=r["host"], methods=tuple(m.upper() for m in r.get("methods", ["*"])), inject=r.get("inject"))
            for r in policy.get("rules", [])
        ]
        return cls(policy.get("default", "deny"), policy.get("intercept", "all"), rules, secrets)

    def _match(self, host: str, method: str) -> _Rule | None:
        for rule in self._rules:
            # CONNECT is a host-reachability check: the TLS tunnel hasn't been established yet,
            # so the real HTTP method is unknown. Match any host-listed rule; the specific method
            # restriction is enforced later at the request (post-interception) phase.
            if fnmatch.fnmatch(host, rule.host) and (
                method.upper() == "CONNECT" or "*" in rule.methods or method.upper() in rule.methods
            ):
                return rule
        return None

    def evaluate(self, host: str, method: str) -> Decision:
        rule = self._match(host, method)
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
        if method.upper() != "CONNECT" and any(fnmatch.fnmatch(host, r.host) for r in self._rules):
            return Decision(allow=False, intercept=False, inject=None)
        if self._default == "allow":
            return Decision(allow=True, intercept=self._intercept == "all", inject=None)
        return Decision(allow=False, intercept=False, inject=None)


_DENY_ALL = EgressPolicy("deny", "all", [], {})


class PolicyStore:
    """Loads the config JSON, caching the parsed EgressPolicy and reloading on mtime change.

    A missing/unreadable/invalid file yields a deny-all policy — fail closed.
    """

    def __init__(self, path: str):
        self._path = path
        self._mtime: float | None = None
        self._policy = _DENY_ALL

    def current(self) -> EgressPolicy:
        try:
            mtime = os.path.getmtime(self._path)  # noqa: PTH204
        except OSError:
            self._mtime, self._policy = None, _DENY_ALL
            return self._policy
        if mtime != self._mtime:
            # Parens around the exception tuple are REQUIRED, not stylistic: this module is copied
            # verbatim into the mitmproxy sidecar image, which runs Python 3.13 where the
            # unparenthesized `except A, B, C:` (PEP 758, 3.14+) is a hard SyntaxError that crashes the
            # addon at import. The repo targets 3.14, so `ruff format` would strip the parens — the
            # `# fmt: off/on` guard keeps them so the source stays valid on 3.13.
            # fmt: off
            try:
                with open(self._path, encoding="utf-8") as fh:  # noqa: PTH123
                    self._policy = EgressPolicy.from_config(json.load(fh))
            except (OSError, ValueError, KeyError):
                logger.exception("egress: failed to load policy from %s; failing closed (deny-all)", self._path)
                self._policy = _DENY_ALL
            # fmt: on
            self._mtime = mtime
        return self._policy
