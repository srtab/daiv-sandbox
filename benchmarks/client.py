from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from types import TracebackType

    from pydantic import BaseModel


class BenchClient:
    """Thin HTTP wrapper over the daiv-sandbox endpoints under benchmark.

    Routes are served at the app root when hitting the service directly (e.g. `make run`);
    pass root_path="/api/v1" only when targeting a deployment behind a prefix-stripping proxy.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        root_path: str = "",
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + root_path,
            headers={"X-API-Key": api_key},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BenchClient:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def version(self) -> str:
        resp = self._client.get("/-/version/")
        resp.raise_for_status()
        return resp.json()["version"]

    def create_session(self, base_image: str) -> str:
        resp = self._client.post("/session/", json={"base_image": base_image})
        resp.raise_for_status()
        return resp.json()["session_id"]

    def seed(self, session_id: str, archive_bytes: bytes) -> None:
        files = {"repo_archive": ("repo.tar.gz", archive_bytes, "application/gzip")}
        resp = self._client.post(f"/session/{session_id}/seed/", files=files)
        resp.raise_for_status()

    def fs(self, session_id: str, op: str, request: BaseModel) -> dict:
        resp = self._client.post(f"/session/{session_id}/fs/{op}", json=request.model_dump(mode="json"))
        resp.raise_for_status()
        return resp.json()

    def delete_session(self, session_id: str, *, force: bool = True) -> None:
        resp = self._client.delete(f"/session/{session_id}/", params={"force": force})
        if resp.status_code not in (200, 204, 404):  # 204 = success, 404 = already gone, 200 = proxy-rewrapped
            resp.raise_for_status()
