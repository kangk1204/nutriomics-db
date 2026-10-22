"""Source checks cannot promote an excluded human declaration into primary evidence."""
import json
import sqlite3

import pytest

from nutrigenomics.composed_science import annotate_matches, record_context, selector_context
from nutrigenomics.reporting_review import digest


@pytest.mark.parametrize("decision", ["pending", "accept", "exclude", "correct", "unconfirmed"])
@pytest.mark.parametrize("eligible", [True, False])
def test_source_checks_and_human_disposition_restrict_all_read_paths(decision, eligible):
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE curation_record_digest(kind TEXT,id TEXT,record_sha256 TEXT);
        CREATE TABLE scientific_review_decision(kind TEXT,id TEXT,record_sha256 TEXT,decision_json TEXT);
        CREATE TABLE source_classification(kind TEXT,id TEXT,record_sha256 TEXT,metadata_json TEXT);
        CREATE TABLE science_record(kind TEXT,id TEXT,metadata_json TEXT);
    """)
    records = {("study", "S"): {"id": "S"}, ("contrast", "C"): {
        "id": "C", "study_id": "S", "series_accession": "GSE-test", "contrast": "after-before", "analysis_version": "v1"}}
    for (kind, identifier), value in records.items():
        db.execute("INSERT INTO science_record VALUES(?,?,?)", (kind, identifier, json.dumps(value)))
        db.execute("INSERT INTO curation_record_digest VALUES(?,?,?)", (kind, identifier, digest(value)))
    sha = digest(records[("study", "S")])
    db.execute("INSERT INTO scientific_review_decision VALUES(?,?,?,?)", ("study", "S", sha, json.dumps({"decision": decision})))
    db.execute("INSERT INTO source_classification VALUES(?,?,?,?)", ("study", "S", sha, json.dumps({
        "classification": "source_only", "source_checked": True, "primary_dietary_evidence_eligible": eligible})))
    restrictive = decision in {"exclude", "correct", "unconfirmed"} or not eligible
    study = record_context(db, "study", "S")
    assert (study.get("primary_dietary_evidence_eligible") is False) == restrictive
    assert study["scientific_approval"] == (decision == "accept")
    matches = annotate_matches(db, [{"study_id": "S", "contrasts": [{"id": "C", "effect": None, "p_value": .9}]}])
    assert matches[0]["primary_dietary_evidence_eligible"] == (not restrictive)
    contrast = matches[0]["contrasts"][0]
    assert (contrast.get("primary_dietary_evidence_eligible") is False) == restrictive
    assert contrast["effect"] is None and contrast["p_value"] == .9
    selector = selector_context(db, "GSE-test", "after-before", "v1")
    assert (selector["curated_contrasts"][0]["study_review"].get("primary_dietary_evidence_eligible") is False) == restrictive
    db.close()
