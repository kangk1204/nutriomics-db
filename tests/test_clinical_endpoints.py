"""Clinical source facts are descriptive evidence, never inferred efficacy."""
import copy
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from nutrigenomics.clinical_endpoints import (
    attach_clinical_endpoints,
    load_clinical_config,
    study_clinical_summary,
    validate_clinical_config,
)
from nutrigenomics.explorer_build import SCHEMA, canonical
from nutrigenomics.workbench_build import WORKBENCH_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "clinical_endpoints.v1.json"


@pytest.fixture
def facts():
    return load_clinical_config(CONFIG)


@pytest.fixture
def db(facts):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.executescript("CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT,PRIMARY KEY(kind,id));"
                     "CREATE TABLE effect(id TEXT,value REAL);")
    db.execute("INSERT INTO effect VALUES('protected-effect',0.123)")
    for audit in facts["study_audits"]:
        study = audit["study_id"]
        db.execute("INSERT INTO science_record VALUES(?,?,?)", (study, "study", canonical({"id": study})))
    for study in ["S03", "S04", "S05", "N15"]:
        identifier = study + ":exposure"
        db.execute("INSERT INTO science_record VALUES(?,?,?)", (identifier, "exposure", canonical({"id": identifier, "study_id": study})))
        db.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", ("Exposure:" + identifier, "diet", identifier, identifier, "[]", "{}", "[]", 0))
    yield db
    db.close()


def test_scope_and_pending_review_are_explicit(facts):
    assert len(facts["records"]) == 44
    assert len(facts["study_audits"]) == 9
    assert {r["study_id"] for r in facts["records"]} == {"S03", "S04", "S05", "N15"}
    assert {r["expert_review_status"] for r in facts["records"]} == {"pending"}
    assert all(a["not_measured"] is None for a in facts["study_audits"])
    assert all(not a["coverage_is_exhaustive"] for a in facts["study_audits"])


