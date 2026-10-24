"""The literature snapshot must coexist with the unchanged research DB/API."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from test_explorer_api import explorer as explorer  # noqa: F401
from test_literature_platform import source as literature_source  # noqa: F401

from nutrigenomics.explorer_build import sha256
from nutrigenomics.literature_snapshot import build_snapshot
from nutrigenomics.public_service import create_app


def test_literature_and_existing_database_routes_coexist_without_mutation(
    explorer, literature_source, tmp_path, monkeypatch
):
    folder = tmp_path / 'science-release'; folder.mkdir()
    database = folder / 'nutriomics.sqlite3'
    source = sqlite3.connect(explorer['db']); target = sqlite3.connect(database)
    source.backup(target); source.close(); target.close()
    with sqlite3.connect(database) as con:
        release = json.loads(con.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()[0])
    (folder / 'dataset-metadata.json').write_text(json.dumps({'release_id': release, 'expert_review': 'pending'}))
    (folder / 'manifest.json').write_text(json.dumps({'database_sha256': sha256(database)}))
    monkeypatch.delenv('NUTRIOMICS_LITERATURE_SNAPSHOT', raising=False)
    before = sha256(database)
    with TestClient(create_app(folder, allow_candidate=True)) as existing:
        query = '/api/v1/explorer/entities?kind=food_record&page_size=3'
        original = existing.get(query).json()
        assert existing.get('/api/v1/explorer/literature/stats').json()['status'] == 'not_connected'
    snapshot = tmp_path / 'literature-public'
    build_snapshot(literature_source, snapshot, release_id='literature-integration-fixture')
    monkeypatch.setenv('NUTRIOMICS_LITERATURE_SNAPSHOT', str(snapshot))
    with TestClient(create_app(folder, allow_candidate=True)) as integrated:
        def request(index):
            if index % 2:
                response = integrated.get(query)
                assert response.status_code == 200 and response.json() == original
            else:
                response = integrated.get('/api/v1/explorer/literature/papers?page_size=3')
                assert response.status_code == 200
                assert response.json()['release_id'] == 'literature-integration-fixture'
                assert response.json()['total'] == 2
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(request, range(20)))
        assert integrated.get('/healthz').json()['release_id'] == release
        assert integrated.get('/api/v1/explorer/meta').json()['release_id'] == release
        assert integrated.post('/api/v1/literature-admin/jobs', json={'kind': 'collect'}).status_code == 404
        assert integrated.get('/api/v1/explorer/literature/papers/1').json()['source_text'] is None
        assert integrated.get('/api/v1/explorer/literature/papers/2').json()['source_text'].startswith('β 😀')
    assert sha256(database) == before
