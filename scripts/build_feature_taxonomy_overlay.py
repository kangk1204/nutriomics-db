"""Build a separate, atomic, hash-bound feature-display overlay by streaming sources.

Only source-key REFERENCES, classification metadata and digests are stored. No
effect estimates, P/FDR, NA substitutions, expert decisions or source edits occur.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from nutrigenomics.feature_taxonomy import (
    SCHEMA_VERSION,
    annotate_effect_record,
    effect_key,
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest_item(digest, value) -> None:
    encoded = canonical(value)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def validate_manifest(manifest: Mapping, output: Path) -> list[dict]:
    if manifest.get("schema_version") != "nutriomics-feature-taxonomy-source-manifest-v1":
        raise ValueError("Unsupported source manifest")
    if not isinstance(manifest.get("release_id"), str) or not manifest["release_id"]:
        raise ValueError("An exact source release identifier is required")
    if not isinstance(manifest.get("overlay_release_id"), str) or not manifest["overlay_release_id"]:
        raise ValueError("An exact derived overlay identifier is required")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("At least one explicit frozen source is required")
    seen = set()
    for source in sources:
        for field in ("series_accession", "analysis_version", "source_feature_type"):
            if not isinstance(source.get(field), str) or not source[field]:
                raise ValueError("Missing original source selector: " + field)
        path = Path(source["path"]).resolve(strict=True)
        if path.is_relative_to(output) or output.is_relative_to(path):
            raise ValueError("Source and output locations overlap")
        if path in seen or not path.is_file():
            raise ValueError("Duplicate or non-file source")
        seen.add(path)
        if (not isinstance(source.get("sha256"), str) or len(source["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in source["sha256"])):
            raise ValueError("Every source requires an exact SHA256")
        if not isinstance(source.get("expected_rows"), int) or source["expected_rows"] < 1:
            raise ValueError("Every source requires a positive frozen row count")
        producer = source.get("source_producer")
        if producer and sha256(Path(producer["path"]).resolve(strict=True)) != producer["sha256"]:
            raise ValueError("Frozen source-analysis code differs from its declared hash")
        if (producer and source.get("region_definition") and
                source["region_definition"]["source_sha256"] != producer["sha256"]):
            raise ValueError("Region declaration does not match the actual frozen producer")
    return sources


def annotation_records(manifest: Mapping, sources: list[dict]) -> tuple[list[dict], dict | None]:
    binding = manifest.get("annotation_binding")
    if binding is None:
        return [], None
    path = Path(binding["path"]).resolve(strict=True)
    actual = sha256(path)
    if actual != binding["sha256"]:
        raise ValueError("Annotation binding source changed")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    records = []
    by_id = {s.get("source_analysis_id"): s for s in sources}
    for group in payload["records"]:
        source = by_id.get(group["parent_analysis_id"])
        if source is None or source["series_accession"] != group["series"]:
            raise ValueError("Annotation binding belongs to another analysis")
        for example in group["examples"]:
            if not example["annotation_id_exact_match"] or not example["manifest_id_exact_match"]:
                raise ValueError("Example source identity was not verified")
            if (example["original"]["feature_type"] != source["source_feature_type"]
                    or example["annotation_record"].get("Name") != example["original"]["feature_id"]):
                raise ValueError("Annotation record does not identify the actual original feature")
            records.append({
                "series_accession": source["series_accession"],
                "analysis_version": source["analysis_version"],
                "source_feature_type": example["original"]["feature_type"],
                "feature_id": example["original"]["feature_id"],
                "annotation_identity_status": "exact_id_verified_in_frozen_installed_sources",
                "chemical_context": "unconfirmed", "review_status": "not_human_approved",
                "source_binding_sha256": actual,
                "annotation_source": group["annotation_source"],
                "manifest_source": group["manifest_source"],
                "annotation_record": example["annotation_record"],
            })
    return records, {"path": str(path), "sha256": actual, "examples": len(records)}


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    PRAGMA journal_mode=DELETE;
    PRAGMA synchronous=FULL;
    PRAGMA foreign_keys=ON;
    PRAGMA cache_size=-16384;
    PRAGMA temp_store=FILE;
    CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT NOT NULL) WITHOUT ROWID;
    CREATE TABLE feature_taxonomy_raw(
      source_feature_type TEXT NOT NULL, feature_id TEXT NOT NULL,
      normalized_feature_type TEXT NOT NULL, identifier_class TEXT NOT NULL,
      chemical_context TEXT NOT NULL);
    CREATE TABLE source_selector(
      id INTEGER PRIMARY KEY, series_accession TEXT NOT NULL, source_feature_type TEXT NOT NULL,
      contrast TEXT NOT NULL, analysis_version TEXT NOT NULL, region_definition_json TEXT,
      UNIQUE(series_accession,source_feature_type,contrast,analysis_version));
    CREATE TABLE effect_membership(
      selector_id INTEGER NOT NULL REFERENCES source_selector(id), feature_id TEXT NOT NULL);
    CREATE TABLE source_input(
      ordinal INTEGER PRIMARY KEY, path TEXT NOT NULL, sha256_before TEXT NOT NULL,
      sha256_after TEXT NOT NULL, rows INTEGER NOT NULL, source_keys_sha256 TEXT NOT NULL,
      original_values_sha256 TEXT NOT NULL, preserved_values_sha256 TEXT NOT NULL);
    CREATE TABLE annotation_identity(
      series_accession TEXT NOT NULL, analysis_version TEXT NOT NULL,
      source_feature_type TEXT NOT NULL, feature_id TEXT NOT NULL, evidence_json TEXT NOT NULL,
      PRIMARY KEY(series_accession,analysis_version,source_feature_type,feature_id)) WITHOUT ROWID;
    """)


