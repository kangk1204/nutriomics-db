import json

import pytest

from nutrigenomics.context_joins import (
    analyte_reference_paths,
    migrate_context,
    put_context,
    trial_context_health,
    validate_context,
)
from nutrigenomics.expansion import connect, insert, migrate
from scripts.import_public_expansion import entity
from scripts.import_trial_context import aggregate, arm, outcome


@pytest.fixture
def db(tmp_path):
    db = connect(tmp_path / "context.sqlite3")
    migrate(db)
    migrate_context(db)
    insert(
        db,
        "ng_release",
        {
            "id": "source",
            "source": "test",
            "version": "1",
            "url": "https://example.org",
            "retrieved_at": "2026-10-01",
            "sha256": "a" * 64,
            "local_path": "",
            "license": "test",
            "access_status": "public",
        },
    )
    for identifier in ["omics", "parent"]:
        insert(
            db,
            "ng_study",
            {
                "id": identifier,
                "title": identifier,
                "population": "test",
                "design": "randomized",
                "participant_group": "same-group",
                "independence_status": "overlap",
                "independence_basis": "same trial",
            },
        )
    entity(db, "food", "food", "trial food", "test", "food")
    outcome(db, "endpoint", "cells per crypt", "tissue_surrogate")
    insert(
        db,
        "ng_dataset",
        {
            "id": "matrix",
            "study_id": "omics",
            "accession": "GSE-test",
            "release_id": "source",
            "modality": "expression",
            "access_status": "public",
            "ingest_status": "measurements",
            "n_assays": 56,
        },
    )
    arm(db, "omics", "fish", "food", 300, "g/week", "6 months", "source")
    aggregate(
        db,
        "null-contrast",
        "food",
        "endpoint",
        "parent",
        "source",
        "123",
        {"endpoint_class": "tissue_surrogate", "null_result": True},
        -0.1,
        "cells/crypt",
        (-0.36, 0.16),
    )
    return db


def link(db, status="verified"):
    put_context(
        db,
        "ng_study_link",
        {
            "id": "link",
            "from_study": "omics",
            "to_study": "parent",
            "relation": "named trial",
            "review_status": status,
            "participant_join_allowed": 0,
            "release_id": "source",
            "basis": "no participant crosswalk",
        },
    )


def test_trial_effect_is_not_repeated_for_56_assays(db):
    link(db)
    rows = trial_context_health(db, "matrix")
    assert len(rows) == 1
    assert rows[0]["ci_low"] == -0.36 and rows[0]["ci_high"] == 0.16
    assert rows[0]["individual_outcome_join"] is False
    assert rows[0]["molecule_effect_attribution"] is False


def test_candidate_protocol_never_produces_health_join(db):
    link(db, "candidate")
    assert trial_context_health(db, "matrix") == []


def test_context_conflicts_are_rejected_without_overwriting(db):
    link(db)
    with pytest.raises(ValueError, match="Conflicting"):
        link(db, "candidate")
    assert len(trial_context_health(db, "matrix")) == 1


def test_missing_individual_namespace_and_cross_study_arm_are_detected(db):
    arm(db, "parent", "fish", "food", 300, "g/week", "6 months", "source")
    put_context(
        db,
        "ng_assay_context",
        {
            "accession": "GSM-test",
            "dataset_id": "matrix",
            "participant_key": "17",
            "arm_id": "parent:fish",
            "tissue": "colon",
            "organism": "Homo sapiens",
            "processing": "source processing",
            "unit_status": "not explicit",
            "release_id": "source",
            "source_ref": "sample",
        },
    )
    errors = validate_context(db)
    assert any("cross-study" in row for row in errors)
    assert any("namespace" in row for row in errors)


def test_untyped_dispersion_is_not_written_as_standard_error(db):
    aggregate(
        db,
        "uncertain",
        "food",
        "endpoint",
        "parent",
        "source",
        "123",
        {
            "endpoint_class": "biomarker",
            "reported_plus_minus": 0.81,
            "reported_plus_minus_type": "unspecified",
            "reported_p": "<0.001",
        },
        -0.61,
        "mmol/L",
    )
    row = db.execute("SELECT * FROM ng_evidence WHERE id='uncertain'").fetchone()
    assert row["standard_error"] is None and row["ci_low"] is None and row["p_value"] is None
    assert json.loads(row["context_json"])["reported_p"] == "<0.001"


def test_reference_join_preserves_zero_and_never_transfers_amount(db):
    for identifier, kind in [
        ("nutrient", "nutrient"),
        ("compound", "compound"),
        ("mechanism_compound", "compound"),
        ("protein", "protein"),
    ]:
        entity(db, identifier, kind, identifier, "test", identifier)
    insert(
        db,
        "ng_evidence",
        {
            "id": "zero-composition",
            "subject_id": "food",
            "object_id": "nutrient",
            "relation": "contains",
            "layer": "composition",
            "release_id": "source",
            "source_ref": "assay",
            "tier": "composition",
            "inference": "descriptive",
            "value_status": "measured_zero",
            "effect": 0,
            "effect_unit": "g/100g",
        },
    )
    for identifier, start, end, relation, method in [
        (
            "USDA-assay-reference:test",
            "nutrient",
            "compound",
            "related",
            json.dumps({"quantity_transfer_allowed": False, "exact_chemical_identity": False}),
        ),
        ("structure", "compound", "mechanism_compound", "related", "different protonation state"),
    ]:
        insert(
            db,
            "ng_mapping",
            {
                "id": identifier,
                "from_id": start,
                "to_id": end,
                "relation": relation,
                "release_id": "source",
                "source_ref": "test",
                "method": method,
                "review_status": "verified",
            },
        )
    insert(
        db,
        "ng_evidence",
        {
            "id": "target",
            "subject_id": "mechanism_compound",
            "object_id": "protein",
            "relation": "cofactor_of",
            "layer": "molecular_target",
            "release_id": "source",
            "source_ref": "test",
            "tier": "curated_mechanism",
            "inference": "descriptive",
            "value_status": "qualitative",
        },
    )
    row = analyte_reference_paths(db)[0]
    assert row["composition_value"] == 0 and row["value_status"] == "measured_zero"
    assert row["compound_amount"] is None and row["quantity_transfer_allowed"] is False
    assert (
        row["health_effect_attribution"] is False and row["participant_measurement_join"] is False
    )


def test_arm_means_and_abstract_summary_do_not_duplicate_quantitative_contrast(db):
    link(db)
    aggregate(
        db,
        "qualitative-summary",
        "food",
        "endpoint",
        "parent",
        "source",
        "123",
        {"endpoint_class": "tissue_surrogate", "direction": "decreased"},
    )
    with db:
        db.execute(
            "INSERT INTO ng_evidence (id,subject_id,object_id,relation,layer,study_id,release_id,source_ref,tier,inference,effect,effect_unit,value_status,context_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "arm-mean",
                "food",
                "endpoint",
                "reported_arm_mean",
                "health_outcome",
                "parent",
                "source",
                "PMID:123#T2",
                "human_randomized",
                "descriptive",
                2.0,
                "cells/crypt",
                "measured",
                '{"endpoint_class":"tissue_surrogate"}',
            ),
        )
    rows = trial_context_health(db, "matrix")
    assert len(rows) == 1
    assert rows[0]["aggregate_evidence_id"] == "null-contrast"
    assert rows[0]["corroborating_qualitative_evidence_ids"] == ["qualitative-summary"]
