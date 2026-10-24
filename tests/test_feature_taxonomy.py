import copy
import csv
import gzip
import hashlib
import json
import math
import sqlite3
from pathlib import Path

import pytest

from nutrigenomics.feature_taxonomy import (
    annotate_effect_record,
    classify_feature,
    effect_key,
    source_types_for_normalized_type,
)
from scripts.build_feature_taxonomy_overlay import build_overlay


def row(kind="cpg", feature="ch.1.24684R"):
    return {"series_accession": "GSE1", "analysis_version": "v1", "feature_type": kind,
            "feature_id": feature, "contrast": "post_minus_pre", "effect_estimate": -.125,
            "standard_error": None, "p_value": .5, "q_value": 1.0,
            "permissions": {"export_allowed": False}, "review_status": "pending"}


@pytest.mark.parametrize("identifier,expected", [
    ("cg00000001", "cg"), ("ch.1.24684R", "ch"), ("ch.X.7566543R", "ch"),
    ("CG00000001", "unrecognized"), ("ch.bad", "unrecognized"), ("rs123", "unrecognized"),
])
def test_prefix_is_not_a_chemical_context_claim(identifier, expected):
    actual = classify_feature("cpg", identifier)
    assert actual.normalized_feature_type == "methylation_probe"
    assert actual.identifier_class == expected
    assert actual.chemical_context == "unconfirmed"
    assert not actual.discovered_or_validated_dmr


def test_adapter_preserves_every_source_key_value_permission_and_missingness():
    original = row()
    before = copy.deepcopy(original)
    result = annotate_effect_record(original, overlay_release_id="overlay-1")
    assert original == before
    assert {key: result[key] for key in original} == original
    assert effect_key(result) == effect_key(original)
    assert result["feature_type"] == result["source_feature_type"] == "cpg"
    assert result["standard_error"] is None
    assert result["permissions"]["export_allowed"] is False
    assert result["feature_taxonomy"]["review_status"] == "derived_candidate_not_human_approved"
    assert annotate_effect_record(result, overlay_release_id="overlay-1") == result


def test_nonfinite_value_is_preserved_not_converted_to_zero():
    original = row(); original["effect_estimate"] = float("nan")
    result = annotate_effect_record(original)
    assert math.isnan(result["effect_estimate"]) and math.isnan(original["effect_estimate"])


@pytest.mark.parametrize("kind,identifier", [("gene", "APOE"), ("pathway", "R-HSA-556833"),
                                              ("Reactome", "R-HSA-556833")])
def test_gene_and_pathway_kinds_are_unchanged(kind, identifier):
    result = annotate_effect_record(row(kind, identifier))
    assert result["feature_type"] == result["normalized_feature_type"] == kind
    assert result["chemical_context"] == "not_applicable"


def test_dmr_name_alone_never_implies_a_discovered_or_fixed_region():
    result = annotate_effect_record(row("dmr", "chr1:1-1000"))
    assert result["normalized_feature_type"] == "tested_region"
    assert result["region_definition"] == "unconfirmed"
    assert not result["discovered_or_validated_dmr"]


def definition(version="v1"):
    return {"kind": "fixed_1000bp", "analysis_version": version, "source_sha256": "a" * 64}


def test_fixed_bin_label_requires_hash_bound_definition_and_exact_coordinates():
    result = annotate_effect_record(row("dmr", "chr1:1001-2000"), region_definition=definition())
    assert result["feature_type"] == "dmr"
    assert result["normalized_feature_type"] == "tested_region"
    assert result["display_label_ko"] == "사전 지정 1 kb 영역 검정"
    assert not result["discovered_or_validated_dmr"]
    for identifier in ("chr1:2-1001", "chr1:1-1001", "segmented-DMR-1"):
        with pytest.raises(ValueError):
            annotate_effect_record(row("dmr", identifier), region_definition=definition())
    with pytest.raises(ValueError):
        annotate_effect_record(row("dmr", "chr1:1-1000"), region_definition=definition("another"))


def test_conflicts_and_cross_feature_annotation_fail_closed():
    original = row(); original["normalized_feature_type"] = "gene"
    with pytest.raises(ValueError, match="overwrite"):
        annotate_effect_record(original)
    with pytest.raises(ValueError, match="series"):
        annotate_effect_record(row(), series_accession="GSE2")
    with pytest.raises(ValueError, match="Annotation"):
        annotate_effect_record(row(), annotation_identity={"feature_id": "cg123"})
    with pytest.raises(ValueError):
        annotate_effect_record(row(), region_definition=definition())


def test_source_filter_mapping_keeps_old_urls_and_source_kind():
    assert source_types_for_normalized_type("methylation_probe") == ("cpg",)
    assert source_types_for_normalized_type("tested_region") == ("dmr",)
    assert source_types_for_normalized_type("pathway") == ("pathway",)


