"""Native SQLite composition, exact reviews, and actual API/download agreement."""
import copy
import csv
import gzip
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nutrigenomics import composed_release as module
from nutrigenomics.composed_science import annotate_matches, record_context
from nutrigenomics.explorer_build import SCHEMA
from nutrigenomics.explorer_performance_build import add_graph_read_indexes
from nutrigenomics.public_service import create_app
from nutrigenomics.publication import EFFECT_FIELDS, INT_EFFECT, REAL_EFFECT
from nutrigenomics.submission_package import _science_sqlite, canonical, digest, sha256


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical(value), encoding="utf-8")


def bind(path, root):
    return {"path": path.relative_to(root).as_posix(), "sha256": sha256(path), "bytes": path.stat().st_size}


@pytest.fixture
def composition(tmp_path, monkeypatch):
    for key in ("NUTRIGENOMICS_PUBLIC_OMICS_INDEX", "NUTRIGENOMICS_EXPLORER_QUERY_INDEX",
                "NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX", "NUTRIGENOMICS_SOURCE_REPLICA_MAP"):
        monkeypatch.delenv(key, raising=False)
    base, candidate = tmp_path / "base", tmp_path / "candidate"
    base.mkdir()
    (candidate / "supplement").mkdir(parents=True)
    study = lambda identifier, series: {"id": identifier, "title": identifier,
        "publication_review_status": "pending", "metadata": {"series": [series],
        "expert_review_status": "pending", "primary_sources": [{"label": "Primary publication", "url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=" + series}]}}
    original = {("study", "N06"): study("N06", "GSE27385"),
        ("study", "S05"): study("S05", "GSE219217"), ("study", "S01"): study("S01", "GSE127530"),
        ("analysis_run", "5"): {"id": 5, "series_accession": "GSE219217", "analysis_version": "v1", "status": "completed"},
        ("exposure", "S05-diet"): {"id": "S05-diet", "study_id": "S05", "label": "Diet", "food_terms": ["food"], "source_id": "open"},
        ("contrast", "c1"): {"id": "c1", "study_id": "S05", "series_accession": "GSE219217", "contrast": "30d", "analysis_version": "v1", "exposure_ids": ["S05-diet"]}}
    path = base / "nutriomics.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT,PRIMARY KEY(kind,id))")
        columns = []
        for name in EFFECT_FIELDS:
            columns.append('"' + name + '" ' + ("REAL" if name in REAL_EFFECT else "INTEGER" if name in INT_EFFECT else "TEXT") + (" PRIMARY KEY" if name == "id" else ""))
        db.execute("CREATE TABLE effect(" + ",".join(columns) + ")")
        db.execute("CREATE TABLE effect_selector(series_accession TEXT,feature_type TEXT,contrast TEXT,analysis_version TEXT,total INTEGER)")
        db.execute("CREATE VIEW study_effect AS SELECT *,NULL AS source_file FROM effect")
        db.executemany("INSERT INTO meta VALUES(?,?)", [("release_id", canonical("base-v2")),
            ("counts", canonical({"science_record": 999})), ("graph_performance", canonical({"indexed_for_release": "base-v2"}))])
        db.execute("INSERT INTO source VALUES('open',?)", (canonical({"url": "https://example.org/source", "version": "v1", "export_allowed": True}),))
        for identifier, kind, payload in [("food:one", "food", {}), ("nutrient:one", "nutrient", {}),
                                          ("Study:N06", "study", {"cached_record": original[("study", "N06")]})]:
            db.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", (identifier, kind, identifier, identifier, "[]", canonical(payload), "[]", 0))
        db.execute("INSERT INTO measurement VALUES('m1','food:one','nutrient:one',0,'g','edible_portion_100g','measured','open','{}')")
        db.executemany("INSERT INTO science_record VALUES(?,?,?)", [(identifier, kind, canonical(record)) for (kind, identifier), record in original.items()])
        for identifier, value in [("native-text-id-Z", None), ("native-text-id-A", 0.0)]:
            row = {k: None for k in EFFECT_FIELDS}
            row.update(id=identifier, series_accession="GSE219217", feature_type="cpg", feature_id=identifier,
                contrast="30d", analysis_version="v1", effect_estimate=value, q_value=.83,
                metadata_json=canonical({"beta_scale_zero": 0, "missing_ci": None}), rights_policy_id="policy1")
            db.execute("INSERT INTO effect VALUES(" + ",".join("?" for _ in EFFECT_FIELDS) + ")", [row[k] for k in EFFECT_FIELDS])
        db.execute("INSERT INTO effect_selector VALUES('GSE219217','cpg','30d','v1',2)")
    write(base / "dataset-metadata.json", {"release_id": "base-v2", "status": "candidate_pending_expert_review", "expert_review": "pending", "counts": {"science_record": 999}})
    write(base / "rights-policy.json", {"analyses": [{"id": "policy1", "series_accession": "GSE219217", "analysis_version": "v1", "export_allowed": True}]})
    (base / "effects").mkdir()
    (base / "effects/native.csv.gz").write_bytes(b"source immutable numeric values")
    (base / "effects/native.parquet").write_bytes(b"source immutable typed values")
    # An old downloadable science export must not survive as current metadata.
    (base / "tables").mkdir()
    (base / "tables/science_record.csv.gz").write_bytes(b"old stale science export")
    files = [bind(p, base) for p in base.rglob("*") if p.is_file() and p.name != "nutriomics.sqlite3"]
    write(base / "manifest.json", {"release_id": "base-v2", "database_sha256": sha256(path), "files": files})
    corrected = copy.deepcopy(original)
    corrected[("study", "N06")]["metadata"]["primary_sources"][0]["url"] = "https://pubmed.ncbi.nlm.nih.gov/21429719/"
    corrected[("study", "S05")]["metadata"]["source_method_notes"] = [{"source_fact": "Invisorb baseline; salting-out follow-up", "expert_review_status": "pending", "scientific_approval": False}]
    patches = []
    for key, field in [(("study", "N06"), ["metadata", "primary_sources", 0, "url"]), (("study", "S05"), ["metadata"])]:
        old, new = original[key], corrected[key]
        for part in field:
            old, new = old[part], new[part]
        patches.append({"record_kind": key[0], "record_id": key[1], "old_record_sha256": digest(original[key]),
            "new_record_sha256": digest(corrected[key]), "expert_review_status": "pending",
            "changes": [{"operation": "replace", "path": field, "old_value": old, "new_value": new}]})
    supplement = candidate / "supplement"
    selectors = [{"series_accession": "GSE219217", "feature_type": "cpg", "contrast": "30d", "analysis_version": "v1",
        "total": 2, "files": [{**bind(base / name, base), "path": "base/" + name} for name in ["effects/native.csv.gz", "effects/native.parquet"]], "rights_policy_id": "policy1"}]
    _science_sqlite(supplement / "release-science.sqlite3", [{"kind": k, "id": i, "record": r} for (k, i), r in corrected.items()], selectors, "base-v2")
    write(supplement / "package-status.json", {"release_id": "corrected-v15", "immutable_base_release_id": "base-v2",
        "machine_package_integrity": True, "publication_ready": False, "science_records": len(corrected), "effect_rows_referenced": 2})
    write(supplement / "correction-ledger.json", {"automatic_expert_approval": False, "patches": patches})
    write(supplement / "producer-provenance.json", {"original_producer_archive_sha256": "a" * 64})
    write(supplement / "rights-machine-audit.json", {"machine_policy_checks_passed": True, "blanket_third_party_license": False})
    bindings = [bind(p, candidate) for p in supplement.iterdir()]
    bindings += [{**bind(path, base), "path": "base/nutriomics.sqlite3"}, *[{**r, "path": "base/" + r["path"]} for r in files if r["path"].startswith("effects/")]]
    write(candidate / "filemap.json", {"files": bindings})
    kwargs = {"base": base, "candidate": candidate, "output": tmp_path / "new", "release_id": "composed-v1",
        "expected_base_manifest_sha256": sha256(base / "manifest.json"), "expected_candidate_filemap_sha256": sha256(candidate / "filemap.json")}
    return {"kwargs": kwargs, "original": original, "corrected": corrected, "path": path, "supplement": supplement}


