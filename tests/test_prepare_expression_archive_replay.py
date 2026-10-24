"""Planner must select exact bounded inputs without running/copying anything."""
from copy import deepcopy
from pathlib import PurePosixPath

import pytest

from scripts.prepare_expression_archive_replay import RUNS, build_plan


@pytest.fixture
def fixture(tmp_path):
    root = "/approved/project/rebuild/old"
    inventory = {"source_root": root, "runs": [], "directories": []}
    small = {"files": []}
    for name, (study, route, producer) in RUNS.items():
        folder = root + "/outputs/" + name
        metadata = {"accession": study, "analysis_version": name, "script_sha256": "1"*64,
                    "input_manifest": []}
        if study == "GSE56960":
            metadata["input_manifest"] = [{"path": root + f"/data/processed/geo/{study}/raw_files/GSM{i}.cel.gz",
                "sha256": "a"*64, "bytes": 1} for i in range(168)]
        if study == "GSE54325":
            metadata["input_manifest"] = [{"path": root + f"/data/processed/geo/{study}/raw_files/GSM{i}_Volunteer{i}.txt.gz",
                "sha256": "b"*64, "bytes": 1} for i in range(7)]
        done = [{"path": "analysis_metadata.json", "sha256": "2"*64, "bytes": 100},
                {"path": "gene_effects.tsv.gz", "sha256": "4"*64, "bytes": 100},
                {"path": "source_snapshot/"+producer, "sha256": "1"*64, "bytes": 100}]
        if route == "saved_normalized_model_replay":
            done.append({"path": "reanalysis_inputs.rds", "sha256": "5"*64, "bytes": 100})
        inventory["runs"].append({"folder": folder, "metadata": metadata,
            "metadata_file": {"path": folder+"/analysis_metadata.json", "exists": True, "bytes": 100, "sha256": "2"*64},
            "done_file": {"path": folder+"/DONE.json", "exists": True, "bytes": 100, "sha256": "3"*64},
            "source_snapshot": [{"path": folder+"/source_snapshot/"+producer, "exists": True, "bytes": 100, "sha256": "1"*64}],
            "input_stats": [{"path": r["path"], "exists": True, "bytes": 1} for r in metadata["input_manifest"]],
            "output_stats": [{"path": folder+"/"+r["path"], "exists": True, "bytes": r["bytes"]} for r in done],
            "done": {"files": done}})
    audit_content = {"status": "verified_fixed_cleanup", "sample_design": {"geo_sample_crosswalk_status": "complete"},
        "selected_analysis_file": "GSE127530_fixed_combinedCounts.txt.gz", "files": []}
    for name, sha in [("GSE127530_fixed_combinedCounts.txt.gz", "6"*64), ("GSE127530_combinedCounts.txt.gz", "7"*64)]:
        small["files"].append({"path": root+"/data/raw/geo/GSE127530/"+name, "exists": True, "sha256": sha, "bytes": 100})
        audit_content["files"].append({"name": name, "compressed_sha256": sha})
    small["files"].append({"path": root+"/outputs/gse127530-v1/gse127530_counts_audit.json",
        "exists": True, "bytes": 100, "sha256": "8"*64, "content": audit_content})
    small["files"].append({"path": root+"/toolchain/r-environment-linux-64.lock.txt",
                            "exists": True, "bytes": 100, "sha256": "9"*64})
    scripts = tmp_path / "scripts"; scripts.mkdir()
    for name in ("analyze_gse127530.R", "analyze_gse56960.R", "analyze_gse54325.R",
                 "expression_archive_helpers.R", "replay_saved_expression_archive.R"):
        (scripts / name).write_text("# Not executed", encoding="utf-8")
    return inventory, small, tmp_path


def plan(fixture):
    inventory, small, code = fixture
    return build_plan(inventory, small, "/approved/project/rebuild/new", code)


def test_exact_selection_deduplicates_raw_and_preserves_no_approval(fixture):
    result = plan(fixture)
    assert result["analysis_run_count"] == 6 and result["study_count"] == 5
    assert result["executed"] is False and result["approval_from_this_plan"] is False
    assert result["scientific_approval"] is False and result["expert_review_status"] == "pending"
    cel = [r for r in result["copy_manifest"] if r["source"].endswith(".cel.gz")]
    assert len(cel) == 168  # Shared by two GSE56960 result families.
    for run in result["runs"]:
        assert run["command_argv"][0] == "/approved/project/toolchain/r-nutrigenomics/bin/Rscript"
        assert run["environment"]["OMP_NUM_THREADS"] == "1"
        if run["route"] == "saved_normalized_model_replay":
            assert "scripts/replay_saved_expression_archive.R" in run["command_argv"]
            assert "--raw-cdf" not in run["command_argv"]
            assert run["preprocessing_repeated"] is False
    assert not result["unresolved_inputs"]
    assert all(PurePosixPath(r["destination"]).is_relative_to("/approved/project/rebuild/new") for r in result["copy_manifest"])


@pytest.mark.parametrize("destination", ["/approved/project/rebuild/old/new", "/elsewhere/new", "/approved/project/rebuild/new/../old"])
def test_unsafe_or_overlapping_destination_rejected(fixture, destination):
    inventory, small, code = fixture
    with pytest.raises(ValueError): build_plan(inventory, small, destination, code)


def test_metadata_completion_hash_drift_rejected(fixture):
    fixture[0]["runs"][0]["metadata_file"]["sha256"] = "f"*64
    with pytest.raises(ValueError, match="immutable completion"): plan(fixture)


def test_count_audit_pending_is_not_implicitly_accepted(fixture):
    next(r for r in fixture[1]["files"] if "content" in r)["content"]["status"] = "manual_review"
    with pytest.raises(ValueError, match="not verified"): plan(fixture)


def test_missing_input_is_unresolved_not_zero_success(fixture):
    fixture[0]["runs"][1]["input_stats"][0]["exists"] = False
    fixture[0]["runs"][2]["input_stats"][0]["exists"] = False
    result = plan(fixture)
    assert result["unresolved_inputs"] and result["executed"] is False


def test_raw_count_and_outside_source_guards(fixture):
    data = deepcopy(fixture)
    data[0]["runs"][1]["metadata"]["input_manifest"].pop()
    with pytest.raises(ValueError, match="168"): plan(data)
    data = deepcopy(fixture)
    data[0]["runs"][1]["metadata"]["input_manifest"][0]["path"] = "/etc/passwd.cel.gz"
    with pytest.raises(ValueError, match="no byte-verified"): plan(data)
