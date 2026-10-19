import json
import sqlite3
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nutrigenomics.clinical_api import router


def test_dictionary_is_derived_from_this_release_without_private_record_values(monkeypatch):
    from nutrigenomics import explorer
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE meta(key TEXT,value_json TEXT); CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT);")
    db.execute("INSERT INTO meta VALUES('release_id','\"current-release\"')")
    db.execute("INSERT INTO science_record VALUES('x','clinical_endpoint_result',?)",
        (json.dumps({"id": "x", "endpoint": {"unit": "mmHg"}, "private_path": "/internal/path"}),))

    @contextmanager
    def database():
        yield db

    monkeypatch.setattr(explorer, "database", database)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/v1/explorer/dictionary")
    assert response.status_code == 200
    value = response.json()
    assert value["release_id"] == "current-release"
    assert value["tables"]["science_record"][2]["name"] == "metadata_json"
    unit = next(r for r in value["clinical_subtypes"]["definitions"] if r["field"] == "endpoint.unit")
    assert unit["present_in_records"] == 1
    assert "/internal/path" not in response.text
    assert value["scientific_approval"] is False
    db.close()
