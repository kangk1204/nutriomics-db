import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nutrigenomics.public_files import attach_public_files


def client(tmp_path):
    data = tmp_path / "science.csv"
    data.write_bytes(b"study,value\nS04,0\n")
    (tmp_path / "internal.secret").write_text("not a release file")
    manifest = {"files": [{"path": data.name, "bytes": data.stat().st_size,
                           "sha256": hashlib.sha256(data.read_bytes()).hexdigest()}]}
    app = FastAPI()
    attach_public_files(app, tmp_path, manifest, "public-test")
    return TestClient(app), data


def test_only_manifested_public_files_are_downloadable(tmp_path):
    api, path = client(tmp_path)
    index = api.get("/api/v1/public/files").json()
    assert index["release_id"] == "public-test"
    assert index["published"] is False
    assert len(index["items"]) == 1
    response = api.get(index["items"][0]["url"])
    assert response.content == path.read_bytes()
    assert response.headers["x-content-sha256"] == index["items"][0]["sha256"]
    assert api.get("/api/v1/public/files/internal.secret").status_code == 404
    assert api.get("/api/v1/public/files/%2e%2e/internal.secret").status_code == 404
    assert api.get("/api/v1/public/files/C:%5csecret").status_code == 404


def test_download_range_and_source_change(tmp_path):
    api, path = client(tmp_path)
    response = api.get("/api/v1/public/files/science.csv", headers={"Range": "bytes=0-4"})
    assert response.status_code == 206
    assert response.content == b"study"
    path.write_bytes(b"changed")
    response = api.get("/api/v1/public/files/science.csv")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "source_changed"


def test_file_listing_never_falls_back_to_internal_directory(tmp_path):
    app = FastAPI()
    attach_public_files(app, tmp_path, {}, "no-manifest")
    api = TestClient(app)
    assert api.get("/api/v1/public/files").json()["items"] == []
    assert api.get("/api/v1/public/files/anything").status_code == 404


def test_invalid_manifest_path_is_rejected_before_serving(tmp_path):
    with pytest.raises(ValueError, match="Invalid"):
        attach_public_files(FastAPI(), tmp_path, {"files": [{"path": "../secret"}]}, "public-test")


def test_equal_size_corruption_is_rejected_at_startup(tmp_path):
    path = tmp_path / "science.csv"
    path.write_bytes(b"original")
    manifest = {"files": [{"path": path.name, "bytes": 8,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}
    path.write_bytes(b"modified")
    with pytest.raises(ValueError, match="hash mismatch"):
        attach_public_files(FastAPI(), tmp_path, manifest, "public-test")
