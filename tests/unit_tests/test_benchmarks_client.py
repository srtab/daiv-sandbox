import base64

import httpx
import pytest

from benchmarks.client import BenchClient
from daiv_sandbox.schemas import FsWriteRequest


def _client(handler, *, root_path=""):
    return BenchClient("http://sandbox.test", "secret-key", root_path=root_path, transport=httpx.MockTransport(handler))


def test_version_parses_body():
    def handler(request):
        assert request.url.path == "/-/version/"
        assert request.headers["X-API-Key"] == "secret-key"
        return httpx.Response(200, json={"version": "9.9.9"})

    with _client(handler) as client:
        assert client.version() == "9.9.9"


def test_create_session_posts_payload_and_returns_id():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/session/"
        assert b"base_image" in request.content
        return httpx.Response(200, json={"session_id": "sid-123"})

    with _client(handler) as client:
        assert client.create_session("python:3.14-slim") == "sid-123"


def test_fs_write_serializes_base64_content():
    def handler(request):
        assert request.url.path == "/session/sid-123/fs/write"
        body = request.content.decode()
        assert base64.b64encode(b"hello").decode() in body
        return httpx.Response(200, json={"error": None, "ok": True})

    with _client(handler) as client:
        req = FsWriteRequest(path="/workspace/tmp/a.bin", content=base64.b64encode(b"hello"))
        assert client.fs("sid-123", "write", req) == {"error": None, "ok": True}


def test_root_path_prefixes_requests():
    def handler(request):
        assert request.url.path == "/api/v1/-/version/"
        return httpx.Response(200, json={"version": "1.0.0"})

    with _client(handler, root_path="/api/v1") as client:
        assert client.version() == "1.0.0"


def test_seed_posts_multipart():
    def handler(request):
        assert request.url.path == "/session/sid-9/seed/"
        assert b"repo_archive" in request.content
        return httpx.Response(204)

    with _client(handler) as client:
        client.seed("sid-9", b"\x1f\x8btarball-bytes")


def test_delete_session_tolerates_404():
    def handler(request):
        assert request.method == "DELETE"
        assert request.url.path == "/session/gone/"
        assert request.url.params["force"] == "true"
        return httpx.Response(404, json={"detail": "not found"})

    with _client(handler) as client:
        client.delete_session("gone")  # 404 must NOT raise


def test_delete_session_raises_on_server_error():
    def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
        client.delete_session("sid-err")
