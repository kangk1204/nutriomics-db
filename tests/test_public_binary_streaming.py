"""Public binary downloads must not be collected by the JSON query middleware."""
import asyncio
import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nutrigenomics.explorer_performance import CACHE, CURRENT, ExplorerReadMiddleware
from nutrigenomics.public_files import attach_public_files
from nutrigenomics.source_replicas import ReplicaGuardMiddleware


def test_real_binary_range_and_manifest_guards_through_full_middleware(tmp_path):
    content = bytes(range(256)) * 1025
    path = tmp_path / "public.sqlite3"
    path.write_bytes(content)
    app = FastAPI()
    attach_public_files(app, tmp_path, {"files": [{"path": path.name, "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest()}]}, "public-binary")
    app.add_middleware(ExplorerReadMiddleware, include_public=True)
    app.add_middleware(ReplicaGuardMiddleware)
    CACHE.clear()
    with TestClient(app) as client:
        listing = client.get("/api/v1/public/files")
        assert listing.headers["x-explorer-cache"] == "miss"
        assert client.get("/api/v1/public/files").headers["x-explorer-cache"] == "hit"
        response = client.get("/api/v1/public/files/public.sqlite3")
        assert response.status_code == 200 and response.content == content
        assert "x-explorer-cache" not in response.headers
        assert response.headers["x-content-sha256"] == hashlib.sha256(content).hexdigest()
        response = client.get("/api/v1/public/files/public.sqlite3", headers={"Range": "bytes=125-785"})
        assert response.status_code == 206 and response.content == content[125:786]
        assert response.headers["content-range"] == f"bytes 125-785/{len(content)}"
        assert client.get("/api/v1/public/files/private.sqlite3").status_code == 404
        assert client.get("/api/v1/public/files/%2e%2e/private.sqlite3").status_code == 404
        path.write_bytes(b"changed")
        changed = client.get("/api/v1/public/files/public.sqlite3")
        assert changed.status_code == 503
        assert changed.json()["detail"]["code"] == "source_changed"


def test_binary_chunks_reach_send_before_stream_finishes_and_without_query_budget():
    delivered = []

    async def send(message):
        delivered.append(message)

    async def receive():
        raise AssertionError("Download GET must not be consumed as a JSON query request")

    async def app(scope, receive, send):
        assert CURRENT.get() is None
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"\xff\x00", "more_body": True})
        assert len(delivered) == 2, "A large file was buffered instead of streaming"
        await send({"type": "http.response.body", "body": b"\x80", "more_body": False})

    middleware = ReplicaGuardMiddleware(ExplorerReadMiddleware(app, include_public=True))
    asyncio.run(middleware({"type": "http", "method": "GET", "path": "/api/v1/public/files/data.parquet"}, receive, send))
    assert b"".join(m.get("body", b"") for m in delivered) == b"\xff\x00\x80"


def test_binary_bypass_keeps_source_guard_before_200_headers(monkeypatch):
    from nutrigenomics import source_replicas
    from nutrigenomics.source_replicas import ReplicaUnavailable

    async def app(scope, receive, send):
        raise AssertionError("Changed-source guard must stop the download")

    def changed():
        raise ReplicaUnavailable("changed original")

    monkeypatch.setattr(source_replicas, "guard", changed)
    response = TestClient(ReplicaGuardMiddleware(ExplorerReadMiddleware(app, include_public=True))).get(
        "/api/v1/public/files/data.sqlite3")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "source_changed"
