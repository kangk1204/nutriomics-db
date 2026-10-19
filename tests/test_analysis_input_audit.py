"""Read-only analysis auditing distinguishes byte identity from validity."""
from __future__ import annotations

import hashlib
import json
import sqlite3

from scripts.audit_analysis_inputs import (
    ArtifactResolver,
    artifact_specs,
    audit_analyses,
    canonical,
    load_science,
    model_findings,
    sample_audit,
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_filename_collision_is_resolved_by_full_hash(tmp_path):
    (tmp_path / "old").mkdir()
    (tmp_path / "fresh").mkdir()
    old = tmp_path / "old" / "input.txt"
    fresh = tmp_path / "fresh" / "input.txt"
    old.write_bytes(b"old")
    fresh.write_bytes(b"new")
    resolver = ArtifactResolver([tmp_path], ["input.txt"])
    result = resolver.verify({"role": "input", "filename": "input.txt", "sha256": digest(fresh)})
    assert result["status"] == "verified"
    assert result["verified_path"] == str(fresh)
    assert result["candidate_paths_count"] == 2
    assert result["resolved_by"] == "full_content_hash_not_basename"


def test_exact_declared_path_mismatch_cannot_be_rescued_by_other_copy(tmp_path):
    source = tmp_path / "input.txt"
    source.write_bytes(b"changed")
    archive = tmp_path / "archive"
    archive.mkdir()
    good = archive / "input.txt"
    good.write_bytes(b"expected")
    resolver = ArtifactResolver([tmp_path], ["input.txt"])
    result = resolver.verify({"role": "input", "filename": "input.txt", "declared_path": str(source), "sha256": digest(good)})
    assert result["status"] == "mismatch_or_unreadable"
    assert result["verified_path"] is None


def test_absence_unknown_hash_wrong_unbound_version_and_budget_are_distinct(tmp_path):
    source = tmp_path / "input.txt"
    source.write_bytes(b"some other version")
    resolver = ArtifactResolver([tmp_path], ["input.txt"])
    assert resolver.verify({"filename": "absent", "sha256": "a" * 64})["status"] == "unavailable"
    assert resolver.verify({"filename": "input.txt", "sha256": None})["status"] == "expected_hash_missing_or_invalid"
    assert resolver.verify({"filename": "input.txt", "sha256": "a" * 64})["status"] == "expected_content_unavailable"
    limited = ArtifactResolver([tmp_path], ["input.txt"], max_hash_bytes=1)
    assert limited.verify({"filename": "input.txt", "sha256": digest(source)})["status"] == "not_checked_hash_budget"


def test_manifest_keeps_declared_path_and_special_count_input():
    value = {"source_script_sha256": "a" * 64, "environment_lock_sha256": "b" * 64,
             "metadata": {"script_path": {"filename": "run.R"}},
             "input_manifest": [{"input_file": "counts.txt.gz", "input_file_sha256": "c" * 64, "input_compressed_bytes": 10}],
             "output_manifest": [{"path": "/approved/result.tsv.gz", "sha256": "d" * 64, "bytes": 20}]}
    specs = artifact_specs(value)
    inp = next(s for s in specs if s["role"] == "input")
    assert inp["filename"] == "counts.txt.gz" and inp["bytes"] == 10
    out = next(s for s in specs if s["role"] == "output")
    assert out["declared_path"] == "/approved/result.tsv.gz"


def verified_table(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return {"role": "output", "filename": name, "status": "verified", "sha256": digest(path), "verified_path": str(path)}


def test_actual_table_counts_and_repeated_cells_not_merely_metadata_keys(tmp_path):
    spec = verified_table(tmp_path, "sample_metadata.tsv", "sample_accession\tsubject_id\ttimepoint\nA\tP1\t0\nB\tP1\t6\nC\tP2\t0\nD\tP2\t6\n")
    analysis = {"metadata": {"sample_count": 4, "independent_people": 2, "samples_per_person": {"P1": 2, "P2": 2}}}
    tables, findings = sample_audit(analysis, [spec])
    assert tables[0]["observed_people"] == 2 and tables[0]["included_rows"] == 4
    assert tables[0]["duplicate_person_condition_cells"] == 0
    assert not findings
    analysis["metadata"]["independent_people"] = 4
    _, findings = sample_audit(analysis, [spec])
    assert any(f["code"] == "person_count_disagrees_with_verified_table" for f in findings)


def test_partial_person_exclusion_and_missing_declared_exclusion_detected(tmp_path):
    spec = verified_table(tmp_path, "sample_qc.tsv", "sample_accession\tsubject_id\ttimepoint\tincluded\nA\tP1\t0\tTRUE\nB\tP1\t6\tFALSE\nC\tP2\t0\tTRUE\nD\tP2\t6\tTRUE\n")
    _, findings = sample_audit({"metadata": {"sample_count": 3, "independent_people": 2, "excluded_people": []}}, [spec])
    codes = {f["code"] for f in findings}
    assert {"partial_person_exclusion", "excluded_people_disagree_with_verified_qc"} <= codes


def test_person_metadata_is_not_mistaken_for_assay_count(tmp_path):
    spec = verified_table(tmp_path, "person_metadata.tsv", "subject_id\tdiet_arm\nP1\tA\nP2\tB\n")
    tables, findings = sample_audit({"metadata": {"sample_count": 4, "independent_people": 2}}, [spec])
    assert tables[0]["scope"] == "person_rows"
    assert not any(f["code"] == "assay_count_disagrees_with_verified_table" for f in findings)


def test_verified_two_color_sample_directions_are_compared(tmp_path):
    labels = json.dumps({"label_ch1": ["Cy3"], "label_ch2": ["Cy5"]})
    wrong = json.dumps({"source_name_ch1": ["8 weeks"], "source_name_ch2": ["time 0"]})
    spec = verified_table(tmp_path, "sample_metadata.tsv", "sample_accession\tsubject_id\tchannel_labels_json\tchannel_source_names_json\nA\tP1\t" + labels + "\t" + wrong + "\n")
    _, findings = sample_audit({"series_accession": "GSE54325", "metadata": {"sample_count": 1, "independent_people": 1}}, [spec])
    assert any(f["code"] == "two_color_channel_direction_mismatch" for f in findings)


def test_concrete_source_and_multiplicity_limits_are_not_expert_approval():
    a = {"series_accession": "GSE54643", "metadata": {"multiple_testing": "BH within each contrast"}}
    codes = {f["code"] for f in model_findings(a, "standard_error = beta_summary$standard_error")}
    assert {"within_contrast_fdr_only", "beta_se_descriptive_m_test_separate", "processed_signals_no_idat_qc"} <= codes
    a = {"analysis_version": "v4-regions-fixed1000bp-v1", "metadata": {"derived_analysis": "FDR corrected"}}
    codes = {f["code"] for f in model_findings(a, "")}
    assert {"fixed_tiles_not_segmented_dmr", "postprocessor_inherits_parent_models", "rds_model_matrices_not_recomputed"} <= codes


def test_invalid_declared_design_is_an_error():
    a = {"metadata": {"design_audit": {"rank": 2, "columns": 3, "people": 3, "residual_df": 5}}}
    findings = model_findings(a, "")
    assert sum(f["severity"] == "error" for f in findings) == 2


def test_absent_files_yield_partial_not_pass_and_exact_count_enforced(tmp_path):
    analysis = {"record_id": "1", "value": {"series_accession": "GSE-X", "analysis_version": "v1", "source_script_sha256": "a" * 64,
                                             "metadata": {"script_path": {"filename": "run.R"}}}}
    result = audit_analyses([analysis], roots=[tmp_path], expected_count=1)
    assert result["status"] == "partial_unavailable_evidence"
    assert result["summary"]["source_producer_verified"] == 0
    assert result["scientific_approval"] is False and result["journal_submission_ready"] is False
    assert audit_analyses([analysis], roots=[tmp_path], expected_count=14)["status"] == "failed"


def test_read_only_database_load_preserves_original_bytes(tmp_path):
    p = tmp_path / "science.sqlite"
    with sqlite3.connect(p) as db:
        db.executescript("CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT);CREATE TABLE meta(key TEXT,value_json TEXT);")
        db.execute("INSERT INTO science_record VALUES(?,?,?)", ("1", "analysis_run", canonical({"analysis_version": "v1"})))
        db.execute("INSERT INTO meta VALUES('release_id','\"release-test\"')")
    before = digest(p)
    release, records = load_science(database=p)
    assert release == "release-test" and records[0]["value"]["analysis_version"] == "v1"
    assert digest(p) == before


def test_read_only_json_load_withholds_no_failures(tmp_path):
    p = tmp_path / "science.json"
    p.write_text(json.dumps({"release_id": "release", "science_records": [{"id": "1", "kind": "analysis_run", "metadata_json": json.dumps({"analysis_version": "v1"})}]}), encoding="utf-8")
    release, records = load_science(science_json=p)
    assert release == "release" and records[0]["record_id"] == "1"
