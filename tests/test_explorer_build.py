from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from nutrigenomics import explorer_build as builder


GRAPH_SCHEMA = """
CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT,name TEXT,metadata_json TEXT);
CREATE TABLE edge(id TEXT PRIMARY KEY,subject TEXT,object TEXT,relation TEXT,tier TEXT,taxon TEXT,context_json TEXT);
CREATE TABLE source(id TEXT PRIMARY KEY,metadata_json TEXT);
CREATE TABLE edge_source(edge_id TEXT,source_id TEXT,PRIMARY KEY(edge_id,source_id));
CREATE TABLE citation(edge_id TEXT,pmid TEXT,PRIMARY KEY(edge_id,pmid));
CREATE TABLE measurement(id TEXT PRIMARY KEY,food TEXT,nutrient TEXT,amount REAL,unit TEXT,basis TEXT,status TEXT,source_id TEXT,raw_json TEXT);
CREATE TABLE identifier_mapping(subject TEXT,namespace TEXT,identifier TEXT,relation TEXT,source_id TEXT,basis TEXT);
"""
CORE_SCHEMA = """
CREATE TABLE food(id INTEGER,source_id TEXT,source_code TEXT,name TEXT,name_en TEXT,aliases TEXT,
 food_group TEXT,edible_state TEXT,preparation_state TEXT,active_release TEXT,composition_mode TEXT,metadata_json TEXT);
CREATE TABLE component(id INTEGER,component_key TEXT,name TEXT,name_en TEXT,category TEXT,canonical_unit TEXT,aliases TEXT);
"""


@pytest.fixture
def inputs(tmp_path):
    graph, core = tmp_path / "graph.sqlite", tmp_path / "core.sqlite"
    with sqlite3.connect(graph) as db:
        db.executescript(GRAPH_SCHEMA)
        db.executemany("INSERT INTO node VALUES (?,?,?,?)", [
            ("FDC:1", "food", "Rice", "{}"),
            ("FDC:2", "food", "Rice", "{}"),
            ("FDCNutrient:1", "nutrient", "Sodium", "{}"),
            ("FDCNutrient:2", "nutrient", "Total fat", "{}"),
            ("MESH:D012964", "chemical", "Sodium", "{}"),
            ("NCBIGene:1", "gene", "GENE1", "{}"),
            ("MESH:D006973", "disease", "Hypertension", "{}")])
        db.execute("INSERT INTO source VALUES (?,?)", ("source:composition", builder.canonical({"sha256": "a" * 64, "export_allowed": True})))
        db.executemany("INSERT INTO edge VALUES (?,?,?,?,?,?,?)", [
            ("composition0", "FDC:1", "FDCNutrient:1", "contains_nutrient", "observed_composition", None, "{}"),
            ("composition_missing", "FDC:2", "FDCNutrient:1", "contains_nutrient", "observed_composition", None, "{}")])
        db.executemany("INSERT INTO edge_source VALUES (?,?)", [("composition0", "source:composition"), ("composition_missing", "source:composition")])
        db.executemany("INSERT INTO citation VALUES (?,?)", [("composition0", "123"), ("composition_missing", "123")])
        db.executemany("INSERT INTO measurement VALUES (?,?,?,?,?,?,?,?,?)", [
            ("m0", "FDC:1", "FDCNutrient:1", 0, "mg", "source_100g", "measured_zero", "source:composition", "{}"),
            ("mmissing", "FDC:2", "FDCNutrient:1", None, "mg", "source_100mL", "missing", "source:composition", "{}")])
    with sqlite3.connect(core) as db:
        db.executescript(CORE_SCHEMA)
        db.execute("INSERT INTO food VALUES (1,'R03','FDC:1','쌀','Rice','[\"쌀밥\"]','grain','edible','cooked','v1','direct','{}')")
        # Same name is deliberately not an exact mapping to FDC:2.
        db.execute("INSERT INTO food VALUES (2,'R03','OTHER:9','쌀','Rice','[]','grain','edible','raw','v1','direct','{}')")
        db.execute("INSERT INTO component VALUES (1,'FDC-NUTRIENT:1','나트륨','Sodium','nutrient','mg','[\"Na\"]')")
    scope = tmp_path / "scope.json"
    scope.write_text(builder.canonical({"domains": ["blood_pressure", "lipids", "obesity"], "diseases": [
        {"id": "MESH:D006973", "label": "Hypertension", "label_ko": "고혈압", "domain": "blood_pressure"}]}), encoding="utf-8")
    return {"graph_db": graph, "core_db": core, "scope_path": scope,
            "output_dir": tmp_path / "release", "release_id": "test-v1"}


