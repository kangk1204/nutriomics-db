"""Classification proposals cannot mutate evidence, grant approval or survive drift."""
import copy
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from nutrigenomics.reporting_review import (
    build_review_package,
    prepare_review_records,
    read_science_records,
    validate_review_decisions,
)
from nutrigenomics.submission_upgrade import (
    analysis_resolution_queue,
    build_review_dependencies,
    build_source_classifications,
    science_inventory,
)


def fixture(tmp_path):
    content = b"Original source snapshot"
    (tmp_path / "source.txt").write_bytes(content)
    source = {"science_records": [
        {"id": "S01", "kind": "study", "record": {"id": "S01", "metadata": {"series": ["GSE127530"]}}},
        {"id": "S05", "kind": "study", "record": {"id": "S05", "metadata": {"series": ["GSE219217"]}}},
        {"id": "1", "kind": "analysis_run", "record": {"series_accession": "GSE127530", "analysis_version": "v1"}},
        {"id": "5", "kind": "analysis_run", "record": {"series_accession": "GSE219217", "analysis_version": "v1"}},
        {"id": "clinical", "kind": "clinical_endpoint_result", "record": {"study_id": "S05", "value": 0}},
        {"id": "source", "kind": "bridge_source", "record": {"id": "source"}},
    ]}
    rows = read_science_records(source)
    def packet(task, sid, proofs):
        return {"task_id": task, "source_references": [
            {"proof_id": proof, "path": "source.txt", "sha256": hashlib.sha256(content).hexdigest(),
             "locator_checked": True, "source_url": "https://example.org/paper", "location": "Methods"}
            for proof in proofs], "record_mapping": [
                {k: r[k] for k in ["record_kind", "record_id", "record_sha256"]} for r in rows],
            "findings": [{"statement_ko": "실제실행모델행렬 미저장"}]}
    packets = [packet("STUDY:S01", "S01", ["GEO:S01", "P:S01"]),
               packet("STUDY:S05", "S05", ["P:S05:extraction", "P:S05:controls"]),
               packet("METHOD:1", "S01", []), packet("METHOD:5", "S05", [])]
    return source, packets


def test_classifications_are_separate_from_numeric_clinical_observations(tmp_path):
    source, packets = fixture(tmp_path)
    before = copy.deepcopy(source)
    result = build_source_classifications(source, packets, tmp_path)
    assert source == before and result["automatic_expert_approval"] is False
    assert len(result["records"]) == 4
    assert all(r["source_checked"] and not r["scientific_approval"] for r in result["records"])
    assert all(r["expert_review_status"] == "pending" for r in result["records"])
    assert not any(r["record_id"] == "clinical" for r in result["records"])


def test_classification_rejects_changed_proof_or_changed_record(tmp_path):
    source, packets = fixture(tmp_path)
    (tmp_path / "source.txt").write_bytes(b"different source")
    with pytest.raises(ValueError, match="proof changed"):
        build_source_classifications(source, packets, tmp_path)
    source, packets = fixture(tmp_path)
    source["science_records"][0]["record"]["title"] = "new record"
    with pytest.raises(ValueError, match="stale"):
        build_source_classifications(source, packets, tmp_path)


def test_classification_rejects_missing_and_unchecked_sources(tmp_path):
    source, packets = fixture(tmp_path)
    packets[0]["source_references"][0]["locator_checked"] = False
    with pytest.raises(ValueError):
        build_source_classifications(source, packets, tmp_path)


def test_duplicate_required_proof_cannot_stand_in_for_a_missing_proof(tmp_path):
    source, packets = fixture(tmp_path)
    packet = packets[1]
    packet["source_references"][1] = copy.deepcopy(packet["source_references"][0])
    with pytest.raises(ValueError, match="Missing or duplicate"):
        build_source_classifications(source, packets, tmp_path)


def test_analysis_queue_is_proposal_not_rerun_or_expert_decision(tmp_path):
    source, packets = fixture(tmp_path)
    q = analysis_resolution_queue(source, packets)
    assert q["counts"] == {"reexecution_needed_for_complete_model_archive": 1, "exploratory_only": 1}
    assert all(r["human_decision"] == "pending" and not r["numerical_error_established"] for r in q["analyses"])
    packets[-1]["record_mapping"] = []
    with pytest.raises(ValueError, match="stale"):
        analysis_resolution_queue(source, packets)


