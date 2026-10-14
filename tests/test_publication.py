import json
import sqlite3

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from test_explorer_api import explorer as explorer  # noqa: PLC0414 -- shared pytest fixture
from test_explorer_scientific_api import _science

from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.explorer_build import sha256
from nutrigenomics.models import AnalysisRun, GeoSeries, ResearchStudy, StudyEffect
from nutrigenomics.public_service import create_app
from nutrigenomics.publication import build_candidate, effect_id, portable, validate_human_review
from scripts.prepare_public_service_release import prepare
from scripts.validate_publication_values import validate


def test_publication_preserves_null_nonsignificant_values_rights_and_keys(explorer, tmp_path, monkeypatch):
    _science(explorer)
    with sqlite3.connect(explorer["db"]) as parent_db:
        parent_db.executemany("INSERT INTO measurement VALUES(?,?,?,?,?,?,?,?,?)", [
            ("m-lod", "food:gap", "nutrient:fat", 0, "g", "source_100g", "below_detection", "open", "{}"),
            ("m-volume", "food:gap", "nutrient:fat", 10, "g", "source_100ml", "measured", "open", "{}"),
        ])
    parent = explorer["db"].parent
    (parent / "manifest.json").write_text(json.dumps({"release_id": "test-parent", "database_sha256": sha256(explorer["db"])}))
    core = tmp_path / "full-core.sqlite3"
    url = "sqlite:///" + core.as_posix()
    create_schema(url)
    source = tmp_path / "result.tsv"
    source.write_text("known derived values")
    digest = sha256(source)
    with get_session_factory(url)() as session:
        session.add(ResearchStudy(id="T1", title="Trial", description="Trial"))
        session.flush()
        session.add(GeoSeries(accession="GSE_FIXTURE", study_id="T1", title="Trial"))
        session.flush()
        session.add(AnalysisRun(series_accession="GSE_FIXTURE", analysis_version="v1", status="completed",
                                output_manifest_json=[{"path": str(source), "sha256": digest}]))
        for feature, value, denied in (("A", None, False), ("B", -0.25, False), ("C", 0.4, True)):
            session.add(StudyEffect(series_accession="GSE_FIXTURE", feature_type="gene_expression", feature_id=feature,
                                    contrast="6h minus baseline", analysis_version="v1", effect_estimate=value,
                                    q_value=0.72, source_file=str(source), metadata_json={"source_file_sha256": digest, "export_allowed": not denied}))
        session.commit()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"analyses": [{"id": "own-1", "series_accession": "GSE_FIXTURE", "analysis_version": "v1", "export_allowed": True}]}))
    output = tmp_path / "candidate"
    result = build_candidate(parent, core, policy, output, "test-public", batch_size=1)
    assert result["counts"]["effect"] == 2
    assert result["expert_review"] == "pending" and result["doi"] is None
    with sqlite3.connect(output / "nutriomics.sqlite3") as db:
        rows = db.execute("SELECT feature_id,effect_estimate,q_value FROM effect ORDER BY feature_id").fetchall()
        assert rows == [("A", None, 0.72), ("B", -0.25, 0.72)]
        assert db.execute("SELECT count(*) FROM science_record").fetchone()[0] == 4
        assert not db.execute("SELECT 1 FROM edge WHERE source_ids_json LIKE '%closed%'").fetchone()
    table = pq.read_table(next((output / "effects").glob("*.parquet")))
    assert table["effect_estimate"].to_pylist() == [None, -0.25]
    assert "/home/research/private" not in (output / "source-catalog.json").read_text()
    assert result["counts"]["science_kinds"]["contrast"] == 1
    parity = validate(output, tmp_path / "values.json", core)
    assert parity["passed"] and parity["source_compared"] and parity["checked_rows"] == 2
    with pytest.raises(FileExistsError):
        build_candidate(parent, core, policy, output, "test-public")
    service = tmp_path / "service"
    prepare(output, parent, service, "test-public-service")
    from nutrigenomics.explorer_scientific import _cached_effects
    missing_preview = _cached_effects(service / "nutriomics.sqlite3", {
        "series_accession": "GSE_FIXTURE", "contrast": "withheld contrast", "analysis_version": "v1"}, None, 6)
    assert missing_preview == ([], None, "not_in_public_release")
    with pytest.raises(ValueError, match="reviewed"):
        create_app(service)
    # Fixture restores process-local paths after this isolated app is closed.
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DATABASE", str(service / "nutriomics.sqlite3"))
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", str(service / "nutriomics.sqlite3"))
    with TestClient(create_app(service, allow_candidate=True)) as client:
        page = client.get("/api/v1/evidence", params={"accession": "GSE_FIXTURE", "contrast": "6h minus baseline", "analysis_version": "v1", "limit": 1}).json()
        assert page["items"][0]["effect_estimate"] is None
        assert page["total_selector"] == 2 and page["next_cursor"]
        next_page = client.get("/api/v1/evidence", params={"accession": "GSE_FIXTURE", "contrast": "6h minus baseline", "analysis_version": "v1", "limit": 1, "cursor": page["next_cursor"]}).json()
        assert next_page["items"][0]["feature_id"] == "B"
        assert client.get("/api/v1/evidence", params={"accession": "GSE_FIXTURE", "contrast": "wrong-selector", "analysis_version": "v1", "cursor": page["next_cursor"]}).status_code == 422
        missing = client.get("/api/v1/evidence", params={"accession": "GSE_FIXTURE", "contrast": "wrong-selector", "analysis_version": "v1"}).json()
        assert missing["total_selector"] is None and missing["effects_status"] == "not_in_public_release"
        assert client.post("/api/v1/collect").status_code == 404
        assert client.get("/api/v1/public/meta").json()["release_id"] == "test-public-service"
        assert client.get("/api/v1/explorer/entities").status_code == 200
        assert client.get("/api/v1/studies/T1").json()["study"]["id"] == "T1"
        meal = client.post("/api/v1/meals/analyze", json={
            "items": [{"food_id": 1, "amount_g": 150}],
            "reference_context": {"tissue": "whole blood", "response_time_h": 6},
        })
        assert meal.status_code == 200
        value = meal.json()
        assert value["components"][0]["amount"] == 3.75
        assert value["observed_evidence"][0]["contrasts"][0]["source_effect_total"] == 2
        assert value["reference_context"]["response_time_h"] == 6
        assert value["individual_prediction"] is False
        assert value["observed_evidence"][0]["query_intake_g"] == 150
        fruit = client.post("/api/v1/meals/analyze", json={"items": [{"food_id": "food:fruit", "amount_g": 100}]}).json()
        assert fruit["components"][0]["amount"] == 0
        assert fruit["excluded_measurements"][0]["status"] == "missing"
        gap = client.post("/api/v1/meals/analyze", json={"items": [{"food_id": "food:gap", "amount_g": 100}]}).json()
        assert gap["components"] == [] and gap["items"][0]["calculation_mode"] == "unavailable"
        assert {x["status"] for x in gap["excluded_measurements"]} == {"below_detection", "measured"}
        assert client.post("/api/v1/meals/analyze", content='{"items":[{"food_id":1,"amount_g":NaN}]}',
                           headers={"content-type": "application/json"}).status_code == 422


