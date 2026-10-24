import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nutrigenomics.pathway_selection import (
    PARENTS,
    SUFFIX,
    PathwaySelection,
    _bh,
    build_overlay,
    file_sha256,
)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return file_sha256(path)


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    parent_runs, parquet_runs, diagnostics = [], [], {}
    pending = {"scientific_approval": False, "expert_review_status": "pending",
               "historical_reproducibility_resolved": False, "public_results_modified": False,
               "new_fgsea_execution_performed": False}
    for aid, (old, family, n_contrasts) in PARENTS.items():
        rows = []
        for k in range(n_contrasts):
            for index, (p, q, nes) in enumerate(((.01, .02, 1.5), (.2, .2, -.8),
                                                (None, None, None))):
                edge = ["1;2", "3", ""][index]
                rows.append({
                    "series_accession": "GSE56960", "feature_type": "pathway",
                    "feature_id": f"R-HSA-{index}", "contrast": f"contrast-{k}",
                    "analysis_version": old + SUFFIX, "effect_estimate": nes,
                    "p_value": p, "q_value_contrast": q,
                    "standard_error": None, "n_people": 7, "n_samples": 168,
                    "leading_edge": edge, "pathway_size": 20,
                    "fgsea_log2err": .1 if p is not None else None,
                    "status": "measured" if p is not None else "not_estimable",
                    "metadata_json": json.dumps({
                        "historical_original_checkpoint_recovered": False,
                        "primary_q_family": family, "leading_edge": edge}),
                })
        adjusted = _bh([r["p_value"] for r in rows], len(rows))
        for row, q in zip(rows, adjusted, strict=True):
            row["q_value_analysis_family"] = q
            row["q_value"] = row["q_value_contrast"] if family == "engine_contrast" else q
            if family == "analysis_family":
                row["q_value_study_family"] = q
        table = pa.Table.from_pylist(rows)
        table = table.set_column(table.column_names.index("standard_error"), "standard_error",
                                 pa.array([None] * len(rows), type=pa.float64()))
        path = root / f"analysis-{aid}/pathway_effects.parquet"
        path.parent.mkdir()
        pq.write_table(table, path)
        receipt = {
            "checks": {"same_checkpoint_twice": {"exact_result_objects_identical": True,
                                                  "exact_rng_traces_identical": True}},
            "reference_exact_historical_t_available": False, "scientific_approval": False,
            "expert_review_status": "pending", "upstream_sources_unchanged": True,
            "raw_results_sha256": "b" * 64,
            "source_inputs": {"checkpoint": {"sha256": "c" * 64}},
            "upstream_sources": {"checkpoint": [{"path": "/private/internal/membership.rds",
                                                    "role": "pathway_mapping", "sha256": "d" * 64}]},
            "environment": {"package_versions": {"reactome.db": "1.95.0"}},
        }
        d_path = tmp_path / f"diagnostic-{aid}.json"
        d_sha = dump(d_path, receipt)
        diagnostics[aid] = d_path
        parent_runs.append({
            "analysis_id": aid, "series_accession": "GSE56960", "parent_analysis_version": old,
            "analysis_version": old + SUFFIX, "parent_record_sha256": "a" * 64,
            "primary_q_family": family, "rows": len(rows), "contrasts": n_contrasts,
            "q_threshold_changes": 2 if aid == 2 else 0,
            "input_artifacts": {"diagnostic_receipt": {"sha256": d_sha},
                                "raw_results": {"sha256": "b" * 64},
                                "checkpoint": {"sha256": "c" * 64},
                                "original_pathways": {"sha256": "e" * 64}},
        })
        parquet_runs.append({
            "analysis_id": aid, "analysis_version": old + SUFFIX,
            "parent_record_sha256": "a" * 64, "fdr_family": family,
            "rows": len(rows), "contrasts": n_contrasts, "exact_parquet_roundtrip": True,
            "path": path.relative_to(root).as_posix(), "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
            "null_counts": {n: table[n].null_count for n in table.column_names},
        })
    parent_path = tmp_path / "original-manifest.json"
    parent_sha = dump(parent_path, {
        "schema": "nutriomics-pathway-checkpoint-candidate-v1",
        "parent_release_id": "fixture-release", "runs": parent_runs, **pending,
    })
    parquet_sha = dump(root / "manifest.json", {
        "schema": "nutriomics-pathway-parquet-candidate-v1", "parent_manifest_sha256": parent_sha,
        "parent_release_id": "fixture-release", "runs": parquet_runs,
        "rows": sum(r["rows"] for r in parquet_runs), **pending,
    })
    return {"parquet_root": root, "parent_manifest": parent_path,
            "diagnostic_receipts": diagnostics, "output": tmp_path / "overlay.json",
            "parquet_manifest_sha256": parquet_sha, "parent_manifest_sha256": parent_sha}


