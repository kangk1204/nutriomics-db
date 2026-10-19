"""Build a new immutable, source-preserving submission candidate from an audited parent.

No human review, DOI registration, or submission readiness is inferred by this builder.
The original scientific measurements/effects and the parent release are never edited.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from nutrigenomics.explorer_build import canonical, read_only, sha256, verify_closed_sqlite
from nutrigenomics.explorer_performance import fingerprint
from nutrigenomics.explorer_performance_build import add_graph_read_indexes
from nutrigenomics.source_updates import atomic_json


def _has_nonempty_wal(database):
    """An RO SQLite reader may leave an empty WAL and a harmless SHM file.

    Any WAL bytes, including an incomplete header, require a separate consistent
    snapshot. Empty WALs are also covered by before/after main-file fingerprints.
    """
    wal = Path(str(database) + "-wal")
    try:
        return wal.is_symlink() or wal.stat().st_size > 0
    except FileNotFoundError:
        return False


def _portable_clinical_config(value):
    """New public facts must not embed operating paths or credentials."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"password", "token", "api_key", "private_key"}:
                raise ValueError("Private credential field in public clinical configuration")
            _portable_clinical_config(item)
    elif isinstance(value, list):
        for item in value:
            _portable_clinical_config(item)
    elif isinstance(value, str) and (value.startswith(("/home/", "/mnt/", "/tmp/", "/Users/", "/private/"))
                                    or re.match(r"^[A-Za-z]:[\\/]", value)):
        raise ValueError("Operating path in public clinical configuration; use a source filename and URL")


def _copy_public_attachments(parent, staging, manifest):
    """Copy only hash-bound, non-symlink public result/policy attachments."""
    declared = {r["path"]: r for r in manifest.get("files", [])}
    required = parent / "rights-policy.json"
    if not required.is_file():
        raise ValueError("Public parent rights policy is required")
    for name in ("effects", "rights-policy.json", "exclusions.json", "reference-payload-audit.json"):
        original = parent / name
        if not original.exists():
            continue
        candidates = list(original.rglob("*")) if original.is_dir() else [original]
        if original.is_symlink() or any(p.is_symlink() for p in candidates):
            raise ValueError("Public attachment symlinks are not allowed")
        for path in candidates:
            if not path.is_file():
                continue
            relative = path.relative_to(parent).as_posix()
            if name == "effects" and not relative.endswith((".csv.gz", ".parquet")):
                raise ValueError("Unexpected file type in public quantitative attachments")
            binding = declared.get(relative)
            if not binding or sha256(path) != binding.get("sha256"):
                raise ValueError("Public attachment has no matching parent manifest hash: " + relative)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            if sha256(target) != binding["sha256"]:
                raise ValueError("Public attachment copy hash mismatch: " + relative)


