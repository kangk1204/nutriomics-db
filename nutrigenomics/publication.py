"""Portable, rights-aware candidate releases; human approval is never inferred."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .explorer import _source
from .explorer_build import canonical, decode, read_only, sha256
from .source_updates import atomic_json, validate_release

BASE_TABLES = ("source", "node", "edge", "measurement", "science_record")
KEY_FIELDS = ("series_accession", "feature_type", "feature_id", "contrast", "analysis_version")
PRIVATE_KEYS = {"raw_json", "password", "token", "api_key", "private_key", "source_file",
                "source_script_path", "environment_lock_path", "core_food_id"}
TEXT_EFFECT = ("series_accession", "feature_type", "feature_id", "feature_label", "contrast",
               "tissue", "timepoint", "intervention", "dose", "duration", "effect_unit", "status",
               "analysis_version")
REAL_EFFECT = ("effect_estimate", "standard_error", "p_value", "q_value")
INT_EFFECT = ("n_people", "n_samples")
EFFECT_FIELDS = ("id", *TEXT_EFFECT, *REAL_EFFECT, *INT_EFFECT, "metadata_json",
                 "source_output_sha256", "rights_policy_id")


def portable(value):
    """Remove operating paths/secrets, preserving scientific nulls and numeric values."""
    if isinstance(value, dict):
        return {k: portable(v) for k, v in value.items() if k.lower() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [portable(v) for v in value]
    if isinstance(value, str) and (value.startswith(("/home/", "/tmp/", "/mnt/")) or
                                   re.match(r"^[A-Za-z]:[\\/]", value)):
        return {"filename": value.replace("\\", "/").rsplit("/", 1)[-1],
                "operational_path_withheld": True}
    return value


def record_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def effect_id(row):
    return "effect:" + record_hash([row[k] for k in KEY_FIELDS])


def _csv(path, header, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=3) as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def _copy_row(row):
    return [canonical(portable(decode(row[k], None))) if k.endswith("_json") and row[k] is not None
            else row[k] for k in row.keys()]  # noqa: SIM118 -- sqlite3.Row iterates values.


def source_reference(db, row):
    """Only citation/permission metadata; links and versions use assessed fallbacks."""
    assessed, value = _source(db, row['id']), decode(row['metadata_json'])
    entry = {'id': row['id'], 'export_allowed': assessed.get('export_allowed') is True}
    for key in ('url', 'original_url', 'version', 'sha256', 'license', 'source_location'):
        entry[key] = portable(value.get(key))
    entry['url'] = portable(assessed.get('url') or value.get('source_url') or entry['url'])
    entry['original_url'] = entry['original_url'] or entry['url']
    entry['version'] = portable(assessed.get('version') or entry['version'])
    for key in ('license', 'rights_policy_sha256', 'rights_reviewed_at'):
        if assessed.get(key) is not None:
            entry[key] = assessed[key]
    if not entry['url']:
        entry['url_missing_reason'] = 'No external URL reported; retain identifier, input hashes and source location'
    if not entry['version']:
        entry['version_missing_reason'] = 'Not reported in source metadata'
    return entry


def reference_node_metadata(value, references):
    """An allowed relationship cannot grant redistribution of its node dictionary."""
    source_id = value.get('source_id')
    source = references.get(source_id, {})
    if 'dictionary' not in value or source.get('export_allowed') is True:
        return value
    safe = {k: v for k, v in value.items() if k != 'dictionary'}
    safe['withheld_source_content'] = {'source_id': source_id, 'fields': ['dictionary'],
        'url': source.get('url'), 'version': source.get('version'),
        'reason': 'Source payload redistribution unconfirmed; identifier/name references remain'}
    return safe


def audit_run(run):
    output_files = decode(run["output_manifest_json"], [])
    if not isinstance(output_files, list):
        output_files = []
    checks = []
    for item in output_files:
        path = Path(item.get("path", ""))
        actual = sha256(path) if path.is_file() else None
        checks.append({"filename": path.name, "expected_sha256": item.get("sha256"),
                       "actual_sha256": actual, "match": bool(actual and actual == item.get("sha256"))})
    provenance_checks = []
    for prefix in ("source_script", "environment_lock"):
        path = Path(run.get(prefix + "_path") or "__absent__")
        actual = sha256(path) if path.is_file() else None
        provenance_checks.append({"role": prefix, "filename": path.name,
                                  "expected_sha256": run.get(prefix + "_sha256"),
                                  "current_sha256": actual,
                                  "match": bool(actual and actual == run.get(prefix + "_sha256"))})
    return {"series_accession": run["series_accession"], "analysis_version": run["analysis_version"],
            "output_checks": checks, "provenance_checks": provenance_checks,
            "all_outputs_match": bool(checks) and all(c["match"] for c in checks),
            "historical_environment_reproducibility": "pending_clean_environment_run",
            "expert_review_status": "pending"}


def _write_review(output, records, sources, audits):
    review = []
    source_by_id = {s["id"]: s for s in sources}
    for row in records:
        if row["kind"] not in {"study", "exposure", "arm", "contrast", "assertion", "analysis_run"}:
            continue
        value = decode(row["metadata_json"])
        origin = source_by_id.get(value.get("source_id"), {})
        review.append({"record_id": row["id"], "record_kind": row["kind"],
                       "record_sha256": record_hash(value), "source_url": origin.get("url"),
                       "source_location_candidate": origin.get("source_location"),
                       "primary_paper_location": None, "decision": "pending", "reviewer": None,
                       "reviewed_at": None, "reason": None, "corrected_record": None,
                       "record": portable(value)})
    atomic_json(output / "review" / "scientific-review.json", review)
    atomic_json(output / "review" / "analysis-audit.json", audits)
    _csv(output / "review" / "scientific-review.csv.gz",
         [k for k in review[0] if k != "record"] if review else ["record_id"],
         ([v for k, v in r.items() if k != "record"] for r in review))


def validate_human_review(records, decisions):
    """Return unresolved records; hashes bind decisions to the exact reviewed data."""
    by_id = {r["record_id"]: r for r in decisions}
    if len(by_id) != len(decisions):
        raise ValueError("duplicate review decisions")
    errors = []
    for expected in records:
        actual = by_id.get(expected["record_id"], {})
        if actual.get("record_sha256") != expected["record_sha256"]:
            errors.append({"id": expected["record_id"], "reason": "review_hash_mismatch"})
        elif actual.get("decision") not in {"accept", "exclude"} or not all(
            actual.get(k) for k in ("reviewer", "reviewed_at", "reason", "primary_paper_location")
        ):
            errors.append({"id": expected["record_id"], "reason": "human_review_pending"})
    return errors


def build_candidate(release, core, policy_path, output, release_id, *, batch_size=5000):
    """Create a new directory atomically; never mutate a source or its WAL."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    release, core, output = Path(release).resolve(), Path(core).resolve(), Path(output).resolve()
    parent_manifest = validate_release(release)
    policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    if output.exists() or output.with_name(output.name + ".building").exists():
        raise FileExistsError("candidate release already exists")
    staging = output.with_name(output.name + ".building")
    for d in (staging, staging / "tables", staging / "effects", staging / "review"):
        d.mkdir(parents=True, exist_ok=True)
    atomic_json(staging / "rights-policy.json", policy)
    effects_schema = pa.schema([pa.field(k, pa.string()) for k in ("id", *TEXT_EFFECT)] +
                              [pa.field(k, pa.float64()) for k in REAL_EFFECT] +
                              [pa.field(k, pa.int64()) for k in INT_EFFECT] +
                              [pa.field(k, pa.string()) for k in EFFECT_FIELDS[-3:]])
    counts, exclusions, audits, catalog = {}, [], [], []
    path = staging / "nutriomics.sqlite3"
    with closing(sqlite3.connect(path)) as dest, read_only(release / "explorer.sqlite") as src, read_only(core) as original:
        # Random SHA identifiers otherwise thrash the default 2 MiB page cache.
        # These are connection-local settings on derived files, not global settings.
        dest.execute("PRAGMA cache_size=-524288")
        dest.execute("PRAGMA synchronous=NORMAL")
        src.execute("PRAGMA cache_size=-262144")
        original.execute("PRAGMA cache_size=-524288")
        # Each read transaction includes committed WAL content without checkpointing the source.
        src.execute("BEGIN")
        original.execute("BEGIN")
        dest.execute("PRAGMA journal_mode=DELETE")
        for table in BASE_TABLES:
            ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if ddl is None:
                raise ValueError("missing required table: " + table)
            dest.execute(ddl[0])
        dest.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)")
        dest.execute("CREATE TABLE effect(" + ",".join(
            # Hashed IDs are random: incremental B-tree maintenance causes
            # millions of random writes. Enforce uniqueness in one bulk index
            # build before validation/activation; failed builds remain private.
            ["id TEXT NOT NULL"] + [f"{k} TEXT" for k in TEXT_EFFECT] +
            [f"{k} REAL" for k in REAL_EFFECT] + [f"{k} INTEGER" for k in INT_EFFECT] +
            [f"{k} TEXT NOT NULL" for k in EFFECT_FIELDS[-3:]]) + ")")
        dest.execute("CREATE TABLE effect_selector(series_accession TEXT,contrast TEXT,analysis_version TEXT,feature_type TEXT,total INTEGER,PRIMARY KEY(series_accession,contrast,analysis_version,feature_type))")
        allowed, nodes = set(), set()
        for row in src.execute("SELECT * FROM source ORDER BY id"):
            entry = source_reference(src, row)
            permitted = entry['export_allowed']
            if permitted:
                allowed.add(row["id"])
            catalog.append(entry)
            dest.execute("INSERT INTO source VALUES(?,?)", (row["id"], canonical(entry)))
        for table in ("measurement", "edge"):
            skipped = 0
            for row in src.execute("SELECT * FROM " + table):
                ids = [row["source_id"]] if table == "measurement" else decode(row["source_ids_json"], [])
                context = decode(row["context_json"]) if table == "edge" else {}
                if not ids or not all(s in allowed for s in ids) or context.get("export_allowed") is False:
                    skipped += 1
                    continue
                nodes.update([row["food"], row["component"]] if table == "measurement" else [row["subject"], row["object"]])
                dest.execute("INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in row) + ")", _copy_row(row))
            exclusions.append({"table": table, "records": skipped, "reason": "redistribution_unconfirmed_or_explicitly_denied"})
        references = {r['id']: r for r in catalog}
        redacted_nodes = 0
        for row in src.execute("SELECT * FROM node"):
            info = decode(row["metadata_json"])
            # Approved identifier-only nodes may be isolated in the dietary
            # graph while still indexing real quantitative expression results.
            identifier_sources = info.get("source_ids", [])
            if (row["kind"] == "gene" and info.get("gene_identity_verified") is True
                    and info.get("feature_identifier_mappings") and identifier_sources
                    and all(value in allowed for value in identifier_sources)):
                nodes.add(row["id"])
            if row["id"] in nodes:
                copied = _copy_row(row)
                safe = reference_node_metadata(info, references)
                redacted_nodes += safe != info
                copied[list(row.keys()).index('metadata_json')] = canonical(portable(safe))
                dest.execute("INSERT INTO node VALUES(" + ",".join("?" for _ in row) + ")", copied)
        exclusions.append({'table': 'node.metadata_json', 'records': redacted_nodes,
            'reason': 'unassessed_dictionary_payload_replaced_with_source_reference'})
        records = [dict(r) for r in src.execute("SELECT * FROM science_record ORDER BY kind,id")]
        for row in records:
            # Project-authored curation is factual metadata; its review remains pending.
            value = decode(row["metadata_json"])
            if row["kind"] == "bridge_source":
                value = next((s for s in catalog if s["id"] == row["id"]), portable(value))
            else:
                value = portable(value)
                value["publication_review_status"] = "pending"
                value["original_record_sha256"] = record_hash(decode(row["metadata_json"]))
            dest.execute("INSERT INTO science_record VALUES(?,?,?)", (row["id"], row["kind"], canonical(value)))
        dest.commit()
        runs = [dict(r) for r in original.execute("SELECT * FROM analysis_run ORDER BY series_accession,analysis_version")]
        policies = {(r["series_accession"], r["analysis_version"]): r for r in policy["analyses"]}
        for run in runs:
            audit = audit_run(run)
            audits.append(audit)
            key = (run["series_accession"], run["analysis_version"])
            permission = policies.get(key)
            if run["status"] != "completed" or not permission or permission.get("export_allowed") is not True or not audit["all_outputs_match"]:
                exclusions.append({"series_accession": key[0], "analysis_version": key[1],
                                   "reason": "policy_unconfirmed_or_output_hash_unverified"})
                continue
            output_hashes = {item["path"]: item["sha256"] for item in decode(run["output_manifest_json"], [])}
            part_id = record_hash(list(key))[:16]
            csv_path = staging / "effects" / (part_id + ".csv.gz")
            parquet_path = staging / "effects" / (part_id + ".parquet")
            total, denied = 0, 0
            with gzip.open(csv_path, "wt", encoding="utf-8", newline="", compresslevel=3) as f, pq.ParquetWriter(parquet_path, effects_schema, compression="zstd") as writer:
                csv_writer = csv.writer(f)
                csv_writer.writerow(EFFECT_FIELDS)
                cursor = original.execute("SELECT * FROM study_effect WHERE series_accession=? AND analysis_version=?", key)
                while batch := cursor.fetchmany(batch_size):
                    values = []
                    for row in batch:
                        metadata = decode(row["metadata_json"])
                        output_hash = output_hashes.get(row["source_file"])
                        if metadata.get("export_allowed") is False or not output_hash or metadata.get("source_file_sha256") != output_hash:
                            denied += 1
                            continue
                        result = {k: row[k] for k in (*TEXT_EFFECT, *REAL_EFFECT, *INT_EFFECT)}
                        result.update(id=effect_id(row), metadata_json=canonical(portable(metadata)),
                                      source_output_sha256=output_hash, rights_policy_id=permission["id"])
                        values.append(result)
                    if values:
                        tuples = [[r[k] for k in EFFECT_FIELDS] for r in values]
                        dest.executemany("INSERT INTO effect VALUES(" + ",".join("?" for _ in EFFECT_FIELDS) + ")", tuples)
                        csv_writer.writerows(tuples)
                        writer.write_table(pa.Table.from_pylist(values, schema=effects_schema))
                        total += len(values)
                    if total % (batch_size * 20) == 0:
                        dest.commit()
                dest.commit()
            audit["public_effects"] = total
            audit["public_partition"] = "effects/" + part_id
            exclusions.append({"series_accession": key[0], "analysis_version": key[1],
                               "records": denied, "reason": "explicit_denial_or_source_output_hash_mismatch"})
            atomic_json(staging / "build-progress.json", {"completed_analysis": key, "public_effects": total})
        dest.execute("CREATE UNIQUE INDEX public_effect_id ON effect(id)")
        dest.execute("CREATE INDEX public_effect_page ON effect(series_accession,analysis_version,contrast,feature_type,feature_id)")
        dest.execute("CREATE INDEX public_effect_feature ON effect(feature_type,feature_id)")
        dest.execute("INSERT INTO effect_selector SELECT series_accession,contrast,analysis_version,feature_type,count(*) FROM effect GROUP BY series_accession,contrast,analysis_version,feature_type")
        for table, fields in (("measurement", "food"), ("edge", "subject"), ("edge", "object"), ("science_record", "kind")):
            dest.execute(f"CREATE INDEX public_{table}_{fields} ON {table}({fields})")
        for table in (*BASE_TABLES, "effect", "effect_selector"):
            counts[table] = dest.execute("SELECT count(*) FROM " + table).fetchone()[0]
        counts["node_kinds"] = dict(dest.execute("SELECT kind,count(*) FROM node GROUP BY kind"))
        counts["relation_tiers"] = dict(dest.execute("SELECT tier,count(*) FROM edge GROUP BY tier"))
        counts["science_kinds"] = dict(dest.execute("SELECT kind,count(*) FROM science_record GROUP BY kind"))
        counts["effect_types"] = dict(dest.execute("SELECT feature_type,count(*) FROM effect GROUP BY feature_type"))
        metadata = {"release_id": release_id, "parent_release_id": parent_manifest["release_id"],
                    "parent_database_sha256": parent_manifest["database_sha256"], "counts": counts,
                    "scope": "rights-filtered food composition, human-intervention curation and project-derived quantitative results",
                    "status": "candidate_not_submission_ready", "expert_review": "pending",
                    "doi": None, "published": False, "blanket_third_party_license": False,
                    "curation_license": "CC-BY-4.0", "code_license": "MIT",
                    "five_part_effect_key": list(KEY_FIELDS), "count_definitions": {
                        "effect": "feature-by-contrast-by-analysis-version records, not independent participants",
                        "study": "curated study records; independent trial membership must be audited",
                        "edge": "source relationships, not validated causal effects"}}
        for k, v in metadata.items():
            dest.execute("INSERT INTO meta VALUES(?,?)", (k, canonical(v)))
        dest.commit()
        for table in (*BASE_TABLES, "effect_selector"):
            columns = [r[1] for r in dest.execute(f"PRAGMA table_info({table})")]
            _csv(staging / "tables" / (table + ".csv.gz"), columns, dest.execute("SELECT * FROM " + table))
        if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("public SQLite integrity failure")
        orphan_edges = dest.execute("SELECT count(*) FROM edge WHERE subject NOT IN (SELECT id FROM node) OR object NOT IN (SELECT id FROM node)").fetchone()[0]
        if orphan_edges or dest.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("public reference integrity failure")
        atomic_json(staging / "dataset-metadata.json", metadata)
        _write_review(staging, records, catalog, audits)
        atomic_json(staging / "source-catalog.json", catalog)
        atomic_json(staging / "exclusions.json", exclusions)
        (staging / "schema.sql").write_text("\n".join(r[0] + ";" for r in dest.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")), encoding="utf-8")
    files = [{"path": str(p.relative_to(staging)).replace(os.sep, "/"), "bytes": p.stat().st_size,
              "sha256": sha256(p)} for p in sorted(staging.rglob("*")) if p.is_file()]
    atomic_json(staging / "manifest.json", {"release_id": release_id, "created_at": datetime.now(UTC).isoformat(),
                "database_sha256": sha256(path), "files": files, "public_counts": counts,
                "publication_gate": "human_review_and_clean_reproduction_pending"})
    with path.open("r+b") as file:
        os.fsync(file.fileno())
    staging.rename(output)
    return metadata
