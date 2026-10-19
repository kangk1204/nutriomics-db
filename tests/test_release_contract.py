import json
import sqlite3

import pytest

from nutrigenomics.release_contract import (
    SCHEMA_VERSION, build_release_contract, install_release_contract, metadata_with_contract,
)


def fixture_db(path=":memory:"):
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE meta(key TEXT PRIMARY KEY,value_json TEXT);
      CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT);
      CREATE TABLE edge(id TEXT PRIMARY KEY,relation TEXT,tier TEXT,pmids_json TEXT);
      CREATE TABLE measurement(id TEXT PRIMARY KEY,food TEXT);
      CREATE TABLE source(id TEXT PRIMARY KEY);
      CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT,PRIMARY KEY(kind,id));
      CREATE TABLE effect(id TEXT,feature_type TEXT);
      CREATE TABLE effect_selector(series_accession TEXT,contrast TEXT,analysis_version TEXT,feature_type TEXT,total INTEGER);
    """)
    db.executemany("INSERT INTO meta VALUES(?,?)", [
        ("release_id", json.dumps("current-r")),
        ("dataset_release_id", json.dumps("ancestor-r")),
        ("service_build_id", json.dumps("old-service-r")),
        ("counts", json.dumps({"node": 400, "node_kinds": {"gene": 999}, "science_kinds": {"contrast": 52}})),
        ("public_counts", json.dumps({"effect": 999999})),
        ("status", json.dumps("submission_ready")),
    ])
    db.executemany("INSERT INTO node VALUES(?,?)", [("a", "food"), ("g", "gene"), ("e", "gene"), ("h", "outcome")])
    db.executemany("INSERT INTO edge VALUES(?,?,?,?)", [
        ("e1", "contains_nutrient", "observed_composition", '["1"]'),
        ("e2", "predicted_ccre_gene_link", "predicted", '["1","2"]')])
    db.executemany("INSERT INTO measurement VALUES(?,?)", [("m1", "a"), ("m2", "a")])
    for kind, item in [
        ("study", {"id": "s1", "metadata": {"series": ["A", "B"]}}),
        ("study", {"id": "s2", "metadata": {"series": ["NO_ANALYSIS"]}}),
        ("analysis_run", {"id": 1, "series_accession": "A", "status": "completed"}),
        ("analysis_run", {"id": 2, "series_accession": "B", "status": "completed"}),
        ("contrast", {"id": "diet", "publication_review_status": "pending"}),
        ("contrast", {"id": "drug", "context_type": "pharmacological_comparator"}),
        ("clinical_endpoint_result", {"id": "c1", "expert_review_status": "pending"}),
    ]:
        db.execute("INSERT INTO science_record VALUES(?,?,?)", (str(item["id"]), kind, json.dumps(item)))
    db.executemany("INSERT INTO effect VALUES(?,?)", [("x", "gene"), ("y", "cpg")])
    db.executemany("INSERT INTO effect_selector VALUES(?,?,?,?,?)", [("A", "diet", "v", "gene", 1), ("A", "diet", "v", "cpg", 1)])
    db.commit()
    return db


def test_inventory_replaces_stale_counts_and_keeps_all_grains_separate():
    with fixture_db() as db:
        result = build_release_contract(db, release_id="derived-r", manifest={"release_id": "current-r", "database_sha256": "a" * 64})
    c = result["counts"]
    assert c["node"] == c["nodes"] == 4
    assert c["by_kind"] == c["node_kinds"] == {"food": 1, "gene": 2, "outcome": 1}
    assert c["by_tier"] == c["relation_tiers"] == {"observed_composition": 1, "predicted": 1}
    assert c["measurement"] == 2 and c["composition_backed_foods"] == 1
    assert c["science_kinds"]["contrast"] == 2 and c["pharmacological_contrasts"] == 1
    assert c["effect"] == 2 and c["effect_types"] == {"cpg": 1, "gene": 1}
    assert c["unique_pmids"] == 2
    assert c["study_registry_records"] == 2 and c["reanalyzed_study_groups"] == 1
    assert c["independent_studies"] is None and c["independent_people"] is None and c["biological_genes"] is None
    assert result["study_scope"]["metadata_only_study_ids"] == ["s2"]
    assert result["study_scope"]["analysis_series"] == ["A", "B"]
    assert result["review_summary"]["core_records"] == 6
    assert result["review_summary"]["additional_clinical_records"] == 1
    assert result["submission_ready"] is False and result["expert_review"] == "pending"
    assert result["release_id"] == result["dataset_release_id"] == result["service_build_id"] == "derived-r"
    assert result["provenance"]["ancestry"][-1] == {"release_id": "current-r", "database_sha256": "a" * 64}
    assert result["provenance"]["legacy_release_identifiers"]["dataset_release_id"] == "ancestor-r"


def test_installed_inventory_is_request_constant_and_never_recounts_effects():
    with fixture_db() as db:
        contract = build_release_contract(db)
        install_release_contract(db, contract)
        statements = []
        db.set_trace_callback(statements.append)
        value = metadata_with_contract(db, {"release_id": "stale-file", "counts": {"effect": 900}})
        assert not any("count(" in sql.lower() or "sum(" in sql.lower() for sql in statements)
        assert value["release_id"] == value["dataset_release_id"] == "current-r"
        assert value["counts"] == value["public_counts"] == contract["counts"]
        assert value["release_contract"]["schema_version"] == SCHEMA_VERSION


def test_unmatched_selector_fails_build_instead_of_publishing_wrong_total():
    with fixture_db() as db:
        db.execute("UPDATE effect_selector SET total=100 WHERE feature_type='cpg'")
        with pytest.raises(ValueError, match="selector total"):
            build_release_contract(db)


def test_internal_subset_does_not_pose_as_full_result_count():
    with fixture_db() as db:
        db.executescript("DROP TABLE effect; DROP TABLE effect_selector; CREATE TABLE perf_effect_selector(total INTEGER); INSERT INTO perf_effect_selector VALUES(3);")
        value = build_release_contract(db)
        assert value["counts"]["indexed_effect_rows"] == 3
        assert value["counts"]["effect"] is None
        with sqlite3.connect(":memory:") as core:
            core.executescript("CREATE TABLE study_effect(feature_type TEXT); INSERT INTO study_effect VALUES('cpg'),('gene'),('gene'),('pathway');")
            value = build_release_contract(db, quantitative_db=core)
        assert value["counts"]["effect"] == 4 and value["counts"]["effect_types"]["gene"] == 2


def test_legacy_fallback_invalidates_on_database_change(tmp_path):
    path = tmp_path / "legacy.sqlite"
    db = fixture_db(path)
    assert metadata_with_contract(db)["counts"]["node"] == 4
    db.execute("INSERT INTO node VALUES('new','regulatory_region')")
    db.commit()
    assert metadata_with_contract(db)["counts"]["node"] == 5
    db.close()


def test_clinical_snapshot_keeps_review_and_context_without_private_operational_keys():
    from nutrigenomics.explorer import _clean_node

    clinical = [{"id": "clinical-r", "effect_estimate": None, "expert_review_status": "pending"}]
    node = {"id": "Outcome:LDL", "metadata": {
        "clinical_endpoints": clinical, "clinical_endpoint_audit": {"status": "source_checked_expert_pending"},
        "clinical_context_only": True, "mediation_established": False,
        "expert_review_status": "pending", "study_id": "s1",
        "local_path": "/home/lab/private", "raw_json": "RESTRICTED"}}
    snapshot = _clean_node(node)
    assert snapshot["metadata"]["clinical_endpoints"] == clinical
    assert snapshot["metadata"]["mediation_established"] is False
    assert snapshot["metadata"]["expert_review_status"] == "pending"
    assert "local_path" not in snapshot["metadata"] and "raw_json" not in snapshot["metadata"]