def selection(bundle):
    overlay = build_overlay(**bundle)
    return overlay, PathwaySelection(bundle["output"], bundle["parquet_root"],
                                    expected_sha256=file_sha256(bundle["output"]))


def selector(aid=2, feature_type="pathway", contrast="contrast-0"):
    return {"series_accession": "GSE56960", "feature_type": feature_type,
            "contrast": contrast, "analysis_version": PARENTS[aid][0]}


def test_explicit_new_version_read_preserves_keys_values_nulls_and_provenance(bundle):
    before = {p: file_sha256(p) for p in bundle["parquet_root"].rglob("*") if p.is_file()}
    overlay, reader = selection(bundle)
    assert overlay["rows"] == 90 and overlay["q_threshold_changes"] == 2
    assert overlay["reactome"]["exact_membership_artifact_sha256"] == "d" * 64
    assert overlay["reactome"]["data_release"] is None
    assert overlay["historical_reproducibility_resolved"] is False
    assert overlay["new_checkpoint_two_loop_reproducible"] is True
    assert "/private/" not in json.dumps(overlay)
    requested = selector()
    resolved = reader.resolve(requested, purpose="internal_technical")
    assert requested["analysis_version"] == PARENTS[2][0]
    assert resolved["selected"]["analysis_version"] == PARENTS[2][0] + SUFFIX
    actual = reader.read(requested, purpose="internal_technical")
    original = pq.read_table(bundle["parquet_root"] / "analysis-2/pathway_effects.parquet",
                             filters=[("contrast", "=", "contrast-0")])
    assert actual.equals(original, check_metadata=True)
    assert actual["standard_error"].null_count == 3
    assert actual["p_value"].to_pylist() == [.01, .2, None]
    assert actual["effect_estimate"].to_pylist() == [1.5, -.8, None]
    assert actual["leading_edge"].to_pylist() == ["1;2", "3", ""]
    assert actual["fgsea_log2err"].to_pylist() == [.1, .1, None]
    assert reader.read(selector(11), purpose="internal_technical")["q_value"].to_pylist() == [.03, .3, None]
    assert reader.read(requested, purpose="internal_technical", feature_id="absent").num_rows == 0
    assert {p: file_sha256(p) for p in before} == before
    with pytest.raises(ValueError, match="new overlay"):
        build_overlay(**bundle)


@pytest.mark.parametrize("feature_type", ["gene", "gene_expression", "cpg", "dmr", "pathways"])
def test_gene_cpg_and_other_feature_types_never_redirect(bundle, feature_type):
    _, reader = selection(bundle)
    request = selector(feature_type=feature_type)
    for purpose in ("internal_technical", "public", "publication"):
        result = reader.resolve(request, purpose=purpose)
        assert result["selection_status"] == "unaffected_use_existing_reader"
        assert result["selected"] == request and result["artifact"] is None
    with pytest.raises(ValueError, match="existing"):
        reader.read(request, purpose="internal_technical")