def test_human_review_cannot_be_satisfied_by_pending_or_wrong_hash():
    record = {"record_id": "r", "record_sha256": "abc"}
    assert validate_human_review([record], [{**record, "decision": "pending"}])
    accepted = {**record, "decision": "accept", "reviewer": "Professor", "reviewed_at": "2026-10-14", "reason": "checked", "primary_paper_location": "Methods, table 1"}
    assert validate_human_review([record], [accepted]) == []
    assert validate_human_review([record], [{**accepted, "record_sha256": "stale"}])[0]["reason"] == "review_hash_mismatch"


def test_portability_and_stable_key():
    assert portable({"source_file": "/home/user/private", "p": None, "path": "/home/user/a.tsv", "effect": 0}) == {"p": None, "path": {"filename": "a.tsv", "operational_path_withheld": True}, "effect": 0}
    row = {"series_accession": "GSE1", "feature_type": "cpg", "feature_id": "cg1", "contrast": "after-before", "analysis_version": "v1"}
    assert effect_id(row) != effect_id({**row, "analysis_version": "v2"})


def test_submission_gate_does_not_accept_metadata_or_automatic_checks_only():
    from scripts.submission_readiness import evaluate
    result = evaluate({"release_id": "r", "counts": {}}, [{"record_id": "x", "record_sha256": "a"}], [], {})
    assert result["submission_ready"] is False
    reasons = {r["reason"] for r in result["failures"]}
    assert "review_hash_mismatch" in reasons
    assert "public_expression_and_DNAm_results_required" in reasons
    assert "sealed_benchmark_pending" in reasons and "public_https_pending" in reasons
