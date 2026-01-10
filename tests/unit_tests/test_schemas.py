from daiv_sandbox.schemas import generate_session_id


def test_generate_session_id():
    assert isinstance(generate_session_id(), str)
    assert len(generate_session_id()) == 36