@pytest.mark.parametrize("purpose", ["public", "publication", "expert_approved", "", None])
def test_pending_candidate_never_becomes_public_or_expert_approval(bundle, purpose):
    _, reader = selection(bundle)
    with pytest.raises(PermissionError, match="Pending correction"):
        reader.resolve(selector(), purpose=purpose)


def test_exact_contrast_version_and_artifact_hash_required(bundle):
    _, reader = selection(bundle)
    with pytest.raises(KeyError, match="Contrast"):
        reader.resolve(selector(contrast="unknown"), purpose="internal_technical")
    request = {**selector(), "analysis_version": "unrelated-v1"}
    assert reader.resolve(request, purpose="internal_technical")["selected"] == request
    with pytest.raises(ValueError, match="explicit"):
        reader.resolve({**selector(), "contrast": ""}, purpose="internal_technical")
    path = bundle["parquet_root"] / "analysis-2/pathway_effects.parquet"
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="immutable Parquet"):
        reader.read(selector(), purpose="internal_technical")


@pytest.mark.parametrize("flag,value", [("scientific_approval", True),
                                       ("public_results_modified", True),
                                       ("historical_reproducibility_resolved", True),
                                       ("new_fgsea_execution_performed", True)])
def test_changed_approval_or_history_flags_fail_closed(bundle, flag, value):
    path = bundle["parquet_root"] / "manifest.json"
    data = json.loads(path.read_text())
    data[flag] = value
    bundle["parquet_manifest_sha256"] = dump(path, data)
    with pytest.raises(ValueError, match="pending"):
        build_overlay(**bundle)
    assert not bundle["output"].exists()


def test_changed_receipt_or_parent_hash_fails_without_writing(bundle):
    receipt = bundle["diagnostic_receipts"][2]
    receipt.write_bytes(receipt.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_overlay(**bundle)
    assert not bundle["output"].exists()


@pytest.mark.parametrize("column,values,error", [
    ("feature_type", ["gene"] * 54, "exact new GSE56960 pathway"),
    ("feature_id", ["duplicate"] * 54, "five-part"),
    ("standard_error", [.1] * 54, "effect SE"),
    ("q_value_contrast", [.9] * 54, "within-contrast BH"),
    ("q_value_analysis_family", [.9] * 54, "Analysis-family BH"),
    ("leading_edge", ["wrong"] * 54, "context/null status"),
])
def test_semantic_corruption_rejected_even_if_a_new_manifest_hash_is_supplied(
        bundle, column, values, error):
    path = bundle["parquet_root"] / "analysis-2/pathway_effects.parquet"
    table = pq.read_table(path)
    table = table.set_column(table.column_names.index(column), column, pa.array(values))
    pq.write_table(table, path)
    manifest_path = bundle["parquet_root"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["runs"][0].update(sha256=file_sha256(path), bytes=path.stat().st_size)
    bundle["parquet_manifest_sha256"] = dump(manifest_path, manifest)
    with pytest.raises(ValueError, match=error):
        build_overlay(**bundle)
    assert not bundle["output"].exists()


def test_overlay_hash_and_public_flag_cannot_be_silently_changed(bundle):
    selection(bundle)
    original = file_sha256(bundle["output"])
    data = json.loads(bundle["output"].read_text())
    data["public_allowed"] = True
    modified = dump(bundle["output"], data)
    with pytest.raises(ValueError, match="hash mismatch"):
        PathwaySelection(bundle["output"], bundle["parquet_root"], expected_sha256=original)
    with pytest.raises(ValueError, match="internal selector"):
        PathwaySelection(bundle["output"], bundle["parquet_root"], expected_sha256=modified)


def test_artifact_escape_is_not_permitted(bundle):
    selection(bundle)
    data = json.loads(bundle["output"].read_text())
    data["selectors"][0]["artifact"]["relative_name"] = "../other.parquet"
    modified = dump(bundle["output"], data)
    with pytest.raises(ValueError, match="relative artifact"):
        PathwaySelection(bundle["output"], bundle["parquet_root"], expected_sha256=modified)