def build(parent, output, release_id, clinical_config, *, public=False, quantitative_database=None):
    parent, output, clinical_config = (Path(p).resolve() for p in (parent, output, clinical_config))
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    if not release_id or release_id == manifest.get("release_id"):
        raise ValueError("A new distinct release ID is required")
    if output.is_relative_to(parent) or parent.is_relative_to(output):
        raise ValueError("Derived release must be outside the immutable parent directory")
    if public and quantitative_database is not None:
        raise ValueError("Public inventory must use its native public effect selector, not an external core")
    filename = "nutriomics.sqlite3" if public else "explorer.sqlite"
    source = parent / filename
    if output.exists() or output.with_name("." + output.name + ".building").exists():
        raise FileExistsError("Immutable candidate already exists; use a new release ID")
    if source.is_symlink() or _has_nonempty_wal(source):
        raise ValueError("Parent must be an immutable snapshot without nonempty WAL")
    initial_parent = fingerprint(source)
    if sha256(source) != manifest["database_sha256"]:
        raise ValueError("Parent SHA-256 mismatch")
    if fingerprint(source) != initial_parent:
        raise ValueError("Parent changed during SHA-256 validation")
    size = source.stat().st_size
    if shutil.disk_usage(output.parent).free < size * 3 + 2 * 1024**3:
        raise ValueError("Insufficient permanent disk space for staged release")
    if Path("/proc/meminfo").exists():
        available = int(next(x.split()[1] for x in Path("/proc/meminfo").read_text().splitlines()
                             if x.startswith("MemAvailable:"))) * 1024
        if available < size * 2 + 6 * 1024**3:
            raise ValueError("Insufficient available RAM for bounded sequential staging")
    staging = output.with_name("." + output.name + ".building")
    staging.mkdir(parents=True)
    atomic_json(staging / "build-state.json", {"phase": "copy_parent", "parent_sha256": manifest["database_sha256"]})
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        with closing(read_only(source)) as original:
            original.backup(db)
        if fingerprint(source) != initial_parent or _has_nonempty_wal(source):
            raise ValueError("Parent changed during immutable backup")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA cache_size=-524288")
        protected = {"measurement", "effect", "effect_selector", "contrast_selector", "study_effect",
                     "perf_effect_ref", "perf_effect_selector", "perf_effect_type"}
        existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        before = {t: db.execute('SELECT count(*) FROM "' + t + '"').fetchone()[0]
                  for t in sorted(protected & existing)}
        inherited_science = {(r[0], r[1]): r[2] for r in db.execute(
            "SELECT kind,id,metadata_json FROM science_record")}
        denied = []
        def guard(action, table, column, database, trigger):
            target = column if action == sqlite3.SQLITE_ALTER_TABLE else table
            if ((target in protected and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
                    sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_ALTER_TABLE))
                    or (target == "science_record" and action in (sqlite3.SQLITE_UPDATE,
                    sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE))):
                denied.append([action, target])
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(guard)
        from nutrigenomics.clinical_endpoints import attach_clinical_endpoints
        config = json.loads(clinical_config.read_text(encoding="utf-8"))
        if public:
            _portable_clinical_config(config)
        clinical = attach_clinical_endpoints(db, config)
        atomic_json(staging / "clinical-integration.json", clinical)
        print(canonical({"phase": "clinical_integrated", "summary": clinical}), flush=True)
        db.execute("DROP TABLE IF EXISTS perf_search")
        for name in ("perf_search_content", "perf_adj", "perf_exposure_match", "node_kind"):
            db.execute('DROP TABLE IF EXISTS "' + name + '"')
        for name in ("perf_edge_tier", "perf_node_label", "perf_node_degree"):
            db.execute('DROP INDEX IF EXISTS "' + name + '"')
        db.execute("CREATE TABLE node_kind(id TEXT PRIMARY KEY,kind TEXT NOT NULL)")
        db.execute("INSERT INTO node_kind SELECT id,kind FROM node")
        db.execute("UPDATE node SET degree=(SELECT count(*) FROM edge WHERE subject=node.id)+(SELECT count(*) FROM edge WHERE object=node.id)")
        indexes = add_graph_read_indexes(db, release_id)
        previous_metadata = json.loads((parent / "dataset-metadata.json").read_text(encoding="utf-8")) if public else {}
        from nutrigenomics.release_contract import build_release_contract, install_release_contract
        quantitative_path = Path(quantitative_database).resolve() if quantitative_database else None
        if quantitative_path and _has_nonempty_wal(quantitative_path):
            raise ValueError("Quantitative source must be an immutable snapshot without nonempty WAL")
        core_identity = fingerprint(quantitative_path) if quantitative_path else None
        if "perf_effect_ref" in existing:
            performance_row = db.execute("SELECT value_json FROM meta WHERE key='performance'").fetchone()
            performance = json.loads(performance_row[0]) if performance_row else {}
            if (core_identity is None or performance.get("core_fingerprint") != json.loads(canonical(core_identity))):
                raise ValueError("Inherited quantitative index requires its exact unchanged core fingerprint")
            performance.update(indexed_for_release=release_id,
                rebound_from_release_id=performance.get("indexed_for_release"),
                effect_reference_reuse="Unchanged protected selector/reference tables; exact core fingerprint revalidated")
            db.execute("UPDATE meta SET value_json=? WHERE key='performance'", (canonical(performance),))
        quantitative = read_only(quantitative_path) if quantitative_path else None
        try:
            contract = build_release_contract(db, release_id=release_id,
                scope="public" if public else "internal", previous_metadata=previous_metadata,
                manifest=manifest, quantitative_db=quantitative)
            if quantitative_path and (fingerprint(quantitative_path) != core_identity or _has_nonempty_wal(quantitative_path)):
                raise ValueError("Quantitative source changed during inventory")
        finally:
            if quantitative is not None:
                quantitative.close()
        install_release_contract(db, contract)
        created_at = datetime.now(UTC).isoformat()
        db.execute("INSERT OR REPLACE INTO meta VALUES('created_at',?)", (canonical(created_at),))
        atomic_json(staging / "release-contract.json", contract)
        after = {t: db.execute('SELECT count(*) FROM "' + t + '"').fetchone()[0] for t in before}
        if before != after or denied:
            raise ValueError("Protected quantitative tables were modified")
        final_science = {(r[0], r[1]): r[2] for r in db.execute("SELECT kind,id,metadata_json FROM science_record")}
        if any(final_science.get(key) != value for key, value in inherited_science.items()):
            raise ValueError("Inherited scientific curation bytes were modified")
        preservation = {"status": "passed", "protected_table_counts_before": before,
            "protected_table_counts_after": after, "denied_writes": denied,
            "method": "Read-only parent backup plus SQLite authorizer denies scientific value writes",
            "parent_sha256": manifest["database_sha256"], "parent_unchanged_during_copy": True,
            "inherited_science_records_preserved": len(inherited_science),
            "expert_semantic_accuracy": "not_evaluated"}
        atomic_json(staging / "quantitative-preservation.json", preservation)
        # No parent review approval is inferred; final targets are bound to their actual current bytes.
        from nutrigenomics.reporting_review import build_review_package
        build_review_package(db, staging / "review")
        if public:
            _copy_public_attachments(parent, staging, manifest)
        from nutrigenomics.publication import _csv
        tables = staging / "tables"
        tables.mkdir()
        for name in ("node", "edge", "source", "measurement", "science_record", "meta",
                     "workbench_evidence", "candidate_document", "effect_selector", "contrast_selector",
                     "food_alias", "composition_assessment"):
            if name not in existing and name not in {"node", "edge", "source", "measurement", "science_record", "meta"}:
                continue
            cursor = db.execute('SELECT * FROM "' + name + '"')
            _csv(tables / (name + ".csv.gz"), [c[0] for c in cursor.description], cursor)
        (staging / "schema.sql").write_text("\n".join(r[0] + ";" for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")), encoding="utf-8")
        dictionary = {"release_id": release_id, "count_definitions": contract["count_definitions"],
            "csv_null_convention": "Empty cells represent SQLite NULL or an empty string; use SQLite or Parquet to recover exact types.",
            "nested_json": "Columns ending in _json retain source and context structures as JSON text.",
            "effect_identity": ["series_accession", "feature_type", "feature_id", "contrast", "analysis_version"],
            "interpretation": "Results are feature-by-contrast-by-analysis-version records, not people. Clinical summaries do not imply mediation or personal prediction.",
            "tables": {}}
        for table in sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")):
            quoted = '"' + table.replace('"', '""') + '"'
            dictionary["tables"][table] = {"columns": [{"name": r[1], "sqlite_type": r[2],
                "not_null": bool(r[3]), "default": r[4], "primary_key_order": r[5]}
                for r in db.execute("PRAGMA table_info(" + quoted + ")")],
                "foreign_keys": [dict(r) for r in db.execute("PRAGMA foreign_key_list(" + quoted + ")")]}
        atomic_json(staging / "data-dictionary.json", dictionary)
        atomic_json(staging / "source-catalog.json", [{**json.loads(r["metadata_json"]), "id": r["id"]}
                                                    for r in db.execute("SELECT * FROM source ORDER BY id")])
        atomic_json(staging / "clinical-endpoints.json", config)
        db.commit()
        check = db.execute("PRAGMA integrity_check").fetchall()
        if [r[0] for r in check] != ["ok"] or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Staging integrity/foreign-key check failed")
        print(canonical({"phase": "sequential_permanent_snapshot"}), flush=True)
        with closing(sqlite3.connect(staging / filename)) as target:
            db.backup(target, pages=4096)
            target.commit()
        # Reopen the actual permanent snapshot, not only the RAM database.
        verify_closed_sqlite(staging / filename)
        from nutrigenomics.release_contract import metadata_with_contract
        dataset = metadata_with_contract(db, previous_metadata)
        atomic_json(staging / "dataset-metadata.json", dataset)
        atomic_json(staging / "coverage.json", {"release_id": release_id, "counts": contract["counts"]})
        current = {**manifest, **contract, "created_at": created_at,
            "database_sha256": sha256(staging / filename), "clinical_integration": clinical,
            "quantitative_preservation": preservation, "graph_performance": indexes,
            "builder_sha256": sha256(Path(__file__)), "clinical_config_sha256": sha256(clinical_config),
            "status": "candidate_pending_expert_review", "journal_submission_ready": False}
        performance_row = db.execute("SELECT value_json FROM meta WHERE key='performance'").fetchone()
        if performance_row:
            current["performance"] = json.loads(performance_row[0])
        if public:
            current["public_counts"] = contract["counts"]
        current.pop("files", None)
        atomic_json(staging / "manifest.json", current)
        atomic_json(staging / "build-state.json", {"phase": "complete", "database_sha256": current["database_sha256"]})
        current["files"] = [{"path": p.relative_to(staging).as_posix(), "bytes": p.stat().st_size,
            "sha256": sha256(p)} for p in sorted(staging.rglob("*")) if p.is_file() and p != staging / "manifest.json"]
        atomic_json(staging / "manifest.json", current)
        os.replace(staging, output)
        return current
    except BaseException as exc:
        atomic_json(staging / "build-failure.json", {"status": "failed", "error": str(exc)})
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("parent", "output", "clinical-config"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--quantitative-database", type=Path)
    args = parser.parse_args()
    result = build(args.parent, args.output, args.release_id, args.clinical_config,
                   public=args.public, quantitative_database=args.quantitative_database)
    print(canonical({"release_id": result["release_id"], "database_sha256": result["database_sha256"]}), flush=True)
