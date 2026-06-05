def test_run_request_no_longer_accepts_archive():
    from daiv_sandbox.schemas import RunRequest

    assert "archive" not in RunRequest.model_fields


def test_start_session_request_no_longer_accepts_ephemeral():
    from daiv_sandbox.schemas import StartSessionRequest

    assert "ephemeral" not in StartSessionRequest.model_fields


def test_fs_error_rejects_empty_message():
    """FsError.message is the agent-actionable hint and must be non-empty."""
    import pytest
    from pydantic import ValidationError

    from daiv_sandbox.schemas import FsError, FsErrorCode

    with pytest.raises(ValidationError):
        FsError(code=FsErrorCode.NOT_FOUND, message="")


def test_fs_write_response_ok_is_derived_from_error():
    """`ok` is a computed field: success ⇔ no error, so the two can never disagree."""
    from daiv_sandbox.schemas import FsError, FsErrorCode, FsWriteResponse

    assert FsWriteResponse().ok is True
    assert FsWriteResponse(error=FsError(code=FsErrorCode.EXEC_FAILED, message="boom")).ok is False


def test_fs_delete_response_rejects_removed_with_error():
    """A failed delete cannot also report removed=True — the model_validator forbids the contradiction."""
    import pytest
    from pydantic import ValidationError

    from daiv_sandbox.schemas import FsDeleteResponse, FsError, FsErrorCode

    with pytest.raises(ValidationError):
        FsDeleteResponse(removed=True, error=FsError(code=FsErrorCode.IS_A_DIRECTORY, message="is a directory"))

    # The valid combinations still construct fine.
    assert FsDeleteResponse(removed=True).ok is True
    assert FsDeleteResponse(error=FsError(code=FsErrorCode.NOT_FOUND, message="nope")).removed is False
