import pytest
from fastapi.testclient import TestClient

from daiv_sandbox.config import settings
from daiv_sandbox.main import app


@pytest.fixture
def client():
    return TestClient(app, headers={"X-API-Key": settings.API_KEY.get_secret_value()}, root_path=settings.API_V1_STR)


@pytest.fixture
def sandbox_session(client: TestClient):
    """
    Create sandbox sessions and always delete them.

    Use as a factory:

        session_id = sandbox_session(base_image="alpine:latest")
    """

    created_session_ids: list[str] = []

    def _create(**payload) -> str:
        resp = client.post("/session/", json=payload)
        assert resp.status_code == 200, resp.text
        session_id = resp.json()["session_id"]
        created_session_ids.append(session_id)
        return session_id

    try:
        yield _create
    finally:
        for session_id in created_session_ids:
            client.delete(f"/session/{session_id}/")