def open_release(inputs):
    db = sqlite3.connect(inputs["output_dir"] / "explorer.sqlite")
    db.row_factory = sqlite3.Row
    return db


def test_source_databases_unchanged_and_immutable_release(inputs):
    before = [builder.sha256(inputs[key]) for key in ("graph_db", "core_db")]
    result = builder.build_release(**inputs)
    assert before == [builder.sha256(inputs[key]) for key in ("graph_db", "core_db")]
    assert result["live_pointer_modified"] is False
    assert (inputs["output_dir"] / "manifest.json").exists()
    assert (inputs["output_dir"] / "coverage.json").exists()
    with pytest.raises(FileExistsError):
        builder.build_release(**inputs)


def test_exact_food_code_mapping_and_aliases(inputs):
    result = builder.build_release(**inputs)
    with open_release(inputs) as db:
        row = db.execute("SELECT * FROM node WHERE id='FDC:1'").fetchone()
        assert row["label"] == "쌀"
        assert "쌀밥" in json.loads(row["aliases_json"])
        assert json.loads(row["metadata_json"])["core_food_id"] == 1
        missing = db.execute("SELECT metadata_json FROM node WHERE id='FDC:2'").fetchone()[0]
        assert "core_food_id" not in json.loads(missing)
    assert result["core_import"]["exact_food_mapping"] == 1


def test_zero_missing_basis_and_pmids_preserved(inputs):
    result = builder.build_release(**inputs)
    with open_release(inputs) as db:
        rows = {r["id"]: r for r in db.execute("SELECT * FROM measurement")}
        assert rows["m0"]["amount"] == 0
        assert rows["m0"]["status"] == "measured_zero"
        assert rows["mmissing"]["amount"] is None
        assert rows["mmissing"]["basis"] == "source_100mL"
        assert rows["mmissing"]["status"] == "missing"
    assert result["counts"]["unique_pmids"] == 1


def test_name_correspondence_is_candidate_not_quantity_or_identity(inputs):
    result = builder.build_release(**inputs)
    assert result["candidate_name_correspondences"] == 1
    with open_release(inputs) as db:
        edge = db.execute("SELECT * FROM edge WHERE relation='candidate_name_correspondence'").fetchone()
        assert edge["tier"] == "candidate"
        assert edge["review_status"] == "candidate"
        context = json.loads(edge["context_json"])
        assert context["quantity_transfer_allowed"] is False
        assert context["molecular_equivalence"] is False
        assert db.execute("SELECT count(*) FROM edge WHERE subject='FDCNutrient:2'").fetchone()[0] == 0


def test_ambiguous_name_correspondence_abstains(inputs):
    with sqlite3.connect(inputs["graph_db"]) as db:
        db.execute("INSERT INTO node VALUES ('MESH:D999','chemical','Sodium','{}')")
    result = builder.build_release(**inputs)
    assert result["candidate_name_correspondences"] == 0


def test_ambiguous_nutrient_label_abstains(inputs):
    with sqlite3.connect(inputs["graph_db"]) as db:
        db.execute("INSERT INTO node VALUES ('FDCNutrient:999','nutrient','Sodium','{}')")
    result = builder.build_release(**inputs)
    assert result["candidate_name_correspondences"] == 0


def test_food_domains_are_navigation_only(inputs):
    builder.build_release(**inputs)
    with open_release(inputs) as db:
        assert db.execute("SELECT count(*) FROM node_domain WHERE node_id='FDC:1'").fetchone()[0] == 3
        scope = json.loads(db.execute("SELECT value_json FROM meta WHERE key='gaps'").fetchone()[0])
        assert any("navigation scope" in gap for gap in scope)


def test_failed_build_does_not_publish_or_modify_source(inputs):
    with sqlite3.connect(inputs["graph_db"]) as db:
        db.execute("UPDATE edge SET object='missing-node'")
    original_hash = builder.sha256(inputs["graph_db"])
    with pytest.raises(sqlite3.IntegrityError):
        builder.build_release(**inputs)
    assert not inputs["output_dir"].exists()
    assert not inputs["output_dir"].with_name(".release.building").exists()
    assert builder.sha256(inputs["graph_db"]) == original_hash


def test_unsafe_release_id_rejected_before_writing(inputs):
    inputs["release_id"] = "../current"
    with pytest.raises(ValueError, match="release_id"):
        builder.build_release(**inputs)
    assert not inputs["output_dir"].exists()