def test_source_hashes_and_specific_ccby_permissions(facts):
    raw = ROOT / "outputs" / "publication" / "2026-10-19" / "clinical-sources"
    if not raw.is_dir():
        pytest.skip("raw OA originals are in the scientific audit package, not required by the code-only distribution")
    for source in facts["sources"]:
        p = raw / source["source_file"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == source["sha256"]
        if p.suffix == ".xml":
            permissions = ET.tostring(ET.parse(p).find(".//permissions"), encoding="unicode")
            assert "creativecommons.org/licenses/by/4.0" in permissions
        assert source["license"] == "CC-BY-4.0"
        assert source["rights_location"]


def test_baseline_is_not_an_eight_week_treatment_effect(facts):
    r = next(r for r in facts["records"] if r["study_id"] == "S03" and r["endpoint"]["id"] == "ClinicalEndpoint:systolic_blood_pressure")
    assert r["result_type"] == "baseline_summary"
    assert r["n_people"] == 13
    assert r["observations"][0]["value"] == 115
    assert r["observations"][0]["dispersion"] == {"kind": "range", "lower": 103, "upper": 124}
    assert r["statistical_tests"] == []
    assert r["clinical_effect_estimate"] is None


def test_preserve_omnibus_and_adjusted_pairwise_pvalues(facts):
    r = next(r for r in facts["records"] if r["study_id"] == "S05" and r["endpoint"]["id"] == "ClinicalEndpoint:bmi")
    assert [x["value"] for x in r["observations"]] == [30.63, 29.66, 29.16]
    assert r["statistical_tests"][0]["p_value"] == .033
    tests = [x for x in r["statistical_tests"] if x["comparison_scope"] == "T1 versus T3"]
    assert [(x["adjusted"], x["p_value"]) for x in tests] == [(False, .075), (True, .226)]
    assert r["standard_error"] is None and r["confidence_interval"] is None
    assert r["observations"][0]["observed_n"] is None
    assert "impute" in r["n_people_scope"]


def test_nonsignificant_is_not_numerical_zero_or_improvement(facts):
    r = next(r for r in facts["records"] if r["study_id"] == "S04" and ":hdl_cholesterol:nuts:" in r["id"])
    assert [o["value"] for o in r["observations"]] == [60.9, 64.6]
    assert r["statistical_tests"][0]["reported_label"] == "ns"
    assert r["statistical_tests"][0]["p_value"] is None
    assert r["statistical_tests"][2]["p_value"] == .022
    assert r["statistical_tests"][2]["comparison_scope"] == "three-arm omnibus difference at 5 years"
    assert r["statistical_tests"][3]["p_value"] is None
    assert r["clinical_effect_estimate"] is None


def test_yoghurt_dose_clinical_n_crossover_order_and_iauc_unit(facts):
    r = next(r for r in facts["records"] if r["study_id"] == "N15" and "hdl_cholesterol" in r["id"])
    assert r["pmid"] == "29489876"
    assert r["comparison"]["dose_g"] == 800
    assert r["n_people"] == 13
    assert [o["n_people"] for o in r["observations"]] == [6, 6, 7, 7]
    assert r["observations"][1]["value"] == -8.3
    assert r["observations"][3]["dispersion"]["lower"] == 0
    assert r["endpoint"]["canonical_unit"] is None
    assert "time unit" in r["endpoint"]["unit_caveat"]
    assert r["statistical_tests"][1]["p_value"] == .612


def test_attach_preserves_effects_measurements_and_original_science(db, facts):
    before_effect = [tuple(r) for r in db.execute("SELECT * FROM effect")]
    before_science = [tuple(r) for r in db.execute("SELECT * FROM science_record ORDER BY kind,id")]
    forbidden = {"measurement", "effect", "effect_selector", "effect_ref", "contrast_selector"}
    def authorizer(action, arg1, arg2, dbname, trigger):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and arg1 in forbidden:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.set_authorizer(authorizer)
    summary = attach_clinical_endpoints(db, facts)
    db.set_authorizer(None)
    assert summary["records"] == 44 and summary["health_endpoint_nodes"] == 13
    assert [tuple(r) for r in db.execute("SELECT * FROM effect")] == before_effect
    assert [tuple(r) for r in db.execute("SELECT * FROM science_record WHERE kind IN ('study','exposure') ORDER BY kind,id")] == before_science
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("SELECT count(*) FROM node WHERE kind='outcome'").fetchone()[0] == 13
    for row in db.execute("SELECT metadata_json FROM node WHERE kind='outcome'"):
        metadata = json.loads(row[0])
        assert metadata["clinical_endpoints"] and metadata["expert_review_status"] == "pending"
    assert db.execute("SELECT count(*) FROM edge WHERE subject LIKE 'NCBIGene:%' OR object LIKE 'NCBIGene:%'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM edge_domain").fetchone()[0] > 44


def test_study_helper_returns_sourcefacts_not_aggregate_patient_numbers(db, facts):
    assert study_clinical_summary(db, "S03")["clinical_endpoints"] == []
    attach_clinical_endpoints(db, facts)
    out = study_clinical_summary(db, "S03")
    assert len(out["clinical_endpoints"]) == 7
    assert out["review_summary"]["human_approved_records"] == 0
    n06 = study_clinical_summary(db, "N06")
    assert n06["clinical_endpoints"] == []
    assert n06["clinical_endpoint_audit"]["status"] == "abstract_only_quantitative_semantics_not_audited"
    assert n06["clinical_endpoint_audit"]["not_measured"] is None
    assert study_clinical_summary(db, "missing")["review_summary"]["availability_reason"] == "clinical_study_not_audited"
    with pytest.raises(ValueError, match="already installed"):
        attach_clinical_endpoints(db, facts)


def test_helper_handles_missing_schema():
    db = sqlite3.connect(":memory:")
    try:
        assert study_clinical_summary(db, "S01")["review_summary"]["availability_reason"] == "clinical_layer_not_installed"
    finally:
        db.close()


def test_workbench_clinical_rows_paginate_without_fake_scalar_statistics(db, facts, monkeypatch):
    from contextlib import contextmanager

    from nutrigenomics import explorer_workbench

    db.executescript(WORKBENCH_SCHEMA)
    summary = attach_clinical_endpoints(db, facts)
    assert summary["workbench_clinical_rows"] == 44
    assert db.execute("SELECT count(*) FROM workbench_evidence WHERE layer='clinical'").fetchone()[0] == 44
    assert {r[0] for r in db.execute("SELECT review_status FROM workbench_evidence")} == {"source_checked_expert_pending"}

    @contextmanager
    def local_database():
        yield db

    monkeypatch.setattr(explorer_workbench, "database", local_database)
    # A study-scoped query uses the native pagination route, independent of the
    # release-specific optional large-table query sidecar.
    pages = [explorer_workbench._query("ClinicalEndpoint:hdl_cholesterol", "clinical", None, "S04", None, "", n, 2)
             for n in (1, 2, 3)]
    assert [len(p["items"]) for p in pages] == [2, 1, 0]
    assert {p["total"] for p in pages} == {3}
    items = [item for p in pages for item in p["items"]]
    assert len({item["id"] for item in items}) == 3
    for item in items:
        assert item["export_allowed"] is True and item["expert_review_status"] == "pending"
        assert item["effect"] is None and item["p_value"] is None and item["q_value"] is None
        assert item["metadata"]["standard_error"] is None
        assert item["metadata"]["clinical_endpoints"][0]["statistical_tests"]
        assert item["source_ref"] and item["metadata"]["source_references"]
        assert item["n_people"] == 12
        assert "scope" in item["hypothesis_family"]


@pytest.mark.parametrize("mutation,match", [
    (lambda c: c.update(expert_review_status="approved"), "pending"),
    (lambda c: c.update(human_approved_records=44), "human approval"),
    (lambda c: c["records"][0].update(genomic_mediation_demonstrated=True), "mediation"),
    (lambda c: c["records"][0].update(standard_error=1.2), "uncertainty"),
    (lambda c: c["records"][0].update(clinical_effect_estimate=-3.2), "summary differences"),
    (lambda c: c["records"][0]["observations"][0].update(value=float("nan")), "finite"),
    (lambda c: c["records"][0]["observations"][0].update(summary_statistic="mean"), "median range"),
    (lambda c: c["sources"][0].update(export_allowed=False), "rights"),
    (lambda c: c["study_audits"][0].update(not_measured=True), "not measured"),
])
def test_scientific_error_guards(facts, mutation, match):
    bad = copy.deepcopy(facts)
    mutation(bad)
    with pytest.raises(ValueError, match=match):
        validate_clinical_config(bad)
