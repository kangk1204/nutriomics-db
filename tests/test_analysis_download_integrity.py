"""Synthetic files/SQLite only: artifact provenance, never measured science."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nutrigenomics import api
from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import AnalysisRun, GeoSeries, ResearchStudy

CONTENT = b"sample\tqc\nSIM1\tpass\n"


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    """A fictional study and byte fixture; no original database is opened."""
    root = tmp_path / "outputs" / "synthetic-v1"
    root.mkdir(parents=True)
    path = root / "sample_qc.tsv"
    path.write_bytes(CONTENT)
    database_url = f"sqlite:///{(tmp_path / 'synthetic.sqlite3').as_posix()}"
    create_schema(database_url)
    session = get_session_factory(database_url)()
    session.add(ResearchStudy(id="S-SYNTHETIC", title="Synthetic fixture", description="Not science"))
    session.flush()
    session.add(GeoSeries(accession="GSE000001", study_id="S-SYNTHETIC", title="Synthetic"))
    session.flush()
    run = AnalysisRun(
        series_accession="GSE000001", analysis_version="synthetic-v1", status="completed",
        metadata_json={"auxiliary_files": [path.name]},
        output_manifest_json=[{"path": str(path), "bytes": len(CONTENT),
                               "sha256": hashlib.sha256(CONTENT).hexdigest()}],
    )
    session.add(run)
    session.commit()
    monkeypatch.setattr(api, "DATA_DIR", tmp_path / "data")
    try:
        yield path, session, run
    finally:
        session.close()


def client_for(artifact, after_prepare=None):
    path, session, _ = artifact
    app = FastAPI()  # No primary-app startup or real database/service jobs.

    @app.get("/synthetic-download")
    def download():
        response = api.download_analysis_auxiliary_file(
            "GSE000001", "synthetic-v1", path.name, session,
        )
        if after_prepare is not None:
            after_prepare(path)
        return response

    return TestClient(app)


@pytest.mark.parametrize("scalar", [False, True])
def test_valid_registered_bytes_download_with_scalar_or_list_allowlist(artifact, scalar):
    path, session, run = artifact
    if scalar:
        run.metadata_json = {"auxiliary_files": path.name}
        session.commit()
    with client_for(artifact) as client:
        response = client.get("/synthetic-download")
    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["content-disposition"] == 'attachment; filename="sample_qc.tsv"'


@pytest.mark.parametrize("changed", [b"sample\tqc\nSIM1\tFAIL\n", CONTENT + b"SIM2\tpass\n"])
def test_post_import_content_or_size_change_is_rejected(artifact, changed):
    path, _, _ = artifact
    path.write_bytes(changed)
    with client_for(artifact) as client:
        response = client.get("/synthetic-download")
    assert response.status_code == 409
    assert "differs" in response.json()["detail"]


@pytest.mark.parametrize("changes", [
    {"sha256": None}, {"sha256": "invalid"}, {"bytes": None}, {"bytes": True}, {"bytes": -1},
])
def test_unverifiable_manifest_is_rejected(artifact, changes):
    _, session, run = artifact
    run.output_manifest_json = [{**run.output_manifest_json[0], **changes}]
    session.commit()
    with client_for(artifact) as client:
        response = client.get("/synthetic-download")
    assert response.status_code == 409
    assert "provenance" in response.json()["detail"]


@pytest.mark.parametrize("delete", [False, True])
def test_response_serves_verified_snapshot_after_source_changes(artifact, delete):
    def change(path):
        if delete:
            path.unlink()
        else:
            path.write_bytes(b"changed after response preparation")

    with client_for(artifact, change) as client:
        response = client.get("/synthetic-download")
    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["etag"] == '"' + hashlib.sha256(CONTENT).hexdigest() + '"'


def test_range_download_uses_verified_bytes_and_cleans_temporary_snapshot(artifact, monkeypatch):
    from nutrigenomics import analysis_downloads

    created = []
    original = analysis_downloads.mkstemp

    def track(*args, **kwargs):
        fd, name = original(*args, **kwargs)
        created.append(Path(name))
        return fd, name

    monkeypatch.setattr(analysis_downloads, "mkstemp", track)
    with client_for(artifact, lambda path: path.write_bytes(b"source replaced")) as client:
        response = client.get("/synthetic-download", headers={"Range": "bytes=0-5"})
    assert response.status_code == 206
    assert response.content == CONTENT[:6]
    assert response.headers["content-range"] == f"bytes 0-5/{len(CONTENT)}"
    assert created and all(not path.exists() for path in created)
