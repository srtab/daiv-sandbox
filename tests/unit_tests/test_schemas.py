import base64

import pytest


def test_put_mutation_validates_mode_range():
    from daiv_sandbox.schemas import PutMutation

    # Valid modes accepted.
    PutMutation(path="/repo/foo.py", content=base64.b64encode(b"x"), mode=0o644)
    PutMutation(path="/repo/script.sh", content=base64.b64encode(b"#!/bin/sh"), mode=0o755)
    PutMutation(path="/repo/.gitkeep", content=base64.b64encode(b""), mode=0)

    # Out-of-range rejected.
    with pytest.raises(ValueError):
        PutMutation(path="/repo/foo.py", content=base64.b64encode(b"x"), mode=-1)
    with pytest.raises(ValueError):
        PutMutation(path="/repo/foo.py", content=base64.b64encode(b"x"), mode=0o10000)


def test_apply_mutations_request_size_limits():
    from daiv_sandbox.schemas import ApplyMutationsRequest, PutMutation

    one = PutMutation(path="/repo/a.py", content=base64.b64encode(b""), mode=0o644)
    # Empty list rejected by min_length=1.
    with pytest.raises(ValueError):
        ApplyMutationsRequest(mutations=[])
    # Up to 64 entries accepted; 65+ rejected by max_length=64.
    ApplyMutationsRequest(mutations=[one] * 64)
    with pytest.raises(ValueError):
        ApplyMutationsRequest(mutations=[one] * 65)


def test_run_request_no_longer_accepts_archive():
    from daiv_sandbox.schemas import RunRequest

    assert "archive" not in RunRequest.model_fields


def test_start_session_request_no_longer_accepts_ephemeral():
    from daiv_sandbox.schemas import StartSessionRequest

    assert "ephemeral" not in StartSessionRequest.model_fields