def test_missing_source_reference_prevents_publish(inputs):
    with sqlite3.connect(inputs["graph_db"]) as db:
        db.execute("UPDATE edge_source SET source_id='nonexistent-source'")
    with pytest.raises(ValueError, match="provenance"):
        builder.build_release(**inputs)
    assert not inputs["output_dir"].exists()


def test_kfind_inconsistent_source_name_abstains(inputs):
    identifier = "K-FIND:15100065:code1:namehash"
    with sqlite3.connect(inputs["graph_db"]) as db:
        db.execute("INSERT INTO node VALUES (?,?,?,?)", (identifier, "food", "Rice", builder.canonical({"food_code": "code1", "source_name": "원본명"})))
    with sqlite3.connect(inputs["core_db"]) as db:
        meta = builder.canonical({"kfind_dataset_id": "15100065", "provider_food_code": "code1", "provider_food_name": "다른원본명"})
        db.execute("INSERT INTO food VALUES (3,'R02',?,'밥','Rice','[]','grain','edible','cooked','v1','direct',?)", (identifier, meta))
    result = builder.build_release(**inputs)
    assert result["core_import"]["ambiguous_food_mapping"] == 1
    with open_release(inputs) as db:
        metadata = json.loads(db.execute("SELECT metadata_json FROM node WHERE id=?", (identifier,)).fetchone()[0])
        assert "core_food_id" not in metadata


def make_ctd(tmp_path, monkeypatch):
    directory = tmp_path / "ctd"
    directory.mkdir()
    records = {
        "CTD_curated_chemicals_diseases": [dict(ChemicalID="D012964", ChemicalName="Sodium", DiseaseID="MESH:D006973", DiseaseName="Hypertension", DirectEvidence="marker/mechanism", PubMedIDs="42")],
        "CTD_curated_genes_diseases": [dict(GeneID="1", GeneSymbol="GENE1", DiseaseID="MESH:D006973", DiseaseName="Hypertension", DirectEvidence="marker/mechanism", PubMedIDs="43")],
        "CTD_chem_gene_ixns": [dict(ChemicalID="D012964", ChemicalName="Sodium", GeneID="1", GeneSymbol="GENE1", OrganismID="10090", Organism="Mus musculus", Interaction="sodium increases GENE1 expression", InteractionActions="increases^expression", GeneForms="", PubMedIDs="44")],
        "CTD_genes_pathways": [dict(GeneID="1", GeneSymbol="GENE1", PathwayID="REACT:R-HSA-1", PathwayName="Test pathway")],
        "CTD_chemicals": [dict(ChemicalID="D012964", ChemicalName="Sodium", MESHSynonyms="", CTDCuratedSynonyms="")],
        "CTD_diseases": [dict(DiseaseID="MESH:D006973", DiseaseName="Hypertension")],
    }
    files = {}
    for stem, rows in records.items():
        file = directory / (stem + ".parquet")
        file.write_text(builder.canonical(rows), encoding="utf-8")
        files[stem + ".csv.gz"] = {"id": stem, "sha256": hashlib.sha256(stem.encode()).hexdigest(),
                                    "parquet": file.name, "parquet_sha256": builder.sha256(file), "export_allowed": False}
    (directory / "manifest.json").write_text(builder.canonical({"files": files}), encoding="utf-8")
    monkeypatch.setattr(builder, "parquet_rows", lambda path, where="", parameters=(): iter(json.loads(path.read_text(encoding="utf-8"))))
    return directory


def test_ctd_unknown_disease_taxon_and_mouse_influence_stay_separate(inputs, monkeypatch):
    inputs["ctd_dir"] = make_ctd(inputs["output_dir"].parent, monkeypatch)
    result = builder.build_release(**inputs)
    assert result["ctd_expansion"]["chemical_gene_source_rows"] == 1
    with open_release(inputs) as db:
        disease = db.execute("SELECT * FROM edge WHERE relation='chemical_disease_marker_mechanism'").fetchone()
        influence = db.execute("SELECT * FROM edge WHERE relation='chemical_gene_influence'").fetchone()
        assert disease["taxon"] is None
        assert influence["taxon"] == "10090"
        assert json.loads(influence["context_json"])["organism"] == "Mus musculus"
        assert json.loads(influence["context_json"])["binding_label"] is False
        assert json.loads(disease["pmids_json"]) == ["42"]


def test_ctd_hash_mismatch_prevents_publish(inputs, monkeypatch):
    ctd = make_ctd(inputs["output_dir"].parent, monkeypatch)
    (ctd / "CTD_chem_gene_ixns.parquet").write_text("[]", encoding="utf-8")
    inputs["ctd_dir"] = ctd
    with pytest.raises(ValueError, match="hash mismatch"):
        builder.build_release(**inputs)
    assert not inputs["output_dir"].exists()


