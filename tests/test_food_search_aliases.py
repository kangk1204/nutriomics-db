"""Korean lookup finds actual source records without inventing food identities."""
import hashlib
import json
import sqlite3
import unicodedata

import pytest
from fastapi.testclient import TestClient
from test_explorer_api import explorer as explorer  # noqa: PLC0414

from nutrigenomics.food_search_aliases import context, search_clause
from nutrigenomics.public_service import create_app


def add_foods(path):
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO node VALUES(?, 'food', ?, NULL, '[]', '{}', '[]', 0)", [
            ("USDA-FDC:2710077", "Kimchi"),
            ("food:yogurt-plain", "Yogurt, plain, whole milk"),
            ("food:yogurt-flavored", "Yogurt, fruit, nonfat"),
            ("food:cabbage", "Cabbage, raw"),
        ])


@pytest.mark.parametrize("query,expected", [
    ("김치", ["USDA-FDC:2710077"]),
    ("요구르트", ["food:yogurt-flavored", "food:yogurt-plain"]),
    ("요거트", ["food:yogurt-flavored", "food:yogurt-plain"]),
    ("김치찌개", []), ("없는음식", []), ("%", ["food:percent"]), ("_", ["food:underscore"]),
])
def test_food_api_resolves_only_declared_terms(explorer, query, expected):
    add_foods(explorer["db"])
    before = hashlib.sha256(explorer["db"].read_bytes()).hexdigest()
    result = explorer["client"].get("/api/v1/explorer/entities", params={"kind": "food_record", "q": query})
    assert result.status_code == 200
    assert [item["id"] for item in result.json()["items"]] == expected
    assert hashlib.sha256(explorer["db"].read_bytes()).hexdigest() == before


def test_filters_kind_pagination_and_original_record_are_preserved(explorer):
    add_foods(explorer["db"])
    client = explorer["client"]
    route = "/api/v1/explorer/entities"
    body = client.get(route, params={"kind": "food_record", "q": "김치"}).json()
    english = client.get(route, params={"kind": "food_record", "q": "Kimchi"}).json()
    assert body["items"] == english["items"]
    assert body["items"][0]["aliases"] == []
    assert body["search_context"]["purpose"] == "search_only"
    assert body["search_context"]["scientific_review"] == "not_assessed"
    for filters in ({"kind": "gene"}, {"kind": "exposure"}, {"domain": "obesity"}, {"tier": "curated"}):
        assert client.get(route, params={"kind": "food_record", "q": "김치", **filters}).json()["total"] == 0
    pages = [client.get(route, params={"kind": "food_record", "q": "요구르트", "page_size": 1, "page": p}).json() for p in (1, 2)]
    assert all(p["total"] == 2 for p in pages)
    assert pages[0]["items"][0]["id"] != pages[1]["items"][0]["id"]


def test_source_code_and_label_must_both_match(explorer):
    add_foods(explorer["db"])
    with sqlite3.connect(explorer["db"]) as db:
        db.execute("UPDATE node SET label='Cabbage, raw' WHERE id='USDA-FDC:2710077'")
    body = explorer["client"].get("/api/v1/explorer/entities", params={"kind": "food_record", "q": "김치"}).json()
    assert body["total"] == 0


def test_nfc_search_term_is_supported_but_not_arbitrary_prefixes():
    assert context(unicodedata.normalize("NFD", "김치"))["food_id"] == "USDA-FDC:2710077"
    assert context("  김치  ")["english"] == "Kimchi"
    assert context("김치 찌개") is None
    assert context("김") is None


def test_public_foods_endpoint_uses_the_same_korean_lookup(explorer, tmp_path):
    add_foods(explorer["db"])
    folder = tmp_path / "public-ko"
    folder.mkdir()
    database = folder / "nutriomics.sqlite3"
    with sqlite3.connect(explorer["db"]) as source, sqlite3.connect(database) as target:
        source.backup(target)
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    (folder / "dataset-metadata.json").write_text(json.dumps({"release_id": "fixture-release-20261003", "expert_review": "pending"}))
    (folder / "manifest.json").write_text(json.dumps({"database_sha256": digest}))
    with TestClient(create_app(folder, allow_candidate=True)) as client:
        korean = client.get("/api/v1/foods", params={"query": "김치"}).json()
        english = client.get("/api/v1/foods", params={"query": "Kimchi"}).json()
        assert korean["items"] == english["items"]
        assert len(korean["items"]) == 1
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("query", ["김치", "요구르트", "요거트", "Yogurt", "Ki", 'Yogurt"', "%", "_"])
def test_fts_and_plain_search_return_same_records(explorer, query):
    add_foods(explorer["db"])
    with sqlite3.connect(explorer["db"]) as db:
        db.execute("CREATE TABLE perf_search_content(rowid INTEGER PRIMARY KEY,node_id TEXT,label TEXT,label_en TEXT,aliases TEXT,identifier TEXT)")
        db.execute("CREATE VIRTUAL TABLE perf_search USING fts5(label,label_en,aliases,identifier, content='perf_search_content',content_rowid='rowid', tokenize='trigram')")
        for rowid, (identifier, label, label_en, aliases) in enumerate(db.execute("SELECT id,label,label_en,aliases_json FROM node").fetchall(), 1):
            db.execute("INSERT INTO perf_search_content VALUES(?,?,?,?,?,?)", (rowid, identifier, label, label_en or "", aliases, identifier))
            db.execute("INSERT INTO perf_search(rowid,label,label_en,aliases,identifier) VALUES(?,?,?,?,?)", (rowid, label, label_en or "", aliases, identifier))
        results = []
        for indexed in (False, True):
            clause, args = search_clause(query, indexed=indexed)
            results.append(db.execute("SELECT n.id FROM node n WHERE n.kind='food' AND " + clause + " ORDER BY n.id", args).fetchall())
        assert results[0] == results[1]