def fixture_manifest(tmp_path, *, duplicate=False):
    sources = []
    records = [("cpg", ["cg00000001", "ch.1.24684R", "unknown-probe"]),
               ("gene", ["APOE"]), ("pathway", ["R-HSA-556833"]),
               ("dmr", ["chr1:1-1000"])]
    for index, (kind, identifiers) in enumerate(records):
        path = tmp_path / f"input-{index}.tsv.gz"
        if duplicate and index == 0:
            identifiers = identifiers + identifiers[:1]
        with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(["feature_type", "feature_id", "contrast", "effect_estimate", "p_value", "standard_error"])
            for identifier in identifiers:
                writer.writerow([kind, identifier, "post_minus_pre", "-0.125", "0.5", "NA"])
        source = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "expected_rows": len(identifiers), "series_accession": "GSE1",
                  "analysis_version": f"v{index}", "source_feature_type": kind}
        if kind == "dmr":
            source["region_definition"] = definition(f"v{index}")
        sources.append(source)
    manifest = {"schema_version": "nutriomics-feature-taxonomy-source-manifest-v1",
                "release_id": "source-release", "overlay_release_id": "taxonomy-release",
                "sources": sources}
    p = tmp_path / "manifest.json"; p.write_text(json.dumps(manifest), encoding="utf-8")
    return p, manifest


def test_streamed_overlay_covers_all_keys_without_copying_effect_values(tmp_path):
    path, manifest = fixture_manifest(tmp_path)
    expected_bytes = {x["path"]: Path(x["path"]).read_bytes() for x in manifest["sources"]}
    output = tmp_path / "overlay"
    result = build_overlay(path, output, batch_size=2)
    assert result["full_row_coverage"] == {"source_rows": 6, "covered_source_keys": 6,
                                            "unmatched_source_keys": 0, "duplicate_source_keys": 0}
    assert result["counts"]["methylation_probe_identifier_classes"] == {"cg": 1, "ch": 1, "unrecognized": 1}
    assert all(x["digests"]["original"] == x["digests"]["preserved"] for x in result["inputs"])
    assert all(Path(p).read_bytes() == b for p, b in expected_bytes.items())
    assert not result["effect_values_copied_to_overlay"]
    assert result["scientific_approvals_created"] == 0
    with sqlite3.connect(output / "taxonomy.sqlite3") as db:
        keys = db.execute("""SELECT s.series_accession,s.source_feature_type,m.feature_id,
            s.contrast,s.analysis_version FROM effect_membership m
            JOIN source_selector s ON s.id=m.selector_id""").fetchall()
        assert len(keys) == 6 and ("GSE1", "cpg", "ch.1.24684R", "post_minus_pre", "v0") in keys
        for table in ("feature_taxonomy", "effect_membership", "source_input"):
            assert not {"effect_estimate", "p_value", "q_value"} & {
                item[1] for item in db.execute(f"PRAGMA table_info({table})")}
    assert (output / "source-manifest.json").read_bytes() == path.read_bytes()


@pytest.mark.parametrize("failure", ["hash", "rows", "counts", "duplicate"])
def test_failed_build_never_promotes_or_edits_source(tmp_path, failure):
    path, manifest = fixture_manifest(tmp_path, duplicate=failure == "duplicate")
    if failure == "hash":
        manifest["sources"][0]["sha256"] = "f" * 64
    elif failure == "rows":
        manifest["sources"][0]["expected_rows"] += 1
    elif failure == "counts":
        manifest["expected_counts"] = {"source_feature_types": {"cpg": 999}}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "overlay"
    before = {s["path"]: hashlib.sha256(Path(s["path"]).read_bytes()).hexdigest() for s in manifest["sources"]}
    with pytest.raises(ValueError):
        build_overlay(path, output, batch_size=2)
    assert not output.exists()
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in before.items())
    assert len(list(tmp_path.glob("overlay.building-*/FAILED.json"))) == 1


def test_existing_overlay_is_not_overwritten(tmp_path):
    path, _ = fixture_manifest(tmp_path)
    out = tmp_path / "existing"; out.mkdir(); marker = out / "keep.txt"; marker.write_text("keep")
    with pytest.raises(ValueError, match="already exists"):
        build_overlay(path, out)
    assert marker.read_text() == "keep"


@pytest.mark.parametrize("wrong_name", [False, True])
def test_annotation_identity_is_analysis_bound_and_never_chemical_approval(tmp_path, wrong_name):
    path, manifest = fixture_manifest(tmp_path)
    manifest["sources"][0]["source_analysis_id"] = 4
    annotation = {"records": [{"parent_analysis_id": 4, "series": "GSE1",
        "annotation_source": {"version": "0.6.1"}, "manifest_source": {"version": "0.4.0"},
        "examples": [{"annotation_id_exact_match": True, "manifest_id_exact_match": True,
            "original": {"feature_type": "cpg", "feature_id": "ch.1.24684R"},
            "annotation_record": {"Name": "wrong" if wrong_name else "ch.1.24684R"}}]}]}
    binding = tmp_path / "binding.json"; binding.write_text(json.dumps(annotation))
    manifest["annotation_binding"] = {"path": str(binding), "sha256": hashlib.sha256(binding.read_bytes()).hexdigest()}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if wrong_name:
        with pytest.raises(ValueError, match="original feature"):
            build_overlay(path, tmp_path / "overlay")
    else:
        result = build_overlay(path, tmp_path / "overlay")
        assert result["annotation_binding"]["examples"] == 1
        with sqlite3.connect(tmp_path / "overlay/taxonomy.sqlite3") as db:
            evidence = json.loads(db.execute("SELECT evidence_json FROM annotation_identity").fetchone()[0])
        assert evidence["chemical_context"] == "unconfirmed"
        assert evidence["review_status"] == "not_human_approved"
