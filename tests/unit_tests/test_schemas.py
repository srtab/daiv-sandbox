def test_run_request_no_longer_accepts_archive():
    from daiv_sandbox.schemas import RunRequest

    assert "archive" not in RunRequest.model_fields


def test_start_session_request_no_longer_accepts_ephemeral():
    from daiv_sandbox.schemas import StartSessionRequest

    assert "ephemeral" not in StartSessionRequest.model_fields
