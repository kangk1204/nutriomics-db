"""Scientific release boundaries, using tiny metadata-only SQLite fixtures."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from nutrigenomics import explorer_scientific_build as builder


def _write_json(path: Path, value) -> None:
    path.write_text(builder.canonical(value) + "\n", encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def scientific_inputs(tmp_path):
    base = tmp_path / "parent-v3"
    base.mkdir()
    parent_db = base / "explorer.sqlite"
    connection = sqlite3.connect(parent_db)
    try:
        connection.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
            CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT,label TEXT,metadata_json TEXT);
            CREATE TABLE edge(id TEXT PRIMARY KEY,subject TEXT,object TEXT,relation TEXT,context_json TEXT);
            CREATE TABLE source(id TEXT PRIMARY KEY,metadata_json TEXT);
            CREATE TABLE omics_reference(node_id TEXT,effect_id INTEGER,series_accession TEXT,
                feature_type TEXT,feature_id TEXT,contrast TEXT,analysis_version TEXT,
                relation TEXT,context_json TEXT,PRIMARY KEY(node_id,effect_id));
        """)
        connection.execute("INSERT INTO meta VALUES ('release_id',?)", ('"parent-v3"',))
        connection.execute("INSERT INTO node VALUES ('gene:1','gene','GENE1','{}')")
        connection.execute("INSERT INTO node VALUES ('food:1','food','Yogurt','{}')")
        connection.execute("INSERT INTO edge VALUES ('e1','food:1','gene:1','candidate',?)",
                           (builder.canonical({"review_status": "candidate", "taxon": None}),))
        connection.execute("INSERT INTO source VALUES ('source:composition',?)",
                           (builder.canonical({"sha256": "b" * 64, "hash_scope": "original_composition_file"}),))
        connection.execute("INSERT INTO omics_reference VALUES (?,?,?,?,?,?,?,?,?)",
                           ("gene:1", 101, "GSETEST", "cpg", "cg00000001", "active_vs_control",
                            "analysis-v1", "platform_probe_gene_annotation",
                            builder.canonical({"annotation_gene": "GENE1", "not_expression_regulation": True})))
        connection.commit()
    finally:
        connection.close()

    core = tmp_path / "core.sqlite"
    connection = sqlite3.connect(core)
    try:
        connection.executescript("""
            CREATE TABLE ng_exposure(id TEXT PRIMARY KEY,study_id TEXT,label TEXT,kind TEXT,
                amount REAL,unit TEXT,dose_basis TEXT,component_keys TEXT,food_terms TEXT,metadata_json TEXT);
            CREATE TABLE ng_arm(id TEXT PRIMARY KEY,study_id TEXT,label TEXT,exposure_ids TEXT,metadata_json TEXT);
            CREATE TABLE ng_contrast(id TEXT PRIMARY KEY,study_id TEXT,series_accession TEXT,
                contrast TEXT,analysis_version TEXT,exposure_ids TEXT,arm_ids TEXT,metadata_json TEXT);
            CREATE TABLE ng_assertion(id TEXT PRIMARY KEY,study_id TEXT,exposure_id TEXT,contrast_id TEXT,
                source_id TEXT,review_status TEXT,organism TEXT,inference TEXT,limitations TEXT,metadata_json TEXT);
            CREATE TABLE ng_bridge_source(id TEXT PRIMARY KEY,url TEXT,version TEXT,sha256 TEXT,
                source_location TEXT,metadata_json TEXT);
            CREATE TABLE research_study(id TEXT PRIMARY KEY,title TEXT,metadata_json TEXT);
            CREATE TABLE analysis_run(id INTEGER PRIMARY KEY,series_accession TEXT,analysis_version TEXT,
                metadata_json TEXT,output_manifest_json TEXT);
            CREATE TABLE study_effect(id INTEGER PRIMARY KEY,series_accession TEXT,feature_type TEXT,
                feature_id TEXT,contrast TEXT,analysis_version TEXT,estimate REAL,pvalue REAL,metadata_json TEXT);
        """)
        connection.execute("INSERT INTO ng_exposure VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "exposure:S1:yogurt", "S1", "Trial yogurt", "product", 800, "g", "single_meal",
            '["total_fat"]', '["yogurt"]', builder.canonical({"expert_review_status": "pending",
                                                            "component_causality_identified": False})))
        connection.execute("INSERT INTO ng_arm VALUES ('arm:S1:active','S1','Active',?,?)", (
            '["exposure:S1:yogurt"]', builder.canonical({"n_people_scope": "public_omics_subset"})))
        connection.execute("INSERT INTO ng_contrast VALUES (?,?,?,?,?,?,?,?)", (
            "contrast:S1:v1:active", "S1", "GSETEST", "active_vs_control", "analysis-v1",
            '["exposure:S1:yogurt"]', '["arm:S1:active"]', builder.canonical({
                "comparison_kind": "within_arm_pre_post", "fdr_family": "study_main_contrasts",
                "sample_scope": {"contrast_people": 7, "fitted_model_assays": 28}})))
        connection.execute("INSERT INTO ng_assertion VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "assertion:S1", "S1", "exposure:S1:yogurt", "contrast:S1:v1:active", "source:S1",
            "source_checked", "Homo sapiens", "association", '["not an individual health prediction"]',
            builder.canonical({"expert_review_status": "pending"})))
        connection.execute("INSERT INTO ng_bridge_source VALUES (?,?,?,?,?,?)", (
            "source:S1", "https://example.org/primary", "bridge-v1", "c" * 64,
            "Methods > product formulation", builder.canonical({"hash_scope": "curation_manifest",
                "curation_manifest": "config/human_exposures.v1.json", "expert_review_status": "pending"})))
        connection.execute("INSERT INTO research_study VALUES ('S1','Trial one','{}')")
        connection.execute("INSERT INTO research_study VALUES ('S2','Unrelated trial','{}')")
        connection.execute("INSERT INTO analysis_run VALUES (1,'GSETEST','analysis-v1',?,?)", (
            builder.canonical({"effect_rows": 1, "analysis_kind": "paired_pre_post"}),
            builder.canonical([{"path": "original/effects.parquet", "sha256": "d" * 64}])))
        connection.execute("INSERT INTO study_effect VALUES (101,'GSETEST','cpg','cg00000001',"
                           "'active_vs_control','analysis-v1',0.032,0.57,?)", (
            builder.canonical({"annotation_gene": "GENE1", "source_file_sha256": "e" * 64}),))
        connection.commit()
    finally:
        connection.close()

    stat = core.stat()
    manifest = {"release_id": "parent-v3", "database_sha256": builder.sha256(parent_db),
                "source_paths": {"core_db": str(core.resolve())},
                "input_hashes": {"core_db": {"sha256": builder.sha256(core), "hash_scope": "original_core_file"}},
                "counts": {"nodes": 2, "edges": 1}, "gaps": ["candidate does not mean efficacy"]}
    _write_json(base / "manifest.json", manifest)
    _write_json(base / "verification.json", {
        "source_stats_after_build": {"core_db": {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}}})
    _write_json(base / "coverage.json", {"release_id": "parent-v3", "independent_study_count": None})
    curation = tmp_path / "curation.json"
    _write_json(curation, {
        "schema_version": 1, "version": "source-check-v1", "expert_review_status": "pending",
        "verification_actor": "assistant_source_check",
        "exposures": [{"core_exposure_id": "exposure:S1:yogurt", "study_id": "S1",
                       "input_match_policy": "exact_exposure_id_only", "expert_review_status": "pending"}],
        "studies": [{"study_id": "S1", "series": ["GSETEST"], "contrast_selectors": [{
            "id": "contrast:S1:v1:active", "series_accession": "GSETEST",
            "contrast": "active_vs_control", "analysis_version": "analysis-v1"}]}]})
    return {"base_dir": base, "core_db": core, "output_dir": tmp_path / "science-v4",
            "release_id": "science-v4", "curation_file": curation}


