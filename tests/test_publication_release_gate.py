import hashlib
import json
import sqlite3

import pytest

from nutrigenomics.explorer_build import sha256
from nutrigenomics.public_service import create_app
from scripts.submission_readiness import REQUIRED_TECHNICAL, evaluate


def test_final_gate_requires_actual_unchanged_reports(tmp_path):
    metadata = {"release_id": "r", "counts": {"effect_types": {"gene_expression": 1, "cpg": 1}}}
    expected = [{"record_id": "one", "record_sha256": "a"}]
    decision = [{**expected[0], "decision": "accept", "reviewer": "Human reviewer",
                 "reviewed_at": "2026-10-14", "reason": "Source checked",
                 "primary_paper_location": "Methods, table 1"}]
    report = {"release_id": "r", "passed": True, "expert_gold_reviewed": True, "n_queries": 30}
    path = tmp_path / "checked.json"
    path.write_text(json.dumps(report))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    evidence = {
        "technical": {n: {"passed": True, "release_id": "r", "report_path": path.name,
                           "report_sha256": digest} for n in REQUIRED_TECHNICAL},
        "sealed_benchmark": {"expert_gold_reviewed": True, "n_queries": 30,
                             "raw_results_path": path.name, "raw_results_sha256": digest},
        "data_version_doi": "10.5281/zenodo.123", "code_version_doi": "10.5281/zenodo.124",
        "authors_and_credit_confirmed": True, "official_affiliation_confirmed": True,
        "funding_and_conflicts_confirmed": True, "institutional_ethics_determination": True,
        "public_https_url": "https://example.org",
    }
    assert evaluate(metadata, expected, decision, evidence, evidence_root=tmp_path)["submission_ready"]
    # Stale success flags cannot approve replaced reports.
    path.write_text(json.dumps({**report, "passed": False}))
    changed = evaluate(metadata, expected, decision, evidence, evidence_root=tmp_path)
    assert not changed["submission_ready"]
    assert sum(r.get("reason") == "technical_evidence_pending" for r in changed["failures"]) == len(REQUIRED_TECHNICAL)
    # Pure declarations without any evidence directory also fail.
    assert not evaluate(metadata, expected, decision, evidence)["submission_ready"]


def test_status_string_cannot_approve_public_deployment(tmp_path):
    db_path = tmp_path / "nutriomics.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE fixture(id INTEGER)")
    (tmp_path / "dataset-metadata.json").write_text(json.dumps({"release_id": "r", "status": "submission_ready"}))
    (tmp_path / "manifest.json").write_text(json.dumps({"database_sha256": sha256(db_path)}))
    with pytest.raises(ValueError, match="hash-bound release gate"):
        create_app(tmp_path)