def test_literature_candidates_keep_source_namespaces_and_unknown_taxon(inputs):
    literature = inputs["output_dir"].parent / "literature.sqlite"
    with sqlite3.connect(literature) as db:
        db.executescript("CREATE TABLE metadata(key TEXT,value_json TEXT);CREATE TABLE assertion(id TEXT,pmid TEXT,subject TEXT,predicate TEXT,object TEXT,status TEXT,tier TEXT,payload_json TEXT)")
        payload = {"payload": {"subject_mention": {"kind": "nutrient", "text": "Sodium"},
                               "object_mention": {"kind": "outcome", "text": "blood pressure"}}}
        db.execute("INSERT INTO assertion VALUES (?,?,?,?,?,?,?,?)", ("a1", "123", "local:sodium", "associated_with", "local:blood-pressure", "candidate", "extracted", builder.canonical(payload)))
    inputs["literature_db"] = literature
    result = builder.build_release(**inputs)
    assert result["literature_import"]["human_approved_assertions"] == 0
    with open_release(inputs) as db:
        row = db.execute("SELECT * FROM edge WHERE tier='extracted'").fetchone()
        assert row["subject"] == "Literature:local:sodium"
        assert row["review_status"] == "candidate"
        assert row["taxon"] is None
        assert db.execute("SELECT count(*) FROM node WHERE id='FDCNutrient:1'").fetchone()[0] == 1


def add_kfind_missing_fixture(inputs):
    identifier = "K-FIND:15100065:code1:namehash"
    meta = {"food_code": "code1", "source_name": "원본명"}
    with sqlite3.connect(inputs["graph_db"]) as db:
        db.execute("INSERT INTO node VALUES (?,?,?,?)", (identifier, "food", "Rice", builder.canonical(meta)))
        db.execute("INSERT INTO measurement VALUES (?,?,?,?,?,?,?,?,?)", ("k1", identifier, "FDCNutrient:1", None, "mg", "source_100g", "missing", "source:composition", "{}"))
        db.execute("INSERT INTO edge VALUES (?,?,?,?,?,?,?)", ("kedge", identifier, "FDCNutrient:1", "contains_nutrient", "observed_composition", None, builder.canonical({"measurement_id": "k1", "status": "missing"})))
        db.execute("INSERT INTO edge_source VALUES (?,?)", ("kedge", "source:composition"))
    with sqlite3.connect(inputs["core_db"]) as db:
        meta = {"kfind_dataset_id": "15100065", "provider_food_code": "code1", "provider_food_name": "원본명"}
        db.execute("INSERT INTO food VALUES (3,'R02',?,'밥','Rice','[]','grain','edible','cooked','v1','direct',?)", (identifier, builder.canonical(meta)))
        db.execute("CREATE TABLE composition_measurement(id INTEGER,food_id INTEGER,component_id INTEGER,release TEXT,source_ref TEXT,amount_per_100g REAL,unit TEXT,basis TEXT,status TEXT)")
        db.execute("INSERT INTO composition_measurement VALUES (10,3,1,'v1',?,NULL,'mg','source_100g','not_measured')", (identifier + ":nutrient:Na",))


def test_exact_kfind_status_overlay_retains_original_and_no_quantity_transfer(inputs):
    add_kfind_missing_fixture(inputs)
    result = builder.build_release(**inputs)
    assert result["core_import"]["kfind_composition_validation"] == {"rows_compared": 1, "statuses_reconciled": 1, "ambiguous": 0}
    with open_release(inputs) as db:
        row = db.execute("SELECT * FROM measurement WHERE id='k1'").fetchone()
        assert row["status"] == "not_measured"
        assert row["amount"] is None
        audit = json.loads(row["raw_json"])["explorer_status_reconciliation"]
        assert audit["original_graph_status"] == "missing"
        assert audit["quantity_changed"] is False
        context = json.loads(db.execute("SELECT context_json FROM edge WHERE id='kedge'").fetchone()[0])
        assert context["status"] == "not_measured"


def prepare_parent_receipt(inputs):
    stat = inputs["core_db"].stat()
    (inputs["output_dir"] / "verification.json").write_text(builder.canonical({
        "source_stats_after_build": {"core_db": {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}},
        "invalid_source_references": 0, "live_pointer_modified": False}), encoding="utf-8")


