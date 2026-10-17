import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from test_explorer_api import explorer as explorer  # noqa: PLC0414

from nutrigenomics.explorer_build import sha256
from nutrigenomics.public_service import create_app


def test_two_public_apps_do_not_change_global_paths_or_mix_releases(explorer, tmp_path, monkeypatch):
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DATABASE", "deliberately-unavailable.sqlite3")
    clients = []
    original = dict(os.environ)
    for name in ("public-a", "public-b"):
        folder = tmp_path / name
        folder.mkdir()
        database = folder / "nutriomics.sqlite3"
        with sqlite3.connect(explorer["db"]) as source, sqlite3.connect(database) as target:
            source.backup(target)
            target.execute("UPDATE meta SET value_json=? WHERE key='release_id'", (json.dumps(name),))
        (folder / "dataset-metadata.json").write_text(json.dumps({"release_id": name, "expert_review": "pending"}))
        (folder / "manifest.json").write_text(json.dumps({"database_sha256": sha256(database)}))
        clients.append(TestClient(create_app(folder, allow_candidate=True)))
    assert dict(os.environ) == original
    def request(i):
        n = i % 2
        response = clients[n].get("/api/v1/explorer/meta")
        assert response.status_code == 200
        assert response.json()["release_id"] == ("public-a", "public-b")[n]
        assert response.headers["X-NutriOmics-Release"] == ("public-a", "public-b")[n]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(request, range(20)))
    finally:
        for client in clients:
            client.close()
    assert dict(os.environ) == original