def build_overlay(manifest_path: Path, output: Path, *, batch_size: int = 2000) -> dict:
    start = time.monotonic()
    if output.exists():
        raise ValueError("Output already exists; select a NEW immutable overlay directory")
    if batch_size < 1 or batch_size > 20_000:
        raise ValueError("Batch size must be between 1 and 20000")
    manifest_path = manifest_path.resolve(strict=True)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    output = output.resolve()
    sources = validate_manifest(manifest, output)
    annotation, binding = annotation_records(manifest, sources)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(output.name + ".building-" + uuid.uuid4().hex)
    staging.mkdir()
    (staging / "source-manifest.json").write_bytes(manifest_bytes)
    db = sqlite3.connect(staging / "taxonomy.sqlite3")
    counts = {"source_feature_types": Counter(), "normalized_feature_types": Counter(),
              "methylation_probe_identifier_classes": Counter({"cg": 0, "ch": 0, "unrecognized": 0})}
    selectors, selector_stats, inputs = {}, {}, []
    taxonomy_batch, member_batch = [], []

    def flush():
        # Original probe order is genomic, not identifier order. Append first;
        # sorted bulk index creation avoids random per-row journal amplification.
        db.executemany("INSERT INTO feature_taxonomy_raw VALUES(?,?,?,?,?)", taxonomy_batch)
        db.executemany("INSERT INTO effect_membership VALUES(?,?)", member_batch)
        db.commit()
        taxonomy_batch.clear()
        member_batch.clear()

    try:
        create_schema(db)
        for ordinal, source in enumerate(sources, 1):
            path = Path(source["path"]).resolve(strict=True)
            before = sha256(path)
            if before != source["sha256"]:
                raise ValueError("Frozen source SHA mismatch: " + str(path))
            hashes = {name: hashlib.sha256() for name in ("keys", "original", "preserved")}
            rows, local_counts = 0, {name: Counter({k: 0 for k in counter})
                                     for name, counter in counts.items()}
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
                headers = reader.fieldnames
                if not headers or len(headers) != len(set(headers)) or not {
                    "feature_type", "feature_id", "contrast"}.issubset(headers):
                    raise ValueError("Missing or duplicate effect-table headers")
                digest_item(hashes["original"], headers)
                digest_item(hashes["preserved"], headers)
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("Malformed effect table row")
                    original = tuple(row[field] for field in headers)
                    key = effect_key(row, series_accession=source["series_accession"],
                                     analysis_version=source["analysis_version"])
                    if key[1] != source["source_feature_type"]:
                        raise ValueError("Source feature kind differs from explicit input manifest")
                    decorated = annotate_effect_record(row, series_accession=key[0],
                        analysis_version=key[4], overlay_release_id=manifest["overlay_release_id"],
                        region_definition=source.get("region_definition"))
                    preserved = tuple(decorated[field] for field in headers)
                    if original != preserved or tuple(row[field] for field in headers) != original:
                        raise AssertionError("A source value was changed by the taxonomy adapter")
                    if effect_key(decorated, series_accession=key[0], analysis_version=key[4]) != key:
                        raise AssertionError("A source five-part key was changed")
                    digest_item(hashes["keys"], key)
                    digest_item(hashes["original"], original)
                    digest_item(hashes["preserved"], preserved)
                    selector = (key[0], key[1], key[3], key[4])
                    if selector not in selectors:
                        cursor = db.execute("INSERT INTO source_selector(series_accession,source_feature_type,contrast,analysis_version,region_definition_json) VALUES(?,?,?,?,?)",
                            (*selector, json.dumps(source.get("region_definition"))))
                        selectors[selector] = cursor.lastrowid
                        selector_stats[selector] = {"rows": 0, "keys": hashlib.sha256()}
                    selector_stats[selector]["rows"] += 1
                    digest_item(selector_stats[selector]["keys"], key)
                    taxonomy_batch.append((key[1], key[2], decorated["normalized_feature_type"],
                                           decorated["identifier_class"], decorated["chemical_context"]))
                    member_batch.append((selectors[selector], key[2]))
                    for counter in (counts, local_counts):
                        counter["source_feature_types"][key[1]] += 1
                        counter["normalized_feature_types"][decorated["normalized_feature_type"]] += 1
                        if key[1] == "cpg":
                            counter["methylation_probe_identifier_classes"][decorated["identifier_class"]] += 1
                    rows += 1
                    if len(member_batch) >= batch_size:
                        flush()
            flush()
            after = sha256(path)
            if after != before or rows != source["expected_rows"]:
                raise ValueError("Source changed or full source row count differs")
            digests = {name: digest.hexdigest() for name, digest in hashes.items()}
            if digests["original"] != digests["preserved"]:
                raise AssertionError("Source-value preservation digest differs")
            db.execute("INSERT INTO source_input VALUES(?,?,?,?,?,?,?,?)", (
                ordinal, str(path), before, after, rows, digests["keys"],
                digests["original"], digests["preserved"]))
            inputs.append({**source, "path": str(path), "sha256_before": before, "sha256_after": after,
                           "rows_scanned": rows, "digests": digests,
                           "counts": {k: dict(v) for k, v in local_counts.items()}})
        db.commit()
        try:
            db.execute("CREATE UNIQUE INDEX effect_membership_key ON effect_membership(selector_id,feature_id)")
        except sqlite3.IntegrityError as exc:
            raise ValueError("Duplicate original five-part effect key") from exc
        db.executescript("""
          CREATE TABLE feature_taxonomy(
            source_feature_type TEXT NOT NULL, feature_id TEXT NOT NULL,
            normalized_feature_type TEXT NOT NULL, identifier_class TEXT NOT NULL,
            chemical_context TEXT NOT NULL,
            PRIMARY KEY(source_feature_type,feature_id)) WITHOUT ROWID;
          INSERT INTO feature_taxonomy
            SELECT DISTINCT source_feature_type,feature_id,normalized_feature_type,
                            identifier_class,chemical_context
            FROM feature_taxonomy_raw ORDER BY source_feature_type,feature_id;
          DROP TABLE feature_taxonomy_raw;
        """)
        for item in annotation:
            present = db.execute("""SELECT 1 FROM source_selector s JOIN effect_membership m
                ON m.selector_id=s.id WHERE s.series_accession=? AND s.analysis_version=?
                AND s.source_feature_type=? AND m.feature_id=? LIMIT 1""", (
                item["series_accession"], item["analysis_version"],
                item["source_feature_type"], item["feature_id"])).fetchone()
            if not present:
                raise ValueError("Annotation example is absent from scanned source identifiers")
            db.execute("INSERT INTO annotation_identity VALUES(?,?,?,?,?)", (
                item["series_accession"], item["analysis_version"], item["source_feature_type"],
                item["feature_id"], canonical(item).decode()))
        actual = {name: dict(counter) for name, counter in counts.items()}
        if binding and sha256(Path(binding["path"])) != binding["sha256"]:
            raise ValueError("Annotation source changed during streaming build")
        for name, expected in manifest.get("expected_counts", {}).items():
            if actual.get(name) != expected:
                raise ValueError("Corrected aggregate counts differ: " + name)
        covered = db.execute("SELECT count(*) FROM effect_membership").fetchone()[0]
        unmatched = db.execute("""SELECT count(*) FROM effect_membership m
            JOIN source_selector s ON s.id=m.selector_id
            LEFT JOIN feature_taxonomy t ON t.source_feature_type=s.source_feature_type AND t.feature_id=m.feature_id
            WHERE t.feature_id IS NULL""").fetchone()[0]
        if covered != sum(i["rows_scanned"] for i in inputs) or unmatched:
            raise AssertionError("Overlay does not cover every original effect key")
        materialized = Counter(dict(db.execute("""SELECT t.normalized_feature_type,count(*)
            FROM effect_membership m JOIN source_selector s ON s.id=m.selector_id
            JOIN feature_taxonomy t ON t.source_feature_type=s.source_feature_type AND t.feature_id=m.feature_id
            GROUP BY t.normalized_feature_type""")))
        if dict(materialized) != actual["normalized_feature_types"]:
            raise AssertionError("Materialized dictionary does not match streamed classification")
        stats = [{"series_accession": key[0], "source_feature_type": key[1], "contrast": key[2],
                  "analysis_version": key[3], "rows": value["rows"],
                  "source_keys_sha256": value["keys"].hexdigest()}
                 for key, value in selector_stats.items()]
        result = {"schema_version": SCHEMA_VERSION, "status": "passed_derived_taxonomy",
            "overlay_release_id": manifest["overlay_release_id"], "source_release_id": manifest["release_id"],
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "counts": actual,
            "full_row_coverage": {"source_rows": covered, "covered_source_keys": covered,
                                  "unmatched_source_keys": unmatched, "duplicate_source_keys": 0},
            "unique_dictionary_entries": db.execute("SELECT count(*) FROM feature_taxonomy").fetchone()[0],
            "source_selectors": stats, "inputs": inputs, "annotation_binding": binding,
            "original_effect_keys_changed": 0, "original_effect_values_changed": 0,
            "original_source_files_modified": 0, "effect_values_copied_to_overlay": False,
            "scientific_approvals_created": 0, "expert_review_status": "pending",
            "public_export_status": "candidate_pending_source_rights_review", "submission_ready": False,
            "chemical_context": "unconfirmed_for_all_methylation_probe_classifications",
            "scope": "Explicit manifest inputs only; effect rows are not independent participants",
            "elapsed_seconds": time.monotonic() - start,
            "source_value_digest_method": "SHA256 of length-prefixed canonical header and original field values; every output source field compared exactly before digesting",
            "tool_files": {"builder_sha256": sha256(Path(__file__)),
                           "adapter_sha256": sha256(Path(__file__).parents[1] / "nutrigenomics/feature_taxonomy.py")}}
        for key, value in (("schema_version", SCHEMA_VERSION), ("overlay_release_id", manifest["overlay_release_id"]),
                           ("source_release_id", manifest["release_id"]), ("counts", actual)):
            db.execute("INSERT INTO metadata VALUES(?,?)", (key, canonical(value).decode()))
        db.commit()
        # Compact this new derivative only; original databases are never opened.
        db.execute("VACUUM")
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise AssertionError("Overlay integrity check failed")
        db.close()
        result["sqlite"] = {"file": "taxonomy.sqlite3", "bytes": (staging / "taxonomy.sqlite3").stat().st_size,
                            "sha256": sha256(staging / "taxonomy.sqlite3"), "quick_check": integrity}
        write_json(staging / "FEATURE_TAXONOMY_COMPLETION.json", result)
        os.replace(staging, output)
        return result
    except BaseException as exc:
        db.close()
        write_json(staging / "FAILED.json", {"status": "failed", "reason": repr(exc),
            "source_release_modified": False, "output_promoted": False})
        raise


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--batch-size", type=int, default=2000)
    args = p.parse_args()
    result = build_overlay(args.manifest, args.output_dir, batch_size=args.batch_size)
    print(json.dumps({"status": result["status"], "source_rows": result["full_row_coverage"]["source_rows"],
                      "counts": result["counts"], "sqlite": result["sqlite"]}))


if __name__ == "__main__":
    main()
