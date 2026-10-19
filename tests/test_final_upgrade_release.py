"""Small real SQLite staging builds: no remote server or raw omics required."""
import copy
import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from nutrigenomics.explorer_build import SCHEMA, canonical, sha256
from nutrigenomics.explorer_performance import fingerprint, indexed_source
from scripts import build_final_upgrade_release as builder


@pytest.fixture
def inputs(tmp_path):
    config = json.loads((Path(__file__).resolve().parents[1] / "config/clinical_endpoints.v1.json").read_text(encoding="utf-8"))
    record = copy.deepcopy(next(r for r in config["records"] if r["study_id"] == "S03"))
    config["records"] = [record]
    config["study_audits"] = [a for a in config["study_audits"] if a["study_id"] == "S03"]
    config["sources"] = [s for s in config["sources"] if s["id"] in record["source_ids"]]
    config_path = tmp_path / "clinical.json"
    config_path.write_text(canonical(config), encoding="utf-8")
    return tmp_path, config_path, config


def parent_release(folder, public=False):
    folder.mkdir()
    path = folder / ("nutriomics.sqlite3" if public else "explorer.sqlite")
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.executescript("""
          CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT,PRIMARY KEY(kind,id));
          CREATE TABLE effect(id TEXT PRIMARY KEY,feature_type TEXT,value REAL,q_value REAL);
          CREATE VIEW study_effect AS SELECT * FROM effect;
          CREATE TABLE effect_selector(series_accession TEXT,contrast TEXT,analysis_version TEXT,feature_type TEXT,total INTEGER);
        """)
        db.executemany("INSERT INTO meta VALUES(?,?)", [("release_id", canonical("parent-r")),
            ("dataset_release_id", canonical("old-alias-r")), ("counts", canonical({"node": 999})),
            ("graph_performance", canonical({"indexed_for_release": "stale-r"}))])
        db.execute("INSERT INTO source VALUES('own',?)", (canonical({"id": "own", "url": "https://example.org/own",
            "export_allowed": True, "version": "v1", "sha256": "a" * 64}),))
        for identifier, kind in (("food", "food"), ("component", "nutrient")):
            db.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", (identifier, kind, identifier, identifier, "[]", "{}", "[]", 1))
        db.execute("INSERT INTO edge VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("composition", "food", "component",
            "contains_nutrient", "observed_composition", "pending", None, "{}", '["own"]', "[]", "[]"))
        db.execute("INSERT INTO measurement VALUES(?,?,?,?,?,?,?,?,?)", ("m", "food", "component", None,
            "g", "source_100g", "not_measured", "own", "{}"))
        for kind, value in (("study", {"id": "S03", "title": "Source trial", "metadata": {"series": ["GSE54325"]}}),
                            ("analysis_run", {"id": 1, "series_accession": "GSE54325", "status": "completed"})):
            db.execute("INSERT INTO science_record VALUES(?,?,?)", (str(value["id"]), kind, canonical(value)))
        db.executemany("INSERT INTO effect VALUES(?,?,?,?)", [("x", "gene", -.125, .72), ("y", "cpg", None, None)])
        db.executemany("INSERT INTO effect_selector VALUES(?,?,?,?,?)", [("GSE54325", "after", "v1", "gene", 1),
                                                                       ("GSE54325", "after", "v1", "cpg", 1)])
    files = []
    if public:
        (folder / "dataset-metadata.json").write_text(canonical({"release_id": "parent-r", "expert_review": "pending",
            "graph_performance": {"indexed_for_release": "old-file-r"}, "counts": {"node": 1234}}), encoding="utf-8")
        (folder / "rights-policy.json").write_text(canonical({"analyses": [{"id": "own", "export_allowed": True}]}))
        (folder / "effects").mkdir()
        with gzip.open(folder / "effects/native.csv.gz", "wt") as f:
            f.write("id,value,q_value\nx,-.125,.72\ny,,\n")
        for p in [folder / "rights-policy.json", folder / "effects/native.csv.gz"]:
            files.append({"path": p.relative_to(folder).as_posix(), "sha256": sha256(p), "bytes": p.stat().st_size})
        (folder / "private-unrelated.txt").write_text("PRIVATE_PARENT_ONLY")
    manifest = {"release_id": "parent-r", "parent_release_id": "grandparent-r",
                "parent_database_sha256": "b" * 64, "database_sha256": sha256(path), "files": files}
    (folder / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    return path, manifest


@pytest.mark.parametrize("public", [False, True])
def test_real_build_preserves_source_values_and_binds_review_nested_manifest(inputs, public, monkeypatch):
    root, config_path, _ = inputs
    parent = root / "parent"
    original, manifest = parent_release(parent, public)
    before_hash = sha256(original)
    with sqlite3.connect(original) as db:
        effect_before = db.execute("SELECT * FROM effect ORDER BY id").fetchall()
        measurement_before = db.execute("SELECT * FROM measurement").fetchall()
        science_before = db.execute("SELECT * FROM science_record ORDER BY kind,id").fetchall()
    reopened = []
    real_verify = builder.verify_closed_sqlite
    def verify(path):
        reopened.append(Path(path))
        real_verify(path)
    monkeypatch.setattr(builder, "verify_closed_sqlite", verify)
    output = root / "new"
    result = builder.build(parent, output, "new-r", config_path, public=public)
    assert sha256(original) == before_hash
    assert reopened == [root / ".new.building" / original.name]
    assert result["parent_release_id"] == "parent-r" and result["parent_database_sha256"] == manifest["database_sha256"]
    assert result["journal_submission_ready"] is False
    assert result["quantitative_preservation"]["inherited_science_records_preserved"] == 2
    files = {r["path"]: r for r in result["files"]}
    assert "review/manifest.json" in files and "manifest.json" not in files
    dictionary = json.loads((output / "data-dictionary.json").read_text(encoding="utf-8"))
    assert dictionary["release_id"] == "new-r"
    assert dictionary["tables"]["effect"]["columns"][0]["name"] == "id"
    assert "NULL" in dictionary["csv_null_convention"]
    for path, binding in files.items():
        assert sha256(output / path) == binding["sha256"]
    with sqlite3.connect(output / original.name) as db:
        assert db.execute("SELECT * FROM effect ORDER BY id").fetchall() == effect_before
        assert db.execute("SELECT * FROM measurement").fetchall() == measurement_before
        for row in science_before:
            assert db.execute("SELECT * FROM science_record WHERE id=? AND kind=?", row[:2]).fetchone() == row
        actual_node = db.execute("SELECT count(*) FROM node").fetchone()[0]
        actual_science = db.execute("SELECT count(*) FROM science_record").fetchone()[0]
    metadata = json.loads((output / "dataset-metadata.json").read_text(encoding="utf-8"))
    contract = json.loads((output / "release-contract.json").read_text(encoding="utf-8"))
    assert metadata["counts"] == result["counts"] == contract["counts"]
    assert metadata["counts"]["node"] == actual_node and metadata["counts"]["science_record"] == actual_science == 4
    assert metadata["graph_performance"]["indexed_for_release"] == "new-r"
    assert metadata["release_id"] == metadata["dataset_release_id"] == result["release_id"] == "new-r"
    assert metadata["created_at"] == result["created_at"]
    reviews = json.loads((output / "review/scientific-review.json").read_text(encoding="utf-8"))
    assert len(reviews) == 4 and all(r["decision"] == "pending" and not r["reviewer_attestation"] for r in reviews)
    sources = json.loads((output / "source-catalog.json").read_text(encoding="utf-8"))
    assert all("id" in r and "metadata_json" not in r for r in sources)
    assert json.loads((output / "coverage.json").read_text())["release_id"] == "new-r"
    if public:
        assert sha256(output / "effects/native.csv.gz") == sha256(parent / "effects/native.csv.gz")
        assert not (output / "private-unrelated.txt").exists()
        assert (output / "tables/effect_selector.csv.gz").is_file()


@pytest.mark.parametrize("sql", ["UPDATE effect SET value=8", "ALTER TABLE effect RENAME TO changed",
                                 "DROP VIEW study_effect", "UPDATE science_record SET metadata_json='{}'",
                                 "INSERT OR REPLACE INTO science_record VALUES('S03','study','{}')"])
def test_inherited_values_and_curation_cannot_be_replaced(inputs, monkeypatch, sql):
    from nutrigenomics import clinical_endpoints
    root, config_path, _ = inputs
    parent = root / "parent"
    source, _ = parent_release(parent)
    before = sha256(source)
    def bad_import(db, config):
        db.execute(sql)
        return {"records": 0}
    monkeypatch.setattr(clinical_endpoints, "attach_clinical_endpoints", bad_import)
    with pytest.raises((sqlite3.DatabaseError, ValueError)):
        builder.build(parent, root / "new", "new-r", config_path)
    assert sha256(source) == before and not (root / "new").exists()


def test_public_unmanifested_attachments_and_operating_paths_fail_closed(inputs):
    root, config_path, config = inputs
    parent = root / "parent"
    parent_release(parent, True)
    (parent / "effects/unlisted.csv.gz").write_bytes(b"UNDECLARED")
    with pytest.raises(ValueError, match="parent manifest hash"):
        builder.build(parent, root / "bad-attachments", "new-r", config_path, public=True)
    config["sources"][0]["source_file"] = "/home/lab/private/source.xml"
    config_path.write_text(canonical(config), encoding="utf-8")
    with pytest.raises(ValueError, match="Operating path"):
        builder.build(parent, root / "bad-config", "new-r", config_path, public=True)


def test_output_cannot_write_inside_parent_or_reuse_id_or_override_public_scope(inputs):
    root, config_path, _ = inputs
    parent = root / "parent"
    parent_release(parent, True)
    with pytest.raises(ValueError, match="outside"):
        builder.build(parent, parent / "new", "new-r", config_path, public=True)
    with pytest.raises(ValueError, match="distinct"):
        builder.build(parent, root / "new", "parent-r", config_path, public=True)
    with pytest.raises(ValueError, match="native public effect selector"):
        builder.build(parent, root / "new", "new-r", config_path, public=True, quantitative_database=root / "private-core")


def test_internal_quantitative_reference_binding_is_revalidated_for_new_release(inputs):
    root, config_path, _ = inputs
    parent = root / "parent"
    source, manifest = parent_release(parent)
    core = root / "core.sqlite3"
    with sqlite3.connect(core) as db:
        db.executescript("CREATE TABLE study_effect(feature_type TEXT); INSERT INTO study_effect VALUES('gene'),('cpg');")
    identity = json.loads(canonical(fingerprint(core)))
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE perf_effect_ref(id INTEGER); CREATE TABLE perf_effect_selector(total INTEGER); INSERT INTO perf_effect_selector VALUES(1);")
        db.execute("INSERT INTO meta VALUES('performance',?)", (canonical({"indexed_for_release": "parent-r", "core_fingerprint": identity}),))
    # Read-only SQLite services may leave zero WAL + SHM artifacts. The main
    # immutable source and its effective WAL fingerprint remain unchanged.
    Path(str(core) + "-wal").write_bytes(b"")
    Path(str(core) + "-shm").write_bytes(b"\0" * 32768)
    assert json.loads(canonical(fingerprint(core))) == identity
    manifest["database_sha256"] = sha256(source)
    (parent / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="exact unchanged core fingerprint"):
        builder.build(parent, root / "no-core", "new-r", config_path)
    result = builder.build(parent, root / "new", "new-r", config_path, quantitative_database=core)
    with sqlite3.connect(root / "new/explorer.sqlite") as db:
        assert indexed_source(db, core)
    assert result["performance"]["indexed_for_release"] == "new-r"
    assert result["performance"]["rebound_from_release_id"] == "parent-r"
    assert result["counts"]["effect"] == 2 and result["counts"]["indexed_effect_rows"] == 1


@pytest.mark.parametrize("wal_bytes", [b"", b"x", b"x" * 31, b"x" * 32, b"x" * 4096])
def test_empty_reader_wal_is_distinct_from_any_nonempty_wal(tmp_path, wal_bytes):
    path = tmp_path / "core.sqlite3"
    path.write_bytes(b"fixture")
    Path(str(path) + "-wal").write_bytes(wal_bytes)
    Path(str(path) + "-shm").write_bytes(b"\0" * 32768)
    assert builder._has_nonempty_wal(path) is bool(wal_bytes)


def test_parent_empty_reader_wal_allowed_but_nonempty_rejected(inputs):
    root, config_path, _ = inputs
    parent = root / "parent"
    source, manifest = parent_release(parent)
    Path(str(source) + "-wal").write_bytes(b"")
    Path(str(source) + "-shm").write_bytes(b"\0" * 32768)
    result = builder.build(parent, root / "empty-wal", "new-r", config_path)
    assert result["parent_database_sha256"] == manifest["database_sha256"]
    assert sha256(source) == manifest["database_sha256"]
    Path(str(source) + "-wal").write_bytes(b"x")
    with pytest.raises(ValueError, match="nonempty WAL"):
        builder.build(parent, root / "nonempty-wal", "new-r2", config_path)
    assert not (root / "nonempty-wal").exists()


def test_nonempty_quantitative_wal_rejected_even_shorter_than_fingerprint_header(inputs):
    root, config_path, _ = inputs
    parent = root / "parent"
    parent_release(parent)
    core = root / "core.sqlite3"
    with sqlite3.connect(core) as db:
        db.executescript("CREATE TABLE study_effect(feature_type TEXT); INSERT INTO study_effect VALUES('gene');")
    db.close()
    Path(str(core) + "-wal").write_bytes(b"x")
    assert fingerprint(core)[2] is None
    with pytest.raises(ValueError, match="nonempty WAL"):
        builder.build(parent, root / "nonempty-core", "new-r", config_path, quantitative_database=core)
    assert not (root / "nonempty-core").exists()