def declaration(record, decision="accept"):
    return {"record_kind": "study", "record_id": "N06", "record_sha256": digest(record), "decision": decision,
        "reviewer": "Human reviewer", "reviewer_type": "human", "reviewer_attestation": True,
        "reviewed_at": "2026-10-20T12:00:00+00:00", "primary_paper_location": "Methods and Table1", "reason": "Source checked",
        "source_dependency_sha256": digest({"record_sha256": digest(record), "dependencies": source_dependency()})}


def source_dependency(source_sha="a" * 64):
    return {"source_proofs": [{"url": "https://example.org/paper", "locator": "Methods Table1", "sha256": source_sha}],
            "related_records": []}


def dependency_args(composition, directory, source_sha="a" * 64):
    rows = []
    for (kind, identifier), record in composition["corrected"].items():
        dependency = source_dependency(source_sha)
        row = {"record_kind": kind, "record_id": identifier, "record_sha256": digest(record), "dependencies": dependency}
        row["source_dependency_sha256"] = digest({"record_sha256": row["record_sha256"], "dependencies": dependency})
        rows.append(row)
    path = directory / "review-dependencies.json"
    write(path, {"schema_version": "nutriomics-review-dependencies-v1", "records": rows})
    return {"review_dependencies": path, "expected_review_dependencies_sha256": sha256(path)}