def _no_publish(inputs):
    assert not inputs["output_dir"].exists()
    assert not inputs["output_dir"].with_name("." + inputs["output_dir"].name + ".building").exists()


def _change_curation(inputs, edit):
    document = _read_json(inputs["curation_file"])
    edit(document)
    _write_json(inputs["curation_file"], document)


def test_fresh_release_preserves_parent_and_core_and_decodes_metadata(scientific_inputs):
    inputs = scientific_inputs
    paths = [inputs["core_db"], inputs["base_dir"] / "explorer.sqlite", inputs["base_dir"] / "manifest.json"]
    hashes = [builder.sha256(path) for path in paths]
    result = builder.upgrade_scientific_release(**inputs)
    assert [builder.sha256(path) for path in paths] == hashes
    assert result["derived_from"]["release_id"] == "parent-v3"
    assert result["derived_from"]["database_sha256"] == hashes[1]
    assert result["scientific_matching"]["expert_review_status"] == "pending"
    assert result["scientific_matching"]["curation_sha256"] == builder.sha256(inputs["curation_file"])
    release = inputs["output_dir"]
    assert result["database_sha256"] == builder.sha256(release / "explorer.sqlite")
    assert (release / "evidence_curations.json").read_bytes() == inputs["curation_file"].read_bytes()
    assert builder.sha256(release / "scientific_builder_source.py") == result["builder_sha256"]
    assert _read_json(release / "verification.json")["parent_database_hash_verified"] is True
    with sqlite3.connect(release / "explorer.sqlite") as db:
        exposure = json.loads(db.execute("SELECT metadata_json FROM science_record WHERE kind='exposure'").fetchone()[0])
        assert exposure["component_keys"] == ["total_fat"]
        assert exposure["food_terms"] == ["yogurt"]
        assert exposure["metadata"]["component_causality_identified"] is False
        assert "metadata_json" not in exposure
        assertion = json.loads(db.execute("SELECT metadata_json FROM science_record WHERE kind='assertion'").fetchone()[0])
        assert assertion["review_status"] == "source_checked"
        assert assertion["metadata"]["expert_review_status"] == "pending"
        assert assertion["inference"] == "association"
        assert assertion["limitations"] == ["not an individual health prediction"]
        assert db.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()[0] == '"science-v4"'


