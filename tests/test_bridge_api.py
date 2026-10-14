from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from nutrigenomics import api
from nutrigenomics.bridge_models import BridgeSource, EvidenceAssertion, Exposure, StudyContrast
from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import (
    AnalysisRun,
    Component,
    CompositionMeasurement,
    Food,
    GeoSeries,
    KnowledgeLink,
    ResearchStudy,
    Resource,
    StudyEffect,
)
from nutrigenomics.schemas import MealAnalysisInput, exportable_payload


@pytest.fixture
def session(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'bridge-api.sqlite3').as_posix()}"
    create_schema(url)
    with get_session_factory(url)() as db:
        db.add(Resource(id="TEST", name="Measured food", layer="composition"))
        db.add(ResearchStudy(id="S-TEST", title="Human crossover", description="Test intervention"))
        db.flush()
        db.add(GeoSeries(accession="GSETEST", study_id="S-TEST", title="Test series"))
        db.add(Component(component_key="test-fat", name="Fat", category="nutrient", canonical_unit="g"))
        db.add(Food(source_id="TEST", source_code="food", name="Test food"))
        db.flush()
        db.add(CompositionMeasurement(
            food_id=1, component_id=1, source_id="TEST", source_ref="food:fat",
            release="v1", amount_per_100g=10, unit="g", basis="edible_portion_100g",
            status="measured",
        ))
        db.commit()
        yield db


@pytest.fixture
def client(session: Session):
    api.app.dependency_overrides[api.get_session] = lambda: session
    instance = TestClient(api.app)
    yield instance
    instance.close()
    api.app.dependency_overrides.clear()