def test_composed_sqlite_and_http_download_have_one_corrected_identity(composition):
    args = composition["kwargs"]
    before = sha256(composition["path"])
    manifest = module.build(**args)
    output = args["output"]
    assert sha256(composition["path"]) == before
    assert manifest["counts"]["science_record"] == 6 and manifest["counts"]["effect"] == 2
    assert manifest["review_summary"]["expert_approved_records"] == 0
    assert manifest["review_summary"]["human_decisions"]["decisions"]["pending"] == 6
    assert not manifest["submission_ready"] and not manifest["journal_submission_ready"]
    assert manifest["immutable_base_release_id"] == "base-v2"
    assert manifest["candidate_composition_release_id"] == "corrected-v15"
    with sqlite3.connect(output / "nutriomics.sqlite3") as db, sqlite3.connect(composition["path"]) as base:
        for table in ["effect", "effect_selector", "measurement"]:
            assert db.execute("SELECT * FROM " + table).fetchall() == base.execute("SELECT * FROM " + table).fetchall()
        assert {(r[0], r[1]): json.loads(r[2]) for r in db.execute("SELECT kind,id,metadata_json FROM science_record")} == composition["corrected"]
        cached = json.loads(db.execute("SELECT metadata_json FROM node WHERE id='Study:N06'").fetchone()[0])
        assert cached["cached_record"] == composition["corrected"][("study", "N06")]
    for row in manifest["files"]:
        assert sha256(output / row["path"]) == row["sha256"]
    with TestClient(create_app(output, allow_candidate=True)) as client:
        meta = client.get("/api/v1/public/meta").json()
        assert meta["release_id"] == meta["dataset_release_id"] == meta["service_build_id"] == "composed-v1"
        assert client.get("/api/v1/explorer/meta").json()["counts"] == meta["counts"]
        study = client.get("/api/v1/studies/N06").json()
        assert study["study"]["metadata"]["primary_sources"][0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/21429719/"
        assert study["science_review_context"]["scientific_approval"] is False
        download = client.get("/api/v1/public/files/tables/science_record.csv.gz")
        assert download.status_code == 200 and download.content != b"old stale science export"
        assert download.headers["x-nutriomics-release"] == "composed-v1"
        evidence = client.get("/api/v1/evidence", params={"accession": "GSE219217", "contrast": "30d", "analysis_version": "v1"}).json()
        assert {r["id"]: r["effect_estimate"] for r in evidence["items"]} == {"native-text-id-A": 0, "native-text-id-Z": None}
        assert evidence["total_selector"] == 2 and evidence["release_id"] == "composed-v1"


def test_source_classification_and_human_declarations_are_separate(composition, tmp_path):
    args = composition["kwargs"]
    decisions = tmp_path / "decisions.json"
    write(decisions, [declaration(composition["corrected"][("study", "N06")], "exclude")])
    classifications = tmp_path / "classifications.json"
    write(classifications, {"schema_version": module.CLASSIFICATION_SCHEMA, "automatic_expert_approval": False,
        "records": [{"record_kind": "study", "record_id": "S05", "record_sha256": digest(composition["corrected"][("study", "S05")]),
            "classification": "exploratory_confounded_extraction_time", "source_checked": True,
            "primary_dietary_evidence_eligible": False, "reason": "Extraction differs with time",
            "source_references": [{"url": "https://example.org/source", "locator": "Methods Sec22", "sha256": "a" * 64}]}]})
    manifest = module.build(**args, decisions=decisions, expected_decisions_sha256=sha256(decisions),
                            classifications=classifications, expected_classifications_sha256=sha256(classifications),
                            **dependency_args(composition, tmp_path))
    assert manifest["review_summary"]["human_decisions"]["decisions"]["exclude"] == 1
    assert manifest["review_summary"]["expert_approved_records"] == 0
    with sqlite3.connect(args["output"] / "nutriomics.sqlite3") as db:
        excluded = record_context(db, "study", "N06")
        assert excluded["publication_disposition"] == "excluded" and excluded["primary_dietary_evidence_eligible"] is False
        source = record_context(db, "study", "S05")
        assert source["source_classification"]["source_checked"] is True and source["scientific_approval"] is False
        evidence = annotate_matches(db, [{"study_id": "S05", "contrasts": [{"id": "c1"}]}])
        assert evidence[0]["primary_dietary_evidence_eligible"] is False
        assert evidence[0]["contrasts"][0]["study_source_classification"]["classification"] == "exploratory_confounded_extraction_time"
    with TestClient(create_app(args["output"], allow_candidate=True)) as client:
        value = client.get("/api/v1/studies/S05").json()
        assert value["science_review_context"]["source_classification"]["scientific_approval"] is False
        direct = client.get("/api/v1/evidence", params={"accession": "GSE219217", "contrast": "30d", "analysis_version": "v1"}).json()
        contextual = direct["science_review_context"]["curated_contrasts"][0]
        assert contextual["study_review"]["source_classification"]["primary_dietary_evidence_eligible"] is False


@pytest.mark.parametrize("decision", ["accept", "exclude", "unconfirmed"])
def test_changed_hash_cannot_reuse_even_attested_human_decision(composition, tmp_path, decision):
    path = tmp_path / "decisions.json"
    write(path, [declaration(composition["original"][("study", "N06")], decision)])
    with pytest.raises(ValueError, match="stale or changed"):
        module.build(**composition["kwargs"], decisions=path, expected_decisions_sha256=sha256(path))
    assert not composition["kwargs"]["output"].exists()


def test_explicit_accept_is_recorded_but_never_promotes_submission(composition, tmp_path):
    path = tmp_path / "decision.json"
    write(path, [declaration(composition["corrected"][("study", "N06")])])
    manifest = module.build(**composition["kwargs"], decisions=path, expected_decisions_sha256=sha256(path),
                            **dependency_args(composition, tmp_path))
    assert manifest["review_summary"]["expert_approved_records"] == 1 and manifest["submission_ready"] is False
    with sqlite3.connect(composition["kwargs"]["output"] / "nutriomics.sqlite3") as db:
        assert record_context(db, "study", "N06")["scientific_approval"] is True
        assert json.loads(db.execute("SELECT metadata_json FROM science_record WHERE kind='study' AND id='N06'").fetchone()[0])["publication_review_status"] == "pending"
        assert record_context(db, "study", "N06")["source_dependency_binding"]["source_dependency_sha256"] == declaration(composition["corrected"][("study", "N06")])["source_dependency_sha256"]


@pytest.mark.parametrize("defect", ["candidate_filemap", "base_manifest", "missing_science", "partial_science", "nonempty_wal", "rights_missing"])
def test_binding_and_source_failures_do_not_activate_release(composition, defect):
    args = composition["kwargs"]
    if defect == "candidate_filemap":
        (args["candidate"] / "filemap.json").write_text("{}")
    elif defect == "base_manifest":
        (args["base"] / "manifest.json").write_text("{}")
    elif defect == "missing_science":
        (composition["supplement"] / "release-science.sqlite3").unlink()
    elif defect == "partial_science":
        (composition["supplement"] / "release-science.sqlite3").write_bytes(b"partial")
    elif defect == "nonempty_wal":
        Path(str(composition["path"]) + "-wal").write_bytes(b"x")
    else:
        (args["base"] / "rights-policy.json").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        module.build(**args)
    assert not args["output"].exists()


def test_startup_rejects_mixed_file_and_sqlite_release_id(composition):
    module.build(**composition["kwargs"])
    output = composition["kwargs"]["output"]
    metadata = json.loads((output / "dataset-metadata.json").read_text(encoding="utf-8"))
    metadata["release_id"] = "not-the-actual-release"
    write(output / "dataset-metadata.json", metadata)
    with pytest.raises(ValueError, match="file/API identity"):
        create_app(output, allow_candidate=True)


def test_sql_authorizer_refuses_numeric_mutation_during_metadata_integration(composition, monkeypatch):
    original = module._sync_cached_records
    def bad_sync(db, old, new):
        db.execute("UPDATE effect SET effect_estimate=42")
        return original(db, old, new)
    monkeypatch.setattr(module, "_sync_cached_records", bad_sync)
    before = sha256(composition["path"])
    with pytest.raises(sqlite3.DatabaseError, match="authorized"):
        module.build(**composition["kwargs"])
    assert sha256(composition["path"]) == before and not composition["kwargs"]["output"].exists()


def test_source_change_during_copy_prevents_activation(composition, monkeypatch):
    original = module._copy_verified
    def copy_then_change(source, target, binding):
        original(source, target, binding)
        if source.name == "rights-policy.json":
            (composition["supplement"] / "package-status.json").write_bytes(b"changed")
    monkeypatch.setattr(module, "_copy_verified", copy_then_change)
    with pytest.raises(ValueError, match="input changed"):
        module.build(**composition["kwargs"])
    assert not composition["kwargs"]["output"].exists()


@pytest.mark.parametrize("fault", ["stale", "approval", "missing_source_hash", "attribution", "private_source"])
def test_source_checks_cannot_smuggle_approval_or_unbound_interpretation(composition, tmp_path, fault):
    row = {"record_kind": "study", "record_id": "S01", "record_sha256": digest(composition["corrected"][("study", "S01")]),
           "classification": "supplement_mapping_unresolved", "source_checked": True,
           "primary_dietary_evidence_eligible": False, "supplement_attribution_established": False,
           "reason": "Supplement visit mapping unresolved",
           "source_references": [{"url": "https://example.org/source", "locator": "Methods", "sha256": "a" * 64}]}
    if fault == "stale":
        row["record_sha256"] = "0" * 64
    elif fault == "approval":
        row["scientific_approval"] = True
    elif fault == "missing_source_hash":
        row["source_references"][0].pop("sha256")
    elif fault == "attribution":
        row["supplement_attribution_established"] = True
    else:
        row["source_references"][0]["path"] = "/home/private/source.xml"
    path = tmp_path / "classifications.json"
    write(path, {"schema_version": module.CLASSIFICATION_SCHEMA, "automatic_expert_approval": False, "records": [row]})
    with pytest.raises(ValueError):
        module.build(**composition["kwargs"], classifications=path, expected_classifications_sha256=sha256(path))
    assert not composition["kwargs"]["output"].exists()


def test_explicit_source_rights_deny_is_not_overridden_by_candidate_status(composition):
    args = composition["kwargs"]
    policy = args["base"] / "rights-policy.json"
    value = json.loads(policy.read_text(encoding="utf-8"))
    value["analyses"][0]["export_allowed"] = False
    write(policy, value)
    manifest_path = args["base"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    next(row for row in manifest["files"] if row["path"] == "rights-policy.json").update(bind(policy, args["base"]))
    write(manifest_path, manifest)
    args["expected_base_manifest_sha256"] = sha256(manifest_path)
    with pytest.raises(ValueError, match="denied"):
        module.build(**args)
    assert not args["output"].exists()


def test_review_hash_dependency_is_checked_at_service_startup(composition):
    module.build(**composition["kwargs"])
    output = composition["kwargs"]["output"]
    path = output / "nutriomics.sqlite3"
    with sqlite3.connect(path) as db:
        row = declaration(composition["corrected"][("study", "N06")])
        db.execute("INSERT INTO scientific_review_decision VALUES(?,?,?,?)", ("study", "N06", row["record_sha256"], canonical(row)))
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database_sha256"] = sha256(path)
    write(manifest_path, manifest)
    with pytest.raises(ValueError, match="review/classification dependency"):
        create_app(output, allow_candidate=True)


def test_new_release_does_not_accept_existing_output_or_parent_path(composition):
    args = composition["kwargs"]
    for output in [args["base"] / "nested", args["candidate"] / "nested"]:
        with pytest.raises(ValueError, match="separate"):
            module.build(**{**args, "output": output})
    module.build(**args)
    with pytest.raises(FileExistsError):
        module.build(**args)


def test_public_routes_use_sql_wrapper_ids_when_payload_omits_id(tmp_path):
    release = tmp_path / "legacy"
    release.mkdir()
    path = release / "nutriomics.sqlite3"
    study = {"title": "SQL wrapper is authoritative", "metadata": {"series": ["GSE_TEST"]}}
    clinical = {"study_id": "S_SQL", "endpoint": {"name": "LDL-C", "unit": "mmol/L"}}
    exposure = {"study_id": "S_SQL", "label": "Exposure without duplicate id"}
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT,PRIMARY KEY(kind,id))")
        db.execute("INSERT INTO meta VALUES('release_id','\"legacy\"')")
        db.executemany("INSERT INTO science_record VALUES(?,?,?)", [
            ("S_SQL", "study", canonical(study)), ("clinical-row", "clinical_endpoint_result", canonical(clinical)),
            ("exposure-row", "exposure", canonical(exposure))])
    write(release / "dataset-metadata.json", {"release_id": "legacy", "expert_review": "pending"})
    write(release / "manifest.json", {"release_id": "legacy", "database_sha256": sha256(path)})
    with TestClient(create_app(release, allow_candidate=True)) as client:
        response = client.get("/api/v1/studies/S_SQL")
        assert response.status_code == 200
        assert response.json()["study"] == study
        assert response.json()["clinical_endpoints"] == [clinical]
        assert {r["kind"] for r in response.json()["records"]} == {"clinical_endpoint_result", "exposure"}
        assert client.get("/api/v1/studies/GSE_TEST").json()["study"] == study
        assert client.get("/api/v1/interventions").json()["items"] == [exposure]
        assert client.get("/api/v1/studies").json()["items"] == [study]


def test_human_decision_requires_sources_even_when_payload_matches(composition, tmp_path):
    path = tmp_path / "decisions.json"
    write(path, [declaration(composition["corrected"][("study", "N06")])])
    with pytest.raises(ValueError, match="current source review dependencies"):
        module.build(**composition["kwargs"], decisions=path, expected_decisions_sha256=sha256(path))
    assert not composition["kwargs"]["output"].exists()


def test_changed_source_with_unchanged_record_rejects_old_human_approval(composition, tmp_path):
    path = tmp_path / "decisions.json"
    write(path, [declaration(composition["corrected"][("study", "N06")])])
    with pytest.raises(ValueError, match="changed or unbound source dependencies"):
        module.build(**composition["kwargs"], decisions=path, expected_decisions_sha256=sha256(path),
                     **dependency_args(composition, tmp_path, "b" * 64))
    assert not composition["kwargs"]["output"].exists()


def test_current_reader_accepts_zero_human_v1_inventory_without_new_dependency_field(composition):
    module.build(**composition["kwargs"])
    path = composition["kwargs"]["output"] / "nutriomics.sqlite3"
    with sqlite3.connect(path) as db:
        metadata = json.loads((path.parent / "dataset-metadata.json").read_text(encoding="utf-8"))
        manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
        metadata["review_summary"]["human_decisions"].pop("source_dependencies_bound")
        module.validate_service_composition(db, metadata, manifest)


def _repin_native_fixture(composition):
    args = composition["kwargs"]
    path = args["base"] / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["database_sha256"] = sha256(composition["path"])
    write(path, value)
    args["expected_base_manifest_sha256"] = sha256(path)
    path = args["candidate"] / "filemap.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    for entry in value["files"]:
        if entry["path"] == "base/nutriomics.sqlite3":
            entry.update(sha256=sha256(composition["path"]), bytes=composition["path"].stat().st_size)
    write(path, value)
    args["expected_candidate_filemap_sha256"] = sha256(path)


def test_isolated_exposure_is_searchable_selectable_and_not_a_clinical_study(composition):
    with sqlite3.connect(composition["path"]) as db:
        db.row_factory = sqlite3.Row
        db.execute("INSERT INTO node VALUES('ClinicalStudy:S05','diet','Clinical study S05',NULL,'[]',?, '[]',0)",
                   (canonical({"entity_type": "clinical_study", "study_id": "S05"}),))
        db.execute("CREATE TABLE node_kind(id TEXT PRIMARY KEY,kind TEXT NOT NULL)")
        db.execute("INSERT INTO node_kind SELECT id,kind FROM node")
        add_graph_read_indexes(db, "base-v2")
    _repin_native_fixture(composition)
    original_sha = sha256(composition["path"])
    manifest = module.build(**composition["kwargs"])
    output = composition["kwargs"]["output"]
    assert original_sha == sha256(composition["path"])
    ledger = json.loads((output / "composition/exposure-node-ledger.json").read_text(encoding="utf-8"))
    assert ledger["registered"] == ledger["added"] == 1 and ledger["edges_added"] == 0
    assert ledger["records"][0]["node_id"] == "Exposure:S05-diet"
    assert ledger["auxiliary_indexes"] == {"node_kind": 1, "search_content": 1, "search_fts": 1,
                                          "exposure_matches": 1, "adjacency_rows_added": 0}
    assert manifest["counts"]["node"] == 5
    with sqlite3.connect(output / "nutriomics.sqlite3") as db, sqlite3.connect(composition["path"]) as parent:
        for table in ("effect", "effect_selector", "edge", "measurement", "source", "perf_adj"):
            assert db.execute("SELECT * FROM " + table).fetchall() == parent.execute("SELECT * FROM " + table).fetchall()
        # Every inherited node's presentation and graph structure are unchanged.
        for original in parent.execute("SELECT * FROM node"):
            actual = db.execute("SELECT * FROM node WHERE id=?", (original[0],)).fetchone()
            assert actual[:5] == original[:5] and actual[6:] == original[6:]
        assert db.execute("SELECT kind FROM node_kind WHERE id='Exposure:S05-diet'").fetchone() == ("diet",)
    with gzip.open(output / "tables/node.csv.gz", "rt", encoding="utf-8", newline="") as stream:
        exported = {r["id"]: r for r in csv.DictReader(stream)}
    assert exported["Exposure:S05-diet"]["degree"] == "0" and exported["Exposure:S05-diet"]["kind"] == "diet"
    with TestClient(create_app(output, allow_candidate=True)) as client:
        listing = client.get("/api/v1/explorer/entities", params={"kind": "exposure", "q": "S05", "tier": "all"})
        assert listing.status_code == 200
        assert {r["id"] for r in listing.json()["items"]} == {"Exposure:S05-diet"}
        detail = client.get("/api/v1/explorer/nodes/Exposure:S05-diet")
        assert detail.status_code == 200
        item = detail.json()
        assert item["metadata"]["original_exposure"]["id"] == "S05-diet"
        assert item["measurements"] == []
        graph = client.get("/api/v1/explorer/graph", params={"seed_id": "Exposure:S05-diet", "tier": "observed"})
        assert graph.status_code == 200
        assert [r["id"] for r in graph.json()["nodes"]] == ["Exposure:S05-diet"]
        assert graph.json()["edges"] == []
        match = client.post("/api/v1/explorer/match", json={"seed_id": "Exposure:S05-diet"})
        assert match.status_code == 200 and len(match.json()["observed_evidence"]) == 1
        assert match.json()["observed_evidence"][0]["mapping_identity"]["status"] == "same_record"
    # These derivative CSVs cannot retain the old index without the new ID.
    for table, id_field in (("node_kind", "id"), ("perf_search_content", "node_id"), ("perf_exposure_match", "node_id")):
        with gzip.open(output / "tables" / (table + ".csv.gz"), "rt", encoding="utf-8", newline="") as stream:
            assert "Exposure:S05-diet" in {r[id_field] for r in csv.DictReader(stream)}


@pytest.mark.parametrize("defect", ["kind", "content", "fts", "match", "source"])
def test_exposure_projection_rejects_stale_auxiliary_indexes_and_source_permissions(composition, defect):
    with sqlite3.connect(composition["path"]) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE node_kind(id TEXT PRIMARY KEY,kind TEXT NOT NULL)")
        db.execute("INSERT INTO node_kind SELECT id,kind FROM node")
        add_graph_read_indexes(db, "base-v2")
    _repin_native_fixture(composition)
    module.build(**composition["kwargs"])
    output = composition["kwargs"]["output"]
    ledger = json.loads((output / "composition/exposure-node-ledger.json").read_text(encoding="utf-8"))
    with sqlite3.connect(output / "nutriomics.sqlite3") as db:
        if defect == "kind":
            db.execute("DELETE FROM node_kind WHERE id='Exposure:S05-diet'")
        elif defect == "content":
            db.execute("UPDATE perf_search_content SET label='Wrong' WHERE node_id='Exposure:S05-diet'")
        elif defect == "fts":
            db.execute("INSERT INTO perf_search(perf_search,rowid,label,label_en,aliases,identifier) "
                       "SELECT 'delete',rowid,label,label_en,aliases,identifier FROM perf_search_content WHERE node_id='Exposure:S05-diet'")
        elif defect == "match":
            db.execute("UPDATE perf_exposure_match SET identity_json='{}' WHERE node_id='Exposure:S05-diet'")
        else:
            db.execute("UPDATE source SET metadata_json=? WHERE id='open'", (canonical({"export_allowed": False}),))
        with pytest.raises(ValueError, match="projection|source identity"):
            module._validate_exposure_projection(db, composition["corrected"], ledger)


def test_nineteen_canonical_registrations_reuse_seventeen_and_add_only_two(composition):
    with sqlite3.connect(composition["path"]) as db:
        db.row_factory = sqlite3.Row
        records = {}
        identifiers = ["R" + str(i) for i in range(17)] + ["S03:flavanols", "N16:green"]
        for index, identifier in enumerate(identifiers):
            record = {"id": identifier, "label": "Source label " + identifier, "food_terms": ["Explicit alias " + identifier],
                      "source_id": "open", "study_id": identifier.split(":")[0]}
            records[("exposure", identifier)] = record
            if index < 17:
                db.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", ("Exposure:" + identifier, "diet", "Curated label " + identifier,
                    None, "[]", canonical({"entity_type": "exposure", "original_exposure": record}), "[]", 0))
        db.execute("DELETE FROM science_record")
        db.executemany("INSERT INTO science_record VALUES(?,?,?)", [(i, k, canonical(r)) for (k, i), r in records.items()])
        db.execute("CREATE TABLE node_kind(id TEXT PRIMARY KEY,kind TEXT NOT NULL)")
        db.execute("INSERT INTO node_kind SELECT id,kind FROM node")
        add_graph_read_indexes(db, "base-v2")
        inherited = dict(db.execute("SELECT id,metadata_json FROM node"))
        planned, ledger = module._plan_exposure_nodes(db, records)
        assert len(planned) == 2 and ledger["registered"] == 19 and ledger["reused"] == 17
        guard = module._install_science(db, records, [], [], [], exposure_nodes=planned)
        ledger["auxiliary_indexes"] = module._insert_exposure_nodes(db, records, planned, guard)
        module._validate_exposure_projection(db, records, ledger)
        for identifier, payload in inherited.items():
            assert db.execute("SELECT metadata_json FROM node WHERE id=?", (identifier,)).fetchone()[0] == payload
        assert {r["id"] for r in planned} == {"Exposure:S03:flavanols", "Exposure:N16:green"}
        assert not guard and ledger["edges_added"] == 0 and db.execute("SELECT count(*) FROM edge").fetchone()[0] == 0


