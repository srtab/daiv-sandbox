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
        secrets = {
            name: (s["header"], s["value"]) for name, s in (data.get("secrets") or {}).items()
        }
        rules = [
            _Rule(
                host=r["host"],
                methods=tuple(m.upper() for m in r.get("methods", ["*"])),
                inject=r.get("inject"),
            )
            for r in policy.get("rules", [])
        ]
        return cls(policy.get("default", "deny"), policy.get("intercept", "all"), rules, secrets)

    def _match(self, host: str, method: str) -> _Rule | None:
        for rule in self._rules:
            if fnmatch.fnmatch(host, rule.host) and ("*" in rule.methods or method.upper() in rule.methods):
                return rule
        return None

    def evaluate(self, host: str, method: str) -> Decision:
        rule = self._match(host, method)
        if rule is not None:
            inject = self._secrets.get(rule.inject) if rule.inject else None
            intercept = self._intercept == "all" or inject is not None
            return Decision(allow=True, intercept=intercept, inject=inject)
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
            try:
                with open(self._path, encoding="utf-8") as fh:  # noqa: PTH123
                    self._policy = EgressPolicy.from_config(json.load(fh))
            except (OSError, ValueError, KeyError):
                logger.exception("egress: failed to load policy from %s; failing closed (deny-all)", self._path)
                self._policy = _DENY_ALL
            self._mtime = mtime
        return self._policy