def test_inventory_names_provenance_without_fabricating_people(tmp_path):
    source, _ = fixture(tmp_path)
    r = science_inventory(source)
    assert r["records"] == 6 and r["core_review_records"] == 5
    assert r["outside_core_by_kind"] == {"bridge_source": 1}
    assert r["counts_are_independent_people"] is False


def test_unchanged_record_requires_new_review_when_source_bytes_change(tmp_path):
    source, packets = fixture(tmp_path)
    dependencies = build_review_dependencies(source, packets, tmp_path)
    row = prepare_review_records(source)[0]
    bound = next(r for r in dependencies["records"] if r["record_kind"] == row["record_kind"] and r["record_id"] == row["record_id"])
    row.update(decision="accept", reviewer="Fixture human", reviewer_type="human", reviewer_attestation=True,
               reviewed_at=(datetime.now(UTC)-timedelta(minutes=1)).isoformat(),
               primary_paper_location="Methods", reason="Synthetic test declaration only",
               source_dependency_sha256=bound["source_dependency_sha256"])
    assert validate_review_decisions(source, [row], dependencies=dependencies)["expert_approved"] == 1
    new = b"Changed original source version"
    (tmp_path / "source.txt").write_bytes(new)
    for p in packets:
        for ref in p["source_references"]:
            ref["sha256"] = hashlib.sha256(new).hexdigest()
    changed = build_review_dependencies(source, packets, tmp_path)
    with pytest.raises(ValueError, match="source dependencies"):
        validate_review_decisions(source, [row], dependencies=changed)
    package = build_review_package(source, tmp_path / "review", dependencies=changed)
    assert package["expert_verified"] == 0 and package["source_dependencies_sha256"]
    html = (tmp_path / "review/scientific-review.html").read_text(encoding="utf-8")
    assert "original.source_dependency_sha256" in html


def test_preserved_original_producer_must_match_exact_hash_cache(tmp_path):
    source, packets = fixture(tmp_path)
    before = build_review_dependencies(source, packets, tmp_path)
    original = (tmp_path / "source.txt").read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    saved = tmp_path / "source_snapshots/review_evidence" / sha / "source.txt"
    saved.parent.mkdir(parents=True); saved.write_bytes(original)
    (tmp_path / "source.txt").write_bytes(b"evolved working source")
    result = build_source_classifications(source, packets, tmp_path)
    assert result["records"][0]["source_references"][0]["source_resolution"] == "preserved_original_hash_cache"
    assert build_review_dependencies(source, packets, tmp_path) == before
    (tmp_path / "source.txt").unlink()
    assert build_review_dependencies(source, packets, tmp_path) == before
    saved.write_bytes(b"wrong archived bytes")
    with pytest.raises(ValueError, match="proof changed"):
        build_source_classifications(source, packets, tmp_path)


def test_offline_review_export_and_import_preserve_source_binding(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable for actual offline review JavaScript roundtrip")
    source, packets = fixture(tmp_path)
    dependencies = build_review_dependencies(source, packets, tmp_path)
    build_review_package(source, tmp_path / "ui", dependencies=dependencies)
    html = (tmp_path / "ui/scientific-review.html").read_text(encoding="utf-8")
    functions = "\n".join(line for line in html.splitlines() if line.startswith(
        ("const fields=", "function savedRows()", "function applySaved(")))
    assert functions.count("function ") == 2
    row = {"record_kind": "study", "record_id": "S01", "record_sha256": "a"*64,
           "source_dependency_sha256": "b"*64, "decision": "accept", "reviewer": "Fixture human",
           "reviewer_type": "human", "reviewer_attestation": True, "reviewed_at": "2026-01-01T00:00:00Z",
           "primary_paper_location": "Methods", "reason": "Synthetic browser behavior test", "proposed_correction": None}
    script = "const assert=require('node:assert/strict');const labels={accept:'a',pending:'p'};const $=()=>({});\n"
    script += "let rows=" + json.dumps([row]) + ";\n" + functions + "\n"
    script += "const saved=JSON.parse(JSON.stringify(savedRows()));assert.equal(saved[0].source_dependency_sha256,rows[0].source_dependency_sha256);applySaved(saved);rows[0].source_dependency_sha256='c'.repeat(64);assert.throws(()=>applySaved(saved),/재검수/);console.log('offline review export/import dependency guard passed');"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 0, result.stderr
