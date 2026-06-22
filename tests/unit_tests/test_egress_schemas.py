import pytest
from pydantic import ValidationError

from daiv_sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule


def test_methods_uppercased_and_default_star():
    rule = EgressRule(host="github.com")
    assert rule.methods == ["*"]
    assert EgressRule(host="x", methods=["get", "Head"]).methods == ["GET", "HEAD"]


def test_policy_defaults_are_deny_and_intercept_all():
    p = EgressPolicy()
    assert p.default == "deny"
    assert p.intercept == "all"
    assert p.rules == []


def test_inject_must_reference_a_known_secret():
    with pytest.raises(ValidationError, match="unknown secret"):
        EgressConfigRequest(policy=EgressPolicy(rules=[EgressRule(host="github.com", inject="ghx")]), secrets={})


def test_valid_request_with_secret_round_trips():
    req = EgressConfigRequest(
        policy=EgressPolicy(rules=[EgressRule(host="github.com", inject="gh")]),
        secrets={"gh": {"header": "Authorization", "value": "Bearer t"}},
    )
    # SecretStr keeps the value out of repr/logs.
    assert "Bearer t" not in repr(req)
    assert req.secrets["gh"].value.get_secret_value() == "Bearer t"