@pytest.mark.parametrize("defect", ["source_missing", "source_denied", "wrong_kind", "wrong_exposure", "payload_id"])
def test_exposure_projection_rejects_source_identity_or_rights_defects(composition, defect):
    records = copy.deepcopy(composition["corrected"])
    exposure = records[("exposure", "S05-diet")]
    with sqlite3.connect(composition["path"]) as db:
        db.row_factory = sqlite3.Row
        if defect == "source_missing":
            exposure["source_id"] = "absent"
        elif defect == "source_denied":
            db.execute("UPDATE source SET metadata_json=? WHERE id='open'", (canonical({"export_allowed": False}),))
        elif defect == "payload_id":
            exposure["id"] = "other"
        else:
            metadata = {"entity_type": "exposure", "original_exposure": {"id": "other" if defect == "wrong_exposure" else "S05-diet"}}
            db.execute("INSERT INTO node VALUES('Exposure:S05-diet',?,'Name',NULL,'[]',?,'[]',0)",
                       ("food" if defect == "wrong_kind" else "diet", canonical(metadata)))
        with pytest.raises(ValueError, match="exposure|Exposure"):
            module._plan_exposure_nodes(db, records)


@pytest.mark.parametrize("operation", ["unplanned", "replace", "effect", "edge"])
def test_exposure_insert_phase_cannot_replace_or_invent_scientific_values(composition, operation):
    with sqlite3.connect(composition["path"]) as db:
        db.row_factory = sqlite3.Row
        planned, _ = module._plan_exposure_nodes(db, composition["corrected"])
        module._install_science(db, composition["corrected"], [], [], [], exposure_nodes=planned)
        with pytest.raises(sqlite3.DatabaseError, match="Unplanned|authorized"):
            if operation == "unplanned":
                db.execute("INSERT INTO node VALUES('fake','diet','Fake',NULL,'[]','{}','[]',0)")
            elif operation == "replace":
                db.execute("INSERT OR REPLACE INTO node VALUES('food:one','diet','Fake',NULL,'[]','{}','[]',0)")
            elif operation == "effect":
                db.execute("UPDATE effect SET effect_estimate=42")
            else:
                db.execute("INSERT INTO edge VALUES('fake','food:one','nutrient:one','causes','observed','approved','9606','{}','[]','[]','[]')")