@pytest.mark.parametrize("auxiliary_files", ["sample_qc.tsv", ["sample_qc.tsv"]])
def test_auxiliary_download_requires_exact_name_for_scalar_or_list(
    client, session, tmp_path, monkeypatch, auxiliary_files
):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path / "data")
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    files = []
    for filename in ["sample_qc.tsv", "qc.tsv", "sample"]:
        path = output_root / filename
        path.write_bytes(b"frozen QC output\n")
        files.append({
            "path": str(path), "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    metadata = {"auxiliary_files": auxiliary_files, "qc_status": "approved"}
    frozen_metadata = json.dumps(metadata, sort_keys=True)
    run = AnalysisRun(
        series_accession="GSETEST", analysis_version="scalar-v1", metadata_json=metadata,
        output_manifest_json=files,
    )
    session.add(run)
    session.commit()
    prefix = "/api/v1/studies/GSETEST/analysis-runs/scalar-v1/files/"
    response = client.get(prefix + "sample_qc.tsv")
    assert response.status_code == 200
    assert response.content == b"frozen QC output\n"
    # Both substrings have real manifest entries, so the whitelist itself must reject them.
    assert client.get(prefix + "qc.tsv").status_code == 404
    assert client.get(prefix + "sample").status_code == 404
    session.refresh(run)
    assert json.dumps(run.metadata_json, sort_keys=True) == frozen_metadata


def test_scalar_auxiliary_download_keeps_output_path_confined(
    client, session, tmp_path, monkeypatch
):
    monkeypatch.setattr(api, "DATA_DIR", tmp_path / "data")
    outside = tmp_path / "sample_qc.tsv"
    outside.write_bytes(b"outside output root")
    session.add(AnalysisRun(
        series_accession="GSETEST", analysis_version="scalar-outside-v1",
        metadata_json={"auxiliary_files": "sample_qc.tsv"},
        output_manifest_json=[{"path": str(outside)}],
    ))
    session.commit()
    response = client.get(
        "/api/v1/studies/GSETEST/analysis-runs/scalar-outside-v1/files/sample_qc.tsv"
    )
    assert response.status_code == 404


def test_meal_context_is_optional_and_additive(client, monkeypatch):
    received = []

    def evidence(db, calculation, reference_context=None):
        received.append(reference_context)
        assert calculation["components"][0]["amount"] == 15
        return {
            "observed_evidence": [], "mechanistic_hypotheses": [],
            "applicability": [], "evidence_gaps": ["duration_unknown"], "provenance": [],
        }

    monkeypatch.setattr(api, "meal_evidence", evidence)
    response = client.post("/api/v1/meals/analyze", json={"items": [{"food_id": 1, "amount_g": 150}]})
    assert response.status_code == 200
    body = response.json()
    assert body["components"][0]["amount"] == 15
    assert body["evidence"] == []
    assert body["evidence_gaps"] == ["duration_unknown"]
    assert body["reference_context"] is None
    assert received == [None]
    response = client.post("/api/v1/meals/analyze", json={
        "items": [{"food_id": 1, "amount_g": 150}],
        "reference_context": {"tissue": " PBMC ", "response_time_h": 6},
    })
    assert response.status_code == 200
    assert received[-1] == {
        "tissue": "PBMC", "response_time_h": 6, "regimen_duration_days": None,
    }


@pytest.mark.parametrize("field,value", [
    ("response_time_h", -1), ("response_time_h", float("inf")),
    ("regimen_duration_days", float("nan")), ("regimen_duration_days", -1),
])
def test_context_rejects_invalid_time(field, value):
    with pytest.raises(ValidationError):
        MealAnalysisInput.model_validate({
            "items": [{"food_id": 1, "amount_g": 100}],
            "reference_context": {field: value},
        })


def test_links_attach_only_exact_effect_versions(session):
    for feature_type, contrast, version in [
        ("gene", "acute", "v1"), ("gene", "chronic", "v1"),
        ("gene", "acute", "v2"), ("pathway", "acute", "v1"),
    ]:
        session.add(StudyEffect(
            series_accession="GSETEST", feature_type=feature_type, feature_id="same-id",
            contrast=contrast, analysis_version=version, effect_estimate=1,
        ))
    for ref, metadata in [
        ("exact-v1", {"series_accession": "GSETEST", "contrast": "acute", "analysis_version": "v1"}),
        ("exact-v2", {"series_accession": "GSETEST", "contrast": "acute", "analysis_version": "v2"}),
        ("ambiguous", {"series_accession": "GSETEST"}),
    ]:
        session.add(KnowledgeLink(
            source="test", source_ref=ref, subject_type="component", subject_id="test-fat",
            relation="observed", target_type="gene", target_id="same-id",
            evidence_kind="human_intervention", metadata_json=metadata,
        ))
    session.flush()
    rows = {row["source_ref"]: row for row in api._evidence_for_components(session, {1})}
    assert [(effect["feature_type"], effect["contrast"], effect["analysis_version"])
            for effect in rows["exact-v1"]["effects"]] == [("gene", "acute", "v1")]
    assert rows["exact-v2"]["effects"][0]["analysis_version"] == "v2"
    assert rows["ambiguous"]["effects"] == []
    assert rows["ambiguous"]["effect_attachment_status"] == "ambiguous_effect_reference"


def test_interventions_and_provenance_routes(client, monkeypatch):
    monkeypatch.setattr(api, "list_interventions", lambda db, **kwargs: {
        "items": [{"id": "exposure-1"}], "total": 1, "count": 1, **kwargs,
    })
    monkeypatch.setattr(api, "get_provenance", lambda db, evidence_id:
                        {"id": evidence_id, "source_url": "https://example.org/source"}
                        if evidence_id == "verified" else None)
    result = client.get("/api/v1/interventions?query=yoghurt&limit=2&offset=3")
    assert result.status_code == 200
    assert result.json()["offset"] == 3
    assert client.get("/api/v1/provenance/verified").status_code == 200
    assert client.get("/api/v1/provenance/missing").status_code == 404


def test_assertion_filters_and_exports_keep_legacy_response(client, monkeypatch):
    received = []

    def assertions(db, **kwargs):
        received.append(kwargs)
        return {"items": [
            {"id": "open", "layer": "observed", "export_allowed": True},
            {"id": "restricted", "layer": "observed", "export_allowed": False,
             "source_text": "do not redistribute"},
        ], "total": 2, "count": 2, "offset": kwargs["offset"], "limit": kwargs["limit"]}

    monkeypatch.setattr(api, "search_assertions", assertions)
    response = client.get(
        "/api/v1/evidence?layer=observed&organism=Homo+sapiens&tissue=PBMC"
        "&study_design=randomized_crossover&contrast=acute&accession=GSETEST"
        "&feature_type=gene&analysis_version=v2&limit=5&offset=2"
    )
    assert response.status_code == 200
    body = response.json()
    assert {"effects", "links", "count", "total_effects", "total_links"} <= body.keys()
    assert body["total_assertions"] == 2
    assert received[-1]["accession"] == "GSETEST"
    assert received[-1]["feature_type"] == "gene"
    assert received[-1]["analysis_version"] == "v2"
    exported = client.get("/api/v1/export/evidence?format=json").json()
    assert exported["assertions"] == [{"id": "open", "layer": "observed", "export_allowed": True}]
    assert exported["export_withheld_records"] == 1
    csv = client.get("/api/v1/export/evidence?format=csv").text
    assert "evidence_assertion" in csv
    assert "restricted" not in csv
    assert "do not redistribute" not in csv


def test_recursive_export_excludes_restricted_rows_without_mutation():
    payload = {
        "items": [{"amount": 150, "analysis": {
            "components": [{"name": "Fat", "amount": 15}],
            "observed_evidence": [{"id": "restricted", "export_allowed": False}],
            "provenance": [{"source_url": "https://example.org", "export_allowed": True}],
        }}],
    }
    original = json.dumps(payload)
    exported = exportable_payload(payload)
    assert exported["items"][0]["analysis"]["observed_evidence"] == []
    assert exported["items"][0]["analysis"]["components"][0]["amount"] == 15
    assert exported["export_withheld_records"] == 1
    assert json.dumps(payload) == original


def test_study_detail_contains_bridge_additively(client, monkeypatch):
    monkeypatch.setattr(api, "study_bridge_detail", lambda db, accession: {
        "exposures": [{"name": "SFA shake"}], "assertions": [],
    })
    response = client.get("/api/v1/studies/GSETEST")
    assert response.status_code == 200
    assert response.json()["counts"]["geo_sample_records"] == 0
    assert response.json()["intervention_evidence"]["exposures"][0]["name"] == "SFA shake"


def test_real_bridge_preserves_null_results_and_dose_mismatch(client, session):
    session.add(BridgeSource(
        id="source", url="https://example.org/human-study", version="v1", sha256="a" * 64,
        source_location="Methods and Table 2", license="Test open data", export_allowed=True,
    ))
    session.flush()
    session.add(Exposure(
        id="exposure", study_id="S-TEST", label="Chronic fat intake", kind="nutrient",
        formulation="specified test nutrient", amount=2, unit="g", dose_basis="assigned g/day",
        duration_days=42, component_keys=["test-fat"], food_terms=[], source_id="source",
    ))
    session.add(StudyContrast(
        id="contrast", study_id="S-TEST", series_accession="GSETEST", contrast="post_minus_pre",
        analysis_version="v1", comparison="within person post minus pre",
        design="uncontrolled_before_after", tissue="PBMC", response_time_h=6,
        exposure_ids=["exposure"], n_people=2, n_samples=4, analysis_status="reanalyzed",
    ))
    session.flush()
    session.add(EvidenceAssertion(
        id="assertion", study_id="S-TEST", exposure_id="exposure", contrast_id="contrast",
        source_id="source", layer="molecular_measurement", assertion="Published paired comparison",
        review_status="source_checked", organism="Homo sapiens", inference="observed",
        limitations=["No concurrent control"],
    ))
    session.add(StudyEffect(
        series_accession="GSETEST", feature_type="gene", feature_id="NULL_GENE",
        contrast="post_minus_pre", analysis_version="v1", effect_estimate=-0.2,
        p_value=0.7, q_value=0.8, n_people=2, n_samples=4,
    ))
    producer_audit = {
        "manifest": {"path": "audit_manifest.json", "sha256": "c" * 64, "bytes": 321},
        "audit": {
            "argv_status": "reconstructed_not_recorded",
            "verification_status": "passed_with_declared_limitations",
            "verification_scope": "Frozen executed source and hashes; narrower historical audit",
            "historical_limitations": ["Original receipt did not capture argv"],
            "expert_manual_review": "pending",
        },
    }
    run_metadata = {"producer_output_audit": producer_audit, "sample_scope": {"fitted_model_assays": 4}}
    frozen_metadata = json.dumps(run_metadata, sort_keys=True)
    analysis_run = AnalysisRun(
        series_accession="GSETEST", analysis_version="v1", metadata_json=run_metadata,
        input_manifest_json=[{"path": "input.tsv", "sha256": "d" * 64}],
        output_manifest_json=[{"path": "effects.tsv", "sha256": "e" * 64}],
    )
    session.add(analysis_run)
    session.commit()
    response = client.post("/api/v1/meals/analyze", json={
        "items": [{"food_id": 1, "amount_g": 150}],
        "reference_context": {"tissue": "whole blood", "response_time_h": 6},
    })
    assert response.status_code == 200
    observed = response.json()["observed_evidence"][0]
    assert observed["results"][0]["effect_estimate"] == -0.2
    assert observed["results"][0]["q_value"] == 0.8
    assert observed["applicability"]["dimensions"]["dose"] == "mismatch"
    assert observed["applicability"]["dimensions"]["duration"] == "unknown"
    assert observed["applicability"]["dimensions"]["tissue"] == "mismatch"
    assert observed["individual_prediction"] is False
    assert observed["clinical_endpoints"] == []
    assert observed["analysis_provenance"]["producer_output_audit"] == producer_audit
    assert observed["analysis_provenance"]["sample_scope"] == {"fitted_model_assays": 4}
    assert observed["analysis_provenance"]["expert_manual_review_status"] == "pending"
    effects = client.get(
        "/api/v1/evidence?accession=GSETEST&contrast=post_minus_pre&analysis_version=v1"
    ).json()
    assert effects["effects"][0]["effect_estimate"] == -0.2
    source = client.get("/api/v1/provenance/assertion").json()
    assert source["source_url"] == "https://example.org/human-study"
    assert source["analysis"]["producer_output_audit"] == producer_audit
    session.refresh(analysis_run)
    assert json.dumps(analysis_run.metadata_json, sort_keys=True) == frozen_metadata


def test_annotation_pagination_and_export_are_separate(client, monkeypatch):
    received = []

    def annotations(db, **kwargs):
        received.append(kwargs)
        return {"total": 5, "records": [
            {"id": "annotation-open", "layer": "molecular_target", "export_allowed": True},
            {"id": "annotation-restricted", "layer": "molecular_target", "export_allowed": False},
        ], "limit": kwargs["limit"], "offset": kwargs["offset"]}

    monkeypatch.setattr(api, "search_annotations", annotations)
    response = client.get("/api/v1/evidence?layer=molecular_target&query=EPA&limit=1000&offset=3")
    assert response.status_code == 200
    result = response.json()
    assert result["total_mechanistic_annotations"] == 5
    assert result["mechanistic_annotation_count"] == 2
    assert result["annotation_limit"] == 200
    assert result["annotation_offset"] == 3
    assert result["count"] == 0
    assert result["assertion_count"] == 0
    assert received[-1]["layer"] == "molecular_target"
    exported = client.get("/api/v1/export/evidence?layer=molecular_target").json()
    assert exported["mechanistic_annotation_count"] == 1
    assert exported["mechanistic_annotations"][0]["id"] == "annotation-open"
    assert exported["export_withheld_records"] == 1
    old_calls = len(received)
    filtered = client.get("/api/v1/evidence?accession=GSETEST&contrast=post_pre").json()
    assert filtered["mechanistic_annotations"] == []
    assert len(received) == old_calls