def test_existing_release_is_immutable(scientific_inputs):
    builder.upgrade_scientific_release(**scientific_inputs)
    destination = scientific_inputs["output_dir"] / "explorer.sqlite"
    before = builder.sha256(destination)
    with pytest.raises(FileExistsError, match="immutable"):
        builder.upgrade_scientific_release(**scientific_inputs)
    assert builder.sha256(destination) == before


def test_parent_hash_mismatch_rejected(scientific_inputs):
    with sqlite3.connect(scientific_inputs["base_dir"] / "explorer.sqlite") as db:
        db.execute("UPDATE node SET label='Changed parent'")
    with pytest.raises(ValueError, match="parent SQLite hash mismatch"):
        builder.upgrade_scientific_release(**scientific_inputs)
    _no_publish(scientific_inputs)


@pytest.mark.parametrize("change", ["size", "mtime", "path"])
def test_core_identity_mismatch_rejected(scientific_inputs, change):
    core = scientific_inputs["core_db"]
    if change == "size":
        with core.open("ab") as handle:
            handle.write(b"changed")
    elif change == "mtime":
        stat = core.stat()
        os.utime(core, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    else:
        renamed = core.with_name("different-source.sqlite")
        renamed.write_bytes(core.read_bytes())
        stat = core.stat()
        os.utime(renamed, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        scientific_inputs["core_db"] = renamed
    with pytest.raises(ValueError, match="core fingerprint"):
        builder.upgrade_scientific_release(**scientific_inputs)
    _no_publish(scientific_inputs)


@pytest.mark.parametrize("field,value", [
    ("core_exposure_id", "name-only:Trial yogurt"), ("study_id", "S2")])
def test_curated_exposure_requires_existing_pk_and_study(scientific_inputs, field, value):
    _change_curation(scientific_inputs, lambda document: document["exposures"][0].update({field: value}))
    with pytest.raises(ValueError, match="curated exposure"):
        builder.upgrade_scientific_release(**scientific_inputs)
    _no_publish(scientific_inputs)


@pytest.mark.parametrize("field,value", [
    ("id", "missing-contrast"), ("series_accession", "GSEOTHER"),
    ("contrast", "another_comparison"), ("analysis_version", "analysis-v2")])
def test_curated_contrast_requires_exact_pk_and_analysis_tuple(scientific_inputs, field, value):
    _change_curation(scientific_inputs,
                     lambda document: document["studies"][0]["contrast_selectors"][0].update({field: value}))
    with pytest.raises(ValueError, match="curated contrast"):
        builder.upgrade_scientific_release(**scientific_inputs)
    _no_publish(scientific_inputs)


@pytest.mark.parametrize("wrong_study", ["S2", "absent-study"])
def test_curated_contrast_cannot_be_assigned_to_another_study(scientific_inputs, wrong_study):
    _change_curation(scientific_inputs, lambda document: document["studies"][0].update(study_id=wrong_study))
    with pytest.raises(ValueError, match="curated (study|contrast)"):
        builder.upgrade_scientific_release(**scientific_inputs)
    _no_publish(scientific_inputs)


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("expert_review_status", "approved")])
def test_unsupported_curation_state_rejected(scientific_inputs, field, value):
    _change_curation(scientific_inputs, lambda document: document.update({field: value}))
    with pytest.raises(ValueError, match="curation"):
        builder.upgrade_scientific_release(**scientific_inputs)
    _no_publish(scientific_inputs)


