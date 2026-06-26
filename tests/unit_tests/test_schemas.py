import pytest
from pydantic import ValidationError

from daiv_sandbox.schemas import (
    EgressConfigRequest,
    EgressPolicy,
    EgressRule,
    FsDeleteResponse,
    FsError,
    FsErrorCode,
    FsWriteResponse,
    RunRequest,
    StartSessionRequest,
)


def test_run_request_no_longer_accepts_archive():
    assert "archive" not in RunRequest.model_fields


def test_start_session_request_no_longer_accepts_ephemeral():
    assert "ephemeral" not in StartSessionRequest.model_fields


def test_fs_error_rejects_empty_message():
    """FsError.message is the agent-actionable hint and must be non-empty."""
    with pytest.raises(ValidationError):
        FsError(code=FsErrorCode.NOT_FOUND, message="")


def test_fs_write_response_ok_is_derived_from_error():
    """`ok` is a computed field: success ⇔ no error, so the two can never disagree."""
    assert FsWriteResponse().ok is True
    assert FsWriteResponse(error=FsError(code=FsErrorCode.EXEC_FAILED, message="boom")).ok is False


def test_fs_delete_response_rejects_removed_with_error():
    """A failed delete cannot also report removed=True — the model_validator forbids the contradiction."""
    with pytest.raises(ValidationError):
        FsDeleteResponse(removed=True, error=FsError(code=FsErrorCode.IS_A_DIRECTORY, message="is a directory"))

    # The valid combinations still construct fine.
    assert FsDeleteResponse(removed=True).ok is True
    assert FsDeleteResponse(error=FsError(code=FsErrorCode.NOT_FOUND, message="nope")).removed is False


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
