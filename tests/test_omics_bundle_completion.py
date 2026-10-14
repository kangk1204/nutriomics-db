import gzip
import hashlib
import json

import pytest

from scripts.freeze_omics_bundle import freeze, sha256


def fixture_bundle(tmp_path, q="0.5"):
    folder = tmp_path / "output"
    folder.mkdir()
    source = tmp_path / "executed.R"
    source.write_text("# Explicit synthetic completion-gate fixture\n")
    with gzip.open(folder / "gene_effects.tsv.gz", "wt") as handle:
        handle.write("feature_id\tcontrast\tp_value\tq_value\tn_people\tn_samples\n")
        handle.write("FIXTURE\tpaired\t0.4\t" + q + "\t1\t4\n")
    metadata = {"accession": "SYNTHETIC_TEST", "analysis_version": "fixture-v1", "run_utc": "2026-01-01 UTC",
                "script_path": str(source), "script_sha256": sha256(source),
                "result_files": ["gene_effects.tsv.gz"]}
    (folder / "analysis_metadata.json").write_text(json.dumps(metadata))
    return folder, source


def test_completion_freezes_source_and_preserves_done_bytes(tmp_path):
    folder, source = fixture_bundle(tmp_path)
    ready = freeze(folder)
    original_bytes = (folder / "DONE.json").read_bytes()
    source.write_text("# Subsequent mutable working source edit\n")
    assert freeze(folder) == ready
    assert (folder / "DONE.json").read_bytes() == original_bytes
    archived = folder / ready["executed_source_snapshots"][0]["path"]
    assert sha256(archived) == ready["executed_source_snapshots"][0]["sha256"]


def test_modified_result_invalidates_previously_completed_bundle(tmp_path):
    folder, _ = fixture_bundle(tmp_path)
    freeze(folder)
    with gzip.open(folder / "gene_effects.tsv.gz", "wt") as handle:
        handle.write("tampered\n")
    with pytest.raises(AssertionError, match="gene_effects"):
        freeze(folder)


def test_modified_source_before_freeze_cannot_be_misattributed(tmp_path):
    folder, source = fixture_bundle(tmp_path)
    source.write_text("# Changed before archival\n")
    with pytest.raises(AssertionError, match="Executed source has changed"):
        freeze(folder)
    assert not (folder / "DONE.json").exists()


def test_invalid_probability_never_publishes_completion(tmp_path):
    folder, _ = fixture_bundle(tmp_path, q="1.5")
    with pytest.raises(AssertionError):
        freeze(folder)
    assert not (folder / "DONE.json").exists()


def test_missing_q_value_preserved_in_null_summary(tmp_path):
    folder, _ = fixture_bundle(tmp_path, q="NA")
    summary = freeze(folder)["result_summary"]["gene_effects.tsv.gz"]["paired"]
    assert summary["rows"] == 1
    assert summary["joint_fdr_lt_005"] == 0
    assert summary["n_people"] == ["1"]
    assert summary["n_samples"] == ["4"]


def test_single_result_filename_from_jsonlite_auto_unbox(tmp_path):
    folder, _ = fixture_bundle(tmp_path)
    path = folder / "analysis_metadata.json"
    metadata = json.loads(path.read_text())
    metadata["result_files"] = "gene_effects.tsv.gz"
    path.write_text(json.dumps(metadata))
    assert freeze(folder)["result_summary"]["gene_effects.tsv.gz"]["paired"]["rows"] == 1