def test_curation_changed_after_read_is_rejected(scientific_inputs, monkeypatch):
    original_read_only = builder.read_only
    mutated = False

    def read_only_with_external_curation_change(path):
        nonlocal mutated
        if Path(path).resolve() == scientific_inputs["core_db"].resolve() and not mutated:
            _change_curation(scientific_inputs, lambda document: document.update(version="changed-during-build"))
            mutated = True
        return original_read_only(path)

    monkeypatch.setattr(builder, "read_only", read_only_with_external_curation_change)
    with pytest.raises(ValueError, match="curation"):
        builder.upgrade_scientific_release(**scientific_inputs)
    assert mutated is True
    _no_publish(scientific_inputs)


def test_core_changed_during_indexing_is_not_published(scientific_inputs, monkeypatch):
    original_read_only = builder.read_only

    def read_only_with_external_core_change(path):
        if Path(path).resolve() == scientific_inputs["core_db"].resolve():
            with sqlite3.connect(path) as writable:
                writable.execute("UPDATE research_study SET title='External change'")
            stat = Path(path).stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        return original_read_only(path)

    monkeypatch.setattr(builder, "read_only", read_only_with_external_core_change)
    with pytest.raises(ValueError, match="core changed"):
        builder.upgrade_scientific_release(**scientific_inputs)
    _no_publish(scientific_inputs)


def test_metadata_derivative_never_reads_or_copies_effect_value_rows(scientific_inputs, monkeypatch):
    original_read_only = builder.read_only
    core_sql = []

    def trace_core(path):
        connection = original_read_only(path)
        if Path(path).resolve() == scientific_inputs["core_db"].resolve():
            connection.set_trace_callback(core_sql.append)
        return connection

    monkeypatch.setattr(builder, "read_only", trace_core)
    result = builder.upgrade_scientific_release(**scientific_inputs)
    assert not any("study_effect" in statement.casefold() for statement in core_sql)
    assert result["scientific_matching"]["effect_value_rows_copied"] == 0
    with sqlite3.connect(scientific_inputs["output_dir"] / "explorer.sqlite") as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='study_effect'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM science_record WHERE kind='effect'").fetchone()[0] == 0
        reference = db.execute("SELECT effect_id,feature_type,feature_id FROM omics_reference").fetchone()
        assert reference == (101, "cpg", "cg00000001")
        columns = [row[1] for row in db.execute("PRAGMA table_info(omics_reference)")]
        assert "estimate" not in columns and "pvalue" not in columns


def test_hash_scope_and_original_graph_are_preserved(scientific_inputs):
    original_parent = sqlite3.connect(scientific_inputs["base_dir"] / "explorer.sqlite")
    try:
        before = {table: original_parent.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
                  for table in ("node", "edge", "source", "omics_reference")}
    finally:
        original_parent.close()
    result = builder.upgrade_scientific_release(**scientific_inputs)
    assert result["input_hashes"]["core_db"]["hash_scope"] == "original_core_file"
    with sqlite3.connect(scientific_inputs["output_dir"] / "explorer.sqlite") as db:
        for table, rows in before.items():
            assert db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall() == rows
        source = json.loads(db.execute("SELECT metadata_json FROM science_record WHERE kind='bridge_source'").fetchone()[0])
        assert source["sha256"] == "c" * 64
        assert source["metadata"]["hash_scope"] == "curation_manifest"
        assert source["metadata"]["curation_manifest"] == "config/human_exposures.v1.json"
        assert source["metadata"]["expert_review_status"] == "pending"
        assert source["source_location"] == "Methods > product formulation"
        run = json.loads(db.execute("SELECT metadata_json FROM science_record WHERE kind='analysis_run'").fetchone()[0])
        assert run["output_manifest"] == [{"path": "original/effects.parquet", "sha256": "d" * 64}]


@pytest.mark.parametrize("release_id", ["../unsafe", "has/slash", "", "two words"])
def test_unsafe_release_identifier_rejected_before_writing(scientific_inputs, release_id):
    scientific_inputs["release_id"] = release_id
    with pytest.raises(ValueError, match="invalid release ID"):
        builder.upgrade_scientific_release(**scientific_inputs)
    _no_publish(scientific_inputs)
