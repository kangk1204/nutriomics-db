"""Scientific API boundaries and snapshot rights using the existing release fixture."""

import json
import sqlite3

import pytest
from test_explorer_api import RELEASE
from test_explorer_api import explorer as explorer  # noqa: PLC0414 -- explicit fixture re-export


def _science(fixture):
    records = {
        "exposure": [
            {
                "id": "trial-yogurt",
                "study_id": "T1",
                "label": "Trial yogurt",
                "kind": "product",
                "formulation": "specific product",
                "amount": 800,
                "unit": "g",
                "duration_days": 0,
                "dose_basis": "whole trial product mass",
                "component_keys": [],
                "food_terms": ["yogurt"],
                "metadata": {"dose_frequency": "single"},
                "source_id": "trial-source",
            }
        ],
        "study": [{"id": "T1", "title": "Trial source"}],
        "bridge_source": [
            {
                "id": "trial-source",
                "url": "https://example.org/trial",
                "export_allowed": True,
                "sha256": "a" * 64,
                "source_location": "Methods",
                "version": "curation-v1",
            }
        ],
        "contrast": [
            {
                "id": "trial-change",
                "study_id": "T1",
                "series_accession": "GSE_FIXTURE",
                "contrast": "6h minus baseline",
                "analysis_version": "v1",
                "exposure_ids": ["trial-yogurt"],
                "arm_ids": [],
                "tissue": "whole blood",
                "response_time_h": 6,
                "design": "randomized_crossover",
                "n_people": 9,
                "n_samples": 72,
            }
        ],
    }
    with sqlite3.connect(fixture["db"]) as db:
        db.execute(
            "CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT,PRIMARY KEY(kind,id))"
        )
        db.execute(
            "UPDATE node SET label='Yogurt, plain',label_en='Yogurt, plain' WHERE id='food:rice'"
        )
        for kind, rows in records.items():
            for record in rows:
                db.execute(
                    "INSERT INTO science_record VALUES (?,?,?)",
                    (record["id"], kind, json.dumps(record)),
                )
    with sqlite3.connect(fixture["core"]) as db:
        db.execute("UPDATE study_effect SET metadata_json='{}'")


def test_legacy_release_reports_missing_context_as_a_normal_gap(explorer):
    response = explorer["client"].post("/api/v1/explorer/match", json={"seed_id": "food:rice"})
    assert response.status_code == 200
    assert response.json()["observed_evidence"] == []
    assert response.json()["evidence_gaps"]
    assert response.json()["reference_context"]["intake_g"] == 100


@pytest.mark.parametrize(
    "payload",
    [
        {"seed_id": "missing"},
        {"seed_id": "food:rice", "gene_node_id": "food:rice"},
        {"seed_id": "food:rice", "intake_g": -1},
        {"seed_id": "food:rice", "intake_g": 0},
        {"seed_id": "food:rice", "intake_g": 20001},
        {"seed_id": "food:rice", "reference_context": {"tissue": "x" * 161}},
        {"seed_id": "food:rice", "reference_context": {"frequency": "weekly"}},
        {"seed_id": "food:rice", "reference_context": {"response_time_h": -1}},
    ],
)
def test_invalid_scientific_context_is_rejected(explorer, payload):
    assert explorer["client"].post("/api/v1/explorer/match", json=payload).status_code in {404, 422}


def test_nonfood_does_not_inherit_food_mass(explorer):
    response = explorer["client"].post(
        "/api/v1/explorer/match", json={"seed_id": "nutrient:fat", "intake_g": 150}
    )
    assert response.status_code == 200
    assert response.json()["reference_context"]["intake_g"] is None


def test_source_effects_keep_missing_family_and_identity_candidate(explorer):
    _science(explorer)
    response = explorer["client"].post(
        "/api/v1/explorer/match",
        json={"seed_id": "food:rice", "intake_g": 150, "gene_node_id": "NCBIGene:41"},
    )
    card = response.json()["observed_evidence"][0]
    assert card["mapping_identity"]["status"] == "related_food"
    assert card["study_evidence"]["expert_review_status"] == "pending"
    assert (
        next(r for r in card["applicability"] if r["dimension"] == "dose")["status"] == "unknown"
    )
    assert card["mapping_identity"]["method"] == "food_term_correspondence"
    effect = card["contrasts"][0]["effects"][0]
    assert effect["feature_mapping"]["status"] == "candidate_label"
    assert effect["effect_estimate"] == -0.25 and effect["q_value"] == 0.72
    assert effect["analysis_context"]["multiple_testing"] is None


def test_export_reconstructs_and_freezes_context_and_withholds_unassessed_stats(explorer):
    _science(explorer)
    payload = {
        "format": "json",
        "release_id": RELEASE,
        "node_ids": ["food:rice"],
        "filters": {
            "scientific_context": {
                "seed_id": "food:rice",
                "intake_g": 150,
                "reference_context": {"response_time_h": 6},
            }
        },
    }
    response = explorer["client"].post("/api/v1/explorer/export", json=payload)
    assert response.status_code == 200
    scientific = response.json()["node_details"]["food:rice"]["scientific_matches"]
    assert scientific["offline_context_frozen"] is True
    assert scientific["reference_context"]["intake_g"] == 150
    assert scientific["reference_context"]["response_time_h"] == 6
    card = scientific["observed_evidence"][0]
    assert card["id"] == "scientific:trial-yogurt"
    assert card["contrasts"][0]["effects"] == []
    assert card["contrasts"][0]["withheld_effect_previews"] == 1
    assert card["provenance"][0]["hash_scope"] == "curation_manifest"