def test_derivative_upgrade_preserves_parent_and_reuses_verified_hash(inputs):
    builder.build_release(**inputs)
    prepare_parent_receipt(inputs)
    before = builder.sha256(inputs["output_dir"] / "explorer.sqlite")
    result = builder.upgrade_release(inputs["output_dir"], inputs["core_db"], inputs["output_dir"].parent / "upgrade", "test-v2")
    assert result["derived_from"]["release_id"] == "test-v1"
    assert result["core_hash_cache_verification"]["sha256"] == builder.sha256(inputs["core_db"])
    assert builder.sha256(inputs["output_dir"] / "explorer.sqlite") == before
    assert result["counts"]["independent_studies"] is None


def test_derivative_upgrade_rejects_changed_source_cache(inputs):
    builder.build_release(**inputs)
    prepare_parent_receipt(inputs)
    with sqlite3.connect(inputs["core_db"]) as db:
        db.execute("UPDATE food SET name='changed'")
    with pytest.raises(ValueError, match="not reusable"):
        builder.upgrade_release(inputs["output_dir"], inputs["core_db"], inputs["output_dir"].parent / "upgrade", "test-v2")
    assert not (inputs["output_dir"].parent / "upgrade").exists()


def test_exact_annotation_tokens_no_substrings_aliases_or_case_folding():
    labels = {"PPARA": ["gene:a"], "IL6": ["gene:b", "gene:c"]}
    assert builder.annotation_targets("PPARA; IL6 ; PPARA", labels) == [("gene:a", "PPARA"), ("gene:b", "IL6"), ("gene:c", "IL6")]
    assert builder.annotation_targets("PPAR;PPARA-AS1;ppara;IL6R;PPARA,IL6", labels) == []
    assert builder.annotation_targets(None, labels) == []
    assert builder.annotation_targets(["PPARA"], labels) == []


def test_omics_references_copy_only_keys_annotations_and_record_fingerprints(inputs):
    with sqlite3.connect(inputs["core_db"]) as core:
        core.execute("CREATE TABLE study_effect(id INTEGER,series_accession TEXT,feature_type TEXT,feature_id TEXT,contrast TEXT,analysis_version TEXT,effect_estimate REAL,p_value REAL,metadata_json TEXT)")
        metadata = {"annotation_gene": "GENE1; GENE1-AS1", "genome_build": "hg19", "source_file_sha256": "b" * 64,
                    "m_value_estimate": 100, "m_value_standard_error": 999}
        core.execute("INSERT INTO study_effect VALUES (1,'GSE1','cpg','cg1','post_vs_pre','v1',0.2,0.5,?)", (builder.canonical(metadata),))
        core.execute("INSERT INTO study_effect VALUES (2,'GSE1','cpg','cg2','post_vs_pre','v1',0.3,0.6,?)", (builder.canonical({"annotation_gene": "GENE1-AS1"}),))
        core.execute("INSERT INTO study_effect VALUES (3,'GSE1','gene','GENE1','post_vs_pre','v1',2,0.01,?)", (builder.canonical({"annotation_gene": "GENE1"}),))
    builder.build_release(**inputs)
    prepare_parent_receipt(inputs)
    source_hash = builder.sha256(inputs["core_db"])
    manifest = builder.upgrade_omics_release(inputs["output_dir"], inputs["core_db"], inputs["output_dir"].parent / "omics", "test-v3")
    assert builder.sha256(inputs["core_db"]) == source_hash
    assert manifest["omics_reference_build"]["cpg_rows_scanned"] == 2
    assert manifest["omics_reference_build"]["reference_count"] == 1
    assert manifest["omics_reference_build"]["annotation_count"] == 1
    with sqlite3.connect(inputs["output_dir"].parent / "omics/explorer.sqlite") as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM omics_reference").fetchone()
        assert row["node_id"] == "NCBIGene:1"
        assert row["effect_id"] == 1
        assert row["relation"] == "platform_probe_gene_annotation"
        context = json.loads(row["context_json"])
        assert context["annotation_gene"] == "GENE1; GENE1-AS1"
        assert context["expression_regulation_evidence"] is False
        assert context["annotation_kind"] == "unknown"
        assert "effect_estimate" not in dict(row)
        assert "m_value_estimate" not in context
        annotation = json.loads(db.execute("SELECT metadata_json FROM omics_annotation").fetchone()[0])
        assert "m_value_estimate" not in annotation
        with sqlite3.connect(inputs["core_db"]) as core:
            core.row_factory = sqlite3.Row
            effect = dict(core.execute("SELECT * FROM study_effect WHERE id=1").fetchone())
            assert context["effect_record_sha256"] == builder.effect_record_hash(effect)
            effect["effect_estimate"] = 0.9
            assert context["effect_record_sha256"] != builder.effect_record_hash(effect)
