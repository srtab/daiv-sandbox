import pytest
from pydantic import ValidationError

from daiv_sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, StartSessionRequest


def test_start_session_request_has_no_network_enabled():
    req = StartSessionRequest(base_image="python:3.14")
    assert not hasattr(req, "network_enabled")
    assert req.egress is None


def test_start_session_request_accepts_egress_block():
    req = StartSessionRequest(
        base_image="python:3.14", egress={"policy": {"default": "deny", "rules": [{"host": "github.com"}]}}
    )
    assert isinstance(req.egress, EgressConfigRequest)
    assert req.egress.policy.rules[0].host == "github.com"


def test_egress_deny_default_with_no_rules_is_rejected():
    with pytest.raises(ValidationError, match="permits nothing"):
        EgressConfigRequest(policy=EgressPolicy(default="deny", rules=[]))


def test_egress_allow_default_with_no_rules_is_allowed():
    req = EgressConfigRequest(policy=EgressPolicy(default="allow", rules=[]))
    assert req.policy.default == "allow"


def test_egress_deny_default_with_a_rule_is_allowed():
    req = EgressConfigRequest(policy=EgressPolicy(default="deny", rules=[EgressRule(host="pypi.org")]))
    assert req.policy.rules[0].host == "pypi.org"
