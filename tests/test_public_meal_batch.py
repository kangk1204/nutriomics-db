"""Batch values/context match single-food requests, including diagnostic paths."""
import json
import sqlite3

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse
from test_explorer_api import explorer as explorer  # noqa: PLC0414
from test_explorer_scientific_api import _science

from nutrigenomics.explorer_build import sha256
from nutrigenomics.public_service import create_app


@pytest.fixture
def public_batch(explorer, tmp_path):
    _science(explorer)
    folder = tmp_path / "public-batch"
    folder.mkdir()
    path = folder / "nutriomics.sqlite3"
    with sqlite3.connect(explorer["db"]) as original, sqlite3.connect(path) as db:
        original.backup(db)
        db.execute("UPDATE meta SET value_json=? WHERE key='release_id'", (json.dumps("batch-release"),))
        db.execute("ATTACH DATABASE ? AS source_effects", (str(explorer["core"]),))
        db.execute("CREATE TABLE study_effect AS SELECT * FROM source_effects.study_effect")
        for i in range(50):
            identifier = f"food:batch:{i}"
            db.execute("INSERT INTO node SELECT ?,kind,label,label_en,aliases_json,metadata_json,domains_json,degree FROM node WHERE id='food:rice'", (identifier,))
            db.execute("INSERT INTO measurement SELECT id||?, ?,component,amount,unit,basis,status,source_id,raw_json FROM measurement WHERE food='food:rice'", (f":batch:{i}", identifier))
    (folder / "dataset-metadata.json").write_text(json.dumps({"release_id": "batch-release", "expert_review": "pending"}))
    (folder / "manifest.json").write_text(json.dumps({"database_sha256": sha256(path)}))
    app = create_app(folder, allow_candidate=True)
    app.state.fixture_database = path
    with TestClient(app) as client:
        yield client, app


def test_fifty_food_values_provenance_and_context_equal_individual_requests(public_batch):
    client, _ = public_batch
    items = [{"food_id": f"food:batch:{i}", "amount_g": 75 + i} for i in range(50)]
    reference = {"tissue": "PBMC", "regimen_duration_days": 14}
    singles = [client.post("/api/v1/meals/analyze", json={"items": [item], "reference_context": reference}).json() for item in items]
    response = client.post("/api/v1/meals/analyze", json={"items": items, "reference_context": reference})
    assert response.status_code == 200
    assert response.headers["x-nutriomics-release"] == "batch-release"
    assert response.headers["x-explorer-cache"] == "miss"
    batch = response.json()
    assert batch["evidence_by_food"] == [s["evidence_by_food"][0] for s in singles]
    assert batch["observed_evidence"] == [e for s in singles for e in s["observed_evidence"]]
    assert batch["applicability"] == [e for s in singles for e in s["applicability"]]
    assert batch["excluded_measurements"] == [e for s in singles for e in s["excluded_measurements"]]
    assert batch["provenance"] == [e for s in singles for e in s["provenance"]]
    for component in batch["components"]:
        entries = [c for s in singles for c in s["components"]
                   if (c["component_key"], c["unit"]) == (component["component_key"], component["unit"])]
        assert component["amount"] == pytest.approx(sum(c["amount"] for c in entries))
        assert component["source_records"] == [source for c in entries for source in c["source_records"]]
    # The direct renderer is byte-identical to FastAPI's former primitive
    # conversion followed by its default Starlette JSONResponse renderer.
    assert response.content == JSONResponse(jsonable_encoder(batch)).body
    assert batch["individual_prediction"] is False
    assert batch["expert_review"] == "pending"
    effects = batch["observed_evidence"][0]["contrasts"][0]["effects"]
    assert effects and effects[0]["key"]["analysis_version"] == "v1"


@pytest.mark.parametrize("kind,status", [("missing", 404), ("validation", 422), ("timeout", 503), ("source", 503)])
def test_release_identity_wraps_negative_validation_and_guard_responses(public_batch, monkeypatch, kind, status):
    client, _ = public_batch
    from nutrigenomics import explorer_performance as performance
    from nutrigenomics import public_service

    if kind == "timeout":
        def timed_out(*args, **kwargs):
            raise performance.QueryUnavailable("query_timeout")
        monkeypatch.setattr(public_service, "_match", timed_out)
    if kind == "source":
        def changed():
            raise performance.QueryUnavailable("source_changed")
        monkeypatch.setattr(performance, "replica_guard", changed)
    payload = {"items": [{"food_id": "absent" if kind == "missing" else "food:batch:0", "amount_g": 0 if kind == "validation" else 100}]}
    response = client.post("/api/v1/meals/analyze", json=payload)
    assert response.status_code == status
    assert response.headers["x-nutriomics-release"] == "batch-release"
    assert response.headers["x-nutriomics-scope"] == "public"
    if status == 503:
        assert response.json()["detail"]["code"] == ("query_timeout" if kind == "timeout" else "source_changed")
        assert "components" not in response.json()


def test_late_cancellation_does_not_commit_success_headers(public_batch, monkeypatch):
    client, _ = public_batch
    from nutrigenomics import explorer_performance as performance
    from nutrigenomics import public_service
    original = public_service._match

    def cancel_after_computation(*args, **kwargs):
        result = original(*args, **kwargs)
        performance.CURRENT.get().cancel()
        return result

    monkeypatch.setattr(public_service, "_match", cancel_after_computation)
    response = client.post("/api/v1/meals/analyze", json={"items": [{"food_id": "food:batch:0", "amount_g": 100}]})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "query_timeout"
    assert response.headers["x-nutriomics-release"] == "batch-release"


def test_source_change_after_business_computation_cannot_use_direct_body_path(public_batch, monkeypatch):
    client, app = public_batch
    from nutrigenomics import public_service
    original = public_service._match

    def change_after_computation(*args, **kwargs):
        result = original(*args, **kwargs)
        with sqlite3.connect(app.state.fixture_database) as db:
            db.execute("UPDATE node SET label='changed source' WHERE id='food:batch:0'")
        return result

    monkeypatch.setattr(public_service, "_match", change_after_computation)
    response = client.post("/api/v1/meals/analyze", json={"items": [{"food_id": "food:batch:0", "amount_g": 100}]})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "source_changed"
    assert response.headers["x-nutriomics-release"] == "batch-release"


def test_uncacheable_meal_does_not_build_json_string_envelope(monkeypatch):
    from fastapi import FastAPI

    from nutrigenomics import explorer_performance as performance
    app = FastAPI()
    exact = JSONResponse({"effect": 0.0, "p": None, "name": "β 음식", "key": ["GSE", "cpg", "cg0", "a-b", "v1"]}).body

    @app.post("/api/v1/meals/analyze")
    def meal():
        from starlette.responses import Response
        return Response(exact, media_type="application/json", headers={"X-Fixture": "unchanged"})

    app.add_middleware(performance.ExplorerReadMiddleware, include_public=True)
    original = performance.canonical

    def no_envelope(value):
        if isinstance(value, dict) and set(value) == {"status", "headers", "body"}:
            raise AssertionError("Uncacheable meal body must not be encoded as a second JSON string")
        return original(value)

    monkeypatch.setattr(performance, "canonical", no_envelope)
    monkeypatch.setattr(performance, "source_paths", list)
    with TestClient(app) as client:
        response = client.post("/api/v1/meals/analyze", json={"items": []})
    assert response.status_code == 200
    assert response.content == exact
    assert response.headers["x-fixture"] == "unchanged"
    assert response.headers["x-explorer-cache"] == "miss"

