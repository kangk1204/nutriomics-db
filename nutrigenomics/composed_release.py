"""Compose a new native public SQLite from an immutable base and corrected curation.

This is a new release builder, not an in-place patch or a readiness gate. Whole
source bytes are hash-checked while copied. An SQLite authorizer then forbids
all scientific-value writes. Human declarations remain bound to canonical
payload hashes in separate tables. A successful build is always a candidate.
"""
from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .release_contract import (
    build_release_contract,
    install_release_contract,
    metadata_with_contract,
)
from .reporting_review import (
    build_review_package,
    validate_review_decisions,
    validate_review_dependencies,
)
from .submission_package import (
    SHA,
    assert_portable,
    canonical,
    digest,
    fingerprint,
    safe_path,
    sha256,
)

SCHEMA_VERSION = "nutriomics-composed-public-release-v1"
CLASSIFICATION_SCHEMA = "nutriomics-source-classifications-v1"
KEY_FIELDS = ["series_accession", "feature_type", "feature_id", "contrast", "analysis_version"]
MUTABLE_TABLES = {"meta", "science_record", "node", "curation_record_digest",
                  "scientific_review_decision", "scientific_review_dependency", "source_classification", "effect_catalog"}
REBUILT_TABLES = {"science_record", "node", "meta", "curation_record_digest",
                  "scientific_review_decision", "scientific_review_dependency", "source_classification", "effect_catalog",
                  "node_kind", "perf_search_content", "perf_exposure_match"}
DECLARATION_FIELDS = {"record_kind", "record_id", "record_sha256", "decision", "reviewer",
    "reviewer_type", "reviewer_attestation", "reviewed_at", "primary_paper_location", "reason",
    "proposed_correction", "source_dependency_sha256"}


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _pinned_json(path, expected_sha):
    raw = Path(path).read_bytes()
    if expected_sha is None or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("Pinned JSON input SHA mismatch")
    return json.loads(raw)


def _write(path, value):
    assert_portable(value)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(canonical(value) + "\n", encoding="utf-8")


def _phase(staging, release_id, phase):
    _write(staging / "build-progress.json", {"release_id": release_id, "phase": phase,
           "at": datetime.now(UTC).isoformat(), "submission_ready": False})


def _read_only(path):
    if _nonempty_wal(path):
        raise ValueError("Immutable SQLite source has a nonempty WAL")
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _nonempty_wal(path):
    wal = Path(str(path) + "-wal")
    return wal.is_symlink() or (wal.exists() and wal.stat().st_size > 0)


def _copy_verified(source, target, binding):
    """One bounded sequential copy with source identity checks and exact SHA."""
    if source.is_symlink() or not source.is_file() or _nonempty_wal(source):
        raise ValueError("Missing, linked, or unclosed immutable source")
    before = fingerprint(source)
    if source.stat().st_size != binding["bytes"] or not SHA.fullmatch(binding["sha256"]):
        raise ValueError("Source size/hash declaration mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with source.open("rb") as original, target.open("xb") as copied:
        for block in iter(lambda: original.read(4 * 1024 * 1024), b""):
            copied.write(block)
            h.update(block)
        copied.flush()
        os.fsync(copied.fileno())
    if h.hexdigest() != binding["sha256"] or fingerprint(source) != before or _nonempty_wal(source):
        raise ValueError("Source changed or source SHA mismatch during immutable copy")
    if sha256(target) != binding["sha256"]:
        raise ValueError("Copied target SHA mismatch")


def _load_candidate(candidate, expected_map_sha):
    filemap_path = safe_path(candidate, "filemap.json")
    filemap = _pinned_json(filemap_path, expected_map_sha)
    bindings = {}
    for entry in filemap["files"]:
        safe_path(candidate, entry["path"])
        if entry["path"] in bindings or not SHA.fullmatch(entry["sha256"]):
            raise ValueError("Duplicate or invalid candidate file binding")
        bindings[entry["path"]] = entry
    inputs = {}
    for name in ["release-science.sqlite3", "package-status.json", "correction-ledger.json",
                 "producer-provenance.json", "rights-machine-audit.json"]:
        relative = "supplement/" + name
        path = safe_path(candidate, relative)
        binding = bindings.get(relative)
        if not binding or not path.is_file() or path.stat().st_size != binding["bytes"] or sha256(path) != binding["sha256"]:
            raise ValueError("Candidate curation file is absent or changed: " + relative)
        inputs[name] = path
    status = _pinned_json(inputs["package-status.json"], bindings["supplement/package-status.json"]["sha256"])
    if status.get("machine_package_integrity") is not True or status.get("publication_ready") is not False:
        raise ValueError("Expected an integrity-checked, unapproved corrected candidate")
    records, selectors = {}, []
    science_identity = fingerprint(inputs["release-science.sqlite3"])
    with closing(_read_only(inputs["release-science.sqlite3"])) as db:
        for row in db.execute("SELECT kind,id,metadata_json,record_sha256 FROM science_record ORDER BY kind,id"):
            key = (row["kind"], str(row["id"]))
            value = json.loads(row["metadata_json"])
            if key in records or digest(value) != row["record_sha256"]:
                raise ValueError("Authoritative science key/payload SHA mismatch")
            assert_portable(value)
            records[key] = value
        for row in db.execute("SELECT * FROM effect_catalog ORDER BY series_accession,feature_type,contrast,analysis_version"):
            row = dict(row)
            row["files"] = json.loads(row.pop("files_json"))
            for reference in row["files"]:
                expected = bindings.get(reference["path"])
                if (not expected or reference["path"].split("/")[:2] != ["base", "effects"]
                        or any(reference[k] != expected[k] for k in ("bytes", "sha256"))):
                    raise ValueError("Effect catalog has an unbound or non-native file reference")
            selectors.append(row)
    if fingerprint(inputs["release-science.sqlite3"]) != science_identity:
        raise ValueError("Authoritative science SQLite changed while reading")
    if len(records) != status.get("science_records") or sum(r["total"] for r in selectors) != status.get("effect_rows_referenced"):
        raise ValueError("Candidate status differs from actual authoritative SQLite")
    rights = _pinned_json(inputs["rights-machine-audit.json"], bindings["supplement/rights-machine-audit.json"]["sha256"])
    if rights.get("machine_policy_checks_passed") is not True or rights.get("blanket_third_party_license") is not False:
        raise ValueError("Candidate lacks source-specific recorded rights audit")
    return filemap, bindings, inputs, status, records, selectors


def _record_wrappers(records):
    return [{"kind": kind, "id": identifier, "record": value} for (kind, identifier), value in sorted(records.items())]


def _review_dependencies(records, path, expected_sha):
    if path is None:
        if expected_sha is not None:
            raise ValueError("Review dependency SHA supplied without file")
        return None
    value = _pinned_json(path, expected_sha)
    validate_review_dependencies(_record_wrappers(records), value)
    assert_portable(value)
    return value


def _decisions(records, path, expected_sha, dependencies=None):
    if path is None:
        if expected_sha is not None:
            raise ValueError("Decision SHA supplied without decision file")
        rows = []
    else:
        rows = _pinned_json(path, expected_sha)
        if not isinstance(rows, list):
            raise ValueError("Human decisions must be record-level declarations")
    summary = validate_review_decisions(_record_wrappers(records), rows, dependencies=dependencies)
    if dependencies is None and any(row.get("decision") != "pending" for row in rows):
        raise ValueError("Every human decision requires current source review dependencies")
    cleaned = [{k: row.get(k) for k in sorted(DECLARATION_FIELDS)} for row in rows]
    assert_portable(cleaned)
    return cleaned, summary


def _classifications(records, path, expected_sha):
    if path is None:
        if expected_sha is not None:
            raise ValueError("Classification SHA supplied without file")
        return []
    value = _pinned_json(path, expected_sha)
    if value.get("schema_version") != CLASSIFICATION_SCHEMA or value.get("automatic_expert_approval") is not False:
        raise ValueError("Source classification cannot grant automatic expert approval")
    rows, seen = [], set()
    for original in value.get("records", []):
        row = copy.deepcopy(original)
        key = (row.get("record_kind"), str(row.get("record_id")))
        if key in seen or key not in records or row.get("record_sha256") != digest(records[key]):
            raise ValueError("Unknown, duplicate, or stale source classification")
        seen.add(key)
        if (row.get("source_checked") is not True or row.get("primary_dietary_evidence_eligible") is not False
                or row.get("classification") not in {"exploratory_confounded_extraction_time", "supplement_mapping_unresolved"}
                or not isinstance(row.get("reason"), str) or not row["reason"].strip()):
            raise ValueError("Unsupported source-only dietary classification")
        if row["classification"] == "supplement_mapping_unresolved" and row.get("supplement_attribution_established") is not False:
            raise ValueError("Unresolved supplement mapping cannot establish supplement attribution")
        if any(row.get(k) not in (None, False, "pending") for k in ("scientific_approval", "human_approved", "expert_review_status")):
            raise ValueError("Source checks cannot carry human approval")
        references = row.get("source_references")
        if not isinstance(references, list) or not references or any(
                not isinstance(r, dict) or not r.get("url", "").startswith("https://")
                or not r.get("locator") or not SHA.fullmatch(r.get("sha256", "")) for r in references):
            raise ValueError("Source classification requires URL, locator and source-file SHA")
        row.update(expert_review_status="pending", scientific_approval=False, classification_actor="source_check_not_human_review")
        assert_portable(row)
        rows.append(row)
    return rows


def _validate_corrections(original, corrected, ledger):
    if set(original) != set(corrected) or ledger.get("automatic_expert_approval") is not False:
        raise ValueError("Corrected curation must preserve the entire base science key set")
    patches = {}
    for row in ledger.get("patches", []):
        key = (row["record_kind"], str(row["record_id"]))
        if key in patches or key not in original:
            raise ValueError("Unknown/duplicate correction ledger key")
        patches[key] = row
    changed = {k for k in original if digest(original[k]) != digest(corrected[k])}
    if changed != set(patches):
        raise ValueError("Unexplained correction or unused correction ledger entry")
    for key, patch in patches.items():
        if digest(original[key]) != patch["old_record_sha256"] or digest(corrected[key]) != patch["new_record_sha256"]:
            raise ValueError("Correction old/new hash differs from actual base/corrected records")
        if patch.get("expert_review_status") != "pending":
            raise ValueError("A metadata patch cannot approve a scientific record")
        value = copy.deepcopy(original[key])
        for change in patch["changes"]:
            if change["operation"] != "replace":
                raise ValueError("Unsupported ledger operation")
            target = value
            for part in change["path"][:-1]:
                target = target[part]
            last = change["path"][-1]
            if target[last] != change["old_value"]:
                raise ValueError("Correction ledger old value mismatch")
            target[last] = change["new_value"]
        if digest(value) != digest(corrected[key]):
            raise ValueError("Correction ledger does not explain the authoritative payload")
    return changed


def _list_field(value, name):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Exposure list field is not explicit source strings: " + name)
    return value


def _plan_exposure_nodes(db, records):
    """Retain canonical exposure identity even when it has no graph edges.

    This projection is registration metadata, not new biological evidence.
    Existing nodes are never renamed, merged, replaced, or reclassified.
    """
    planned, entries = [], []
    for (kind, identifier), record in sorted(records.items()):
        if kind != "exposure":
            continue
        if record.get("id") != identifier or not isinstance(record.get("label"), str) or not record["label"].strip():
            raise ValueError("Canonical exposure SQL/payload identity or explicit label mismatch")
        node_id, source_id = "Exposure:" + identifier, record.get("source_id")
        source_row = db.execute("SELECT metadata_json FROM source WHERE id=?", (source_id,)).fetchone()
        if source_row is None:
            raise ValueError("Canonical exposure source is absent: " + identifier)
        source = json.loads(source_row[0])
        if source.get("export_allowed") is not True:
            raise ValueError("Canonical exposure source is not explicitly export-permitted: " + identifier)
        node = db.execute("SELECT * FROM node WHERE id=?", (node_id,)).fetchone()
        if node is not None:
            metadata = json.loads(node["metadata_json"])
            if (node["kind"] != "diet" or metadata.get("entity_type") != "exposure"
                    or metadata.get("original_exposure", {}).get("id") != identifier):
                raise ValueError("Existing exposure node has incompatible source identity: " + node_id)
            action = "reused"
        else:
            aliases = _list_field(record.get("food_terms", []), "food_terms")
            contrasts, assertions = [], []
            for (related_kind, _), value in sorted(records.items()):
                if related_kind == "contrast" and identifier in _list_field(value.get("exposure_ids", []), "exposure_ids"):
                    contrasts.append(value)
                elif related_kind == "assertion" and value.get("exposure_id") == identifier:
                    assertions.append(value)
            metadata = {"entity_type": "exposure", "original_exposure": record,
                        "contrasts": contrasts, "assertions": assertions,
                        "effect_data_storage": "Original native effect keys; no effect rows copied",
                        "component_causality_identified": False, "source_ids": [source_id],
                        "expert_review_status": "pending", "scientific_approval": False,
                        "projection_basis": "Canonical registered exposure; no biological relationship added"}
            node = {"id": node_id, "kind": "diet", "label": record["label"], "label_en": "",
                    "aliases_json": canonical(aliases), "metadata_json": canonical(metadata),
                    "domains_json": "[]", "degree": 0}
            planned.append(node)
            action = "added_isolated"
        entries.append({"exposure_id": identifier, "node_id": node_id, "action": action,
                        "record_sha256": digest(record), "node_sha256": digest(dict(node)),
                        "source_id": source_id, "source_metadata_sha256": digest(source),
                        "expert_review_status": "pending", "scientific_approval_inferred": False})
    return planned, {"schema_version": "nutriomics-exposure-node-projection-v1", "records": entries,
                     "registered": len(entries), "added": len(planned),
                     "reused": len(entries) - len(planned), "edges_added": 0,
                     "health_or_component_links_inferred": False}


class _MetadataWrites(list):
    """Authorizer audit plus a narrowly bounded auxiliary-index write phase."""
    index_maintenance = False

    def __call__(self, action, table, column, database, trigger):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            node_write = table == "node" and (action == sqlite3.SQLITE_INSERT
                         or action == sqlite3.SQLITE_UPDATE and column == "metadata_json")
            index_write = (self.index_maintenance and table in {
                "node_kind", "perf_exposure_match", "perf_search_content", "perf_search"
            } and action == sqlite3.SQLITE_INSERT) or table in {
                # FTS5 also flushes its private index pages during COMMIT,
                # after the explicit projection phase has finished.
                "perf_search_data", "perf_search_idx", "perf_search_docsize", "perf_search_config"
            }
            if not node_write and not index_write and (table not in MUTABLE_TABLES or table == "node"):
                self.append([action, table, column])
                return sqlite3.SQLITE_DENY
        elif action in {sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_VIEW,
                       sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TRIGGER}:
            self.append([action, table, column])
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def _install_science(db, records, selectors, decisions, classifications, dependencies=None, exposure_nodes=()):
    db.executescript("""
        CREATE TABLE curation_record_digest(kind TEXT NOT NULL,id TEXT NOT NULL,
            record_sha256 TEXT NOT NULL,PRIMARY KEY(kind,id));
        CREATE TABLE scientific_review_decision(kind TEXT NOT NULL,id TEXT NOT NULL,
            record_sha256 TEXT NOT NULL,decision_json TEXT NOT NULL,PRIMARY KEY(kind,id));
        CREATE TABLE scientific_review_dependency(kind TEXT NOT NULL,id TEXT NOT NULL,
            record_sha256 TEXT NOT NULL,dependency_json TEXT NOT NULL,PRIMARY KEY(kind,id));
        CREATE TABLE source_classification(kind TEXT NOT NULL,id TEXT NOT NULL,
            record_sha256 TEXT NOT NULL,metadata_json TEXT NOT NULL,PRIMARY KEY(kind,id));
        CREATE TABLE effect_catalog(series_accession TEXT NOT NULL,feature_type TEXT NOT NULL,
            contrast TEXT NOT NULL,analysis_version TEXT NOT NULL,total INTEGER NOT NULL,
            files_json TEXT NOT NULL,rights_policy_id TEXT NOT NULL,
            PRIMARY KEY(series_accession,feature_type,contrast,analysis_version));
    """)
    # The temporary trigger permits only the exact planned, previously absent
    # eight-column rows. INSERT OR REPLACE cannot overwrite an inherited node.
    db.execute("CREATE TEMP TABLE composition_expected_exposure_node AS SELECT * FROM node WHERE 0")
    columns = ["id", "kind", "label", "label_en", "aliases_json", "metadata_json", "domains_json", "degree"]
    db.executemany("INSERT INTO composition_expected_exposure_node VALUES(?,?,?,?,?,?,?,?)",
                   [tuple(row[c] for c in columns) for row in exposure_nodes])
    equality = " AND ".join("e." + c + " IS NEW." + c for c in columns)
    db.execute("CREATE TEMP TRIGGER composition_exposure_insert_guard BEFORE INSERT ON main.node "
               "WHEN EXISTS(SELECT 1 FROM main.node WHERE id=NEW.id) OR NOT EXISTS(SELECT 1 FROM "
               "composition_expected_exposure_node e WHERE " + equality + ") BEGIN "
               "SELECT RAISE(ABORT,'Unplanned exposure insertion or existing node replacement'); END")
    denied = _MetadataWrites()
    db.set_authorizer(denied)
    db.execute("DELETE FROM science_record")
    for (kind, identifier), record in sorted(records.items()):
        db.execute("INSERT INTO science_record(id,kind,metadata_json) VALUES(?,?,?)", (identifier, kind, canonical(record)))
        db.execute("INSERT INTO curation_record_digest VALUES(?,?,?)", (kind, identifier, digest(record)))
    for row in decisions:
        db.execute("INSERT INTO scientific_review_decision VALUES(?,?,?,?)", (row["record_kind"], str(row["record_id"]), row["record_sha256"], canonical(row)))
    for row in (dependencies or {}).get("records", []):
        db.execute("INSERT INTO scientific_review_dependency VALUES(?,?,?,?)", (row["record_kind"], str(row["record_id"]), row["record_sha256"], canonical(row)))
    for row in classifications:
        db.execute("INSERT INTO source_classification VALUES(?,?,?,?)", (row["record_kind"], str(row["record_id"]), row["record_sha256"], canonical(row)))
    for row in selectors:
        refs = [{**r, "path": r["path"].removeprefix("base/")} for r in row["files"]]
        db.execute("INSERT INTO effect_catalog VALUES(?,?,?,?,?,?,?)", (row["series_accession"], row["feature_type"], row["contrast"], row["analysis_version"], row["total"], canonical(refs), row["rights_policy_id"]))
    return denied


def _sync_cached_records(db, original, corrected):
    """Replace only exact embedded old payloads, never a guessed identifier join."""
    replacements = {digest(original[k]): corrected[k] for k in original if original[k] != corrected[k]}
    def replace(value):
        if isinstance(value, dict):
            replacement = replacements.get(digest(value))
            if replacement is not None:
                return copy.deepcopy(replacement)
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        return value
    changed = []
    for identifier, payload in db.execute("SELECT id,metadata_json FROM node").fetchall():
        value = json.loads(payload)
        updated = replace(value)
        if updated != value:
            db.execute("UPDATE node SET metadata_json=? WHERE id=?", (canonical(updated), identifier))
            changed.append(identifier)
    return changed


def _insert_exposure_nodes(db, records, planned, guard):
    """Add registration rows and their existing read-index projections only."""
    from .explorer_scientific import decoded, exposure_match_identity

    columns = ["id", "kind", "label", "label_en", "aliases_json", "metadata_json", "domains_json", "degree"]
    db.executemany("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", [tuple(n[c] for c in columns) for n in planned])
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if ("perf_search" in tables) != ("perf_search_content" in tables):
        raise ValueError("Incomplete inherited exposure search index")
    exposures = [r for (kind, _), r in sorted(records.items()) if kind == "exposure"]
    curated = {r["core_exposure_id"]: r for (kind, _), value in records.items() if kind == "curation"
               for r in value.get("exposures", [])}
    counts = {"node_kind": 0, "search_content": 0, "search_fts": 0, "exposure_matches": 0,
              "adjacency_rows_added": 0}
    guard.index_maintenance = True
    try:
        for node in planned:
            if "node_kind" in tables:
                db.execute("INSERT INTO node_kind(id,kind) VALUES(?,?)", (node["id"], node["kind"]))
                counts["node_kind"] += 1
            if "perf_search" in tables:
                values = (node["id"], node["label"], node["label_en"] or "", node["aliases_json"], node["id"])
                cursor = db.execute("INSERT INTO perf_search_content(node_id,label,label_en,aliases,identifier) VALUES(?,?,?,?,?)", values)
                db.execute("INSERT INTO perf_search(rowid,label,label_en,aliases,identifier) VALUES(?,?,?,?,?)",
                           (cursor.lastrowid, *values[1:]))
                counts["search_content"] += 1
                counts["search_fts"] += 1
            if "perf_exposure_match" in tables:
                seed = {**node, "metadata": json.loads(node["metadata_json"]), "aliases": json.loads(node["aliases_json"]), "label_en": node["label_en"] or ""}
                for original in exposures:
                    exposure = {**original, **curated.get(original["id"], {})}
                    identity = exposure_match_identity(seed, exposure, decoded(original.get("component_keys"), []))
                    if identity["status"] != "unknown":
                        db.execute("INSERT INTO perf_exposure_match VALUES(?,?,?)", (node["id"], original["id"], canonical(identity)))
                        counts["exposure_matches"] += 1
    finally:
        guard.index_maintenance = False
    return counts


def _validate_exposure_projection(db, records, ledger):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    entries = {r["exposure_id"]: r for r in ledger["records"]}
    exposures = {i: r for (kind, i), r in records.items() if kind == "exposure"}
    if len(entries) != len(ledger["records"]) or set(entries) != set(exposures):
        raise ValueError("Exposure node ledger does not cover canonical registrations")
    if (ledger.get("registered") != len(exposures)
            or ledger.get("added") != sum(r["action"] == "added_isolated" for r in entries.values())
            or ledger.get("reused") != sum(r["action"] == "reused" for r in entries.values())
            or ledger.get("edges_added") != 0):
        raise ValueError("Exposure node ledger count/relationship mismatch")
    if ("perf_search" in tables) != ("perf_search_content" in tables):
        raise ValueError("Incomplete inherited exposure search index")
    for identifier, exposure in exposures.items():
        entry = entries[identifier]
        cursor = db.execute("SELECT * FROM node WHERE id=?", ("Exposure:" + identifier,))
        value = cursor.fetchone()
        row = dict(zip([c[0] for c in cursor.description], value)) if value else None
        metadata = json.loads(row["metadata_json"]) if row else {}
        if (not row or row["kind"] != "diet" or metadata.get("entity_type") != "exposure"
                or metadata.get("original_exposure", {}).get("id") != identifier
                or entry["record_sha256"] != digest(exposure) or entry["node_id"] != row["id"]
                or entry["node_sha256"] != digest(dict(row))):
            raise ValueError("Canonical exposure node/source/ledger mismatch")
        source = db.execute("SELECT metadata_json FROM source WHERE id=?", (exposure.get("source_id"),)).fetchone()
        source = json.loads(source[0]) if source else None
        if (source is None or source.get("export_allowed") is not True
                or entry["source_id"] != exposure.get("source_id")
                or entry["source_metadata_sha256"] != digest(source)):
            raise ValueError("Exposure source identity or redistribution permission changed")
        if entry["action"] == "added_isolated" and (row["degree"] != 0 or db.execute(
                "SELECT 1 FROM edge WHERE subject=? OR object=? LIMIT 1", (row["id"], row["id"])).fetchone()):
            raise ValueError("New exposure registration cannot invent a biological edge")
        if "node_kind" in tables:
            kind = db.execute("SELECT kind FROM node_kind WHERE id=?", (row["id"],)).fetchone()
            if not kind or kind[0] != "diet":
                raise ValueError("Exposure kind projection is stale")
        if "perf_search" in tables:
            content = db.execute("SELECT label,label_en,aliases,identifier FROM perf_search_content WHERE node_id=?", (row["id"],)).fetchone()
            if not content or tuple(content) != (row["label"], row["label_en"] or "", row["aliases_json"], row["id"]):
                raise ValueError("Exposure search content projection is stale")
            phrase = '"' + row["id"].replace('"', '""') + '"'
            found = db.execute("SELECT 1 FROM perf_search f JOIN perf_search_content c ON c.rowid=f.rowid "
                               "WHERE perf_search MATCH ? AND c.node_id=?", (phrase, row["id"])).fetchone()
            if not found:
                raise ValueError("Exposure full-text projection is stale")
        if "perf_exposure_match" in tables:
            match = db.execute("SELECT identity_json FROM perf_exposure_match WHERE node_id=? AND exposure_id=?", (row["id"], identifier)).fetchone()
            if not match or json.loads(match[0]).get("status") != "same_record":
                raise ValueError("Exposure matching projection is stale")


def _csv(path, cursor):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=3) as stream:
        writer = csv.writer(stream)
        writer.writerow([r[0] for r in cursor.description])
        writer.writerows(cursor)


def validate_service_composition(db, metadata, manifest):
    """Startup integrity check for the small authoritative dependencies, no matrix scan."""
    composition = manifest.get("composition")
    if composition is None:
        return
    if composition.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported composed release schema")
    release_id = manifest.get("release_id")
    for key in ("release_id", "dataset_release_id", "service_build_id"):
        if metadata.get(key) != release_id:
            raise ValueError("Composed release file/API identity mismatch")
    stored = {r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value_json FROM meta")}
    if any(stored.get(k) != release_id for k in ("release_id", "dataset_release_id", "service_build_id")):
        raise ValueError("Composed SQLite identity mismatch")
    if stored.get("composition") != composition or metadata.get("composition") != composition:
        raise ValueError("Composed dependency hashes differ between SQLite and files")
    rows = list(db.execute("SELECT s.kind,s.id,s.metadata_json,d.record_sha256 FROM science_record s "
                           "JOIN curation_record_digest d ON d.kind=s.kind AND d.id=s.id ORDER BY s.kind,s.id"))
    if len(rows) != metadata["counts"]["science_record"] or any(digest(json.loads(r[2])) != r[3] for r in rows):
        raise ValueError("Composed canonical science payload/hash/count mismatch")
    actual_count = db.execute("SELECT count(*) FROM science_record").fetchone()[0]
    if actual_count != len(rows) or digest([[r[0], str(r[1]), r[3]] for r in rows]) != composition["science_record_set_sha256"]:
        raise ValueError("Composed science record-set binding mismatch")
    if composition.get("exposure_node_ledger_sha256") is not None:
        ledger = stored.get("exposure_node_ledger")
        if not ledger or digest(ledger) != composition["exposure_node_ledger_sha256"]:
            raise ValueError("Composed exposure node ledger binding mismatch")
        _validate_exposure_projection(db, {(r[0], str(r[1])): json.loads(r[2]) for r in rows}, ledger)
    expected = {(r[0], r[1], r[2], r[3]): r[4] for r in db.execute(
        "SELECT series_accession,feature_type,contrast,analysis_version,total FROM effect_selector")}
    actual = {(r[0], r[1], r[2], r[3]): r[4] for r in db.execute(
        "SELECT series_accession,feature_type,contrast,analysis_version,total FROM effect_catalog")}
    if expected != actual or sum(actual.values()) != metadata["counts"]["effect"]:
        raise ValueError("Composed native quantitative catalog mismatch")
    declarations = [json.loads(r[0]) for r in db.execute("SELECT decision_json FROM scientific_review_decision ORDER BY kind,id")]
    classes = [json.loads(r[0]) for r in db.execute("SELECT metadata_json FROM source_classification ORDER BY kind,id")]
    if digest(declarations) != composition["human_decision_set_sha256"] or digest(classes) != composition["source_classification_set_sha256"]:
        raise ValueError("Composed review/classification dependency mismatch")
    dependency_rows = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='scientific_review_dependency'").fetchone():
        dependency_rows = [json.loads(r[0]) for r in db.execute("SELECT dependency_json FROM scientific_review_dependency ORDER BY kind,id")]
    if composition.get("review_dependency_set_sha256") is not None:
        if digest(dependency_rows) != composition["review_dependency_set_sha256"]:
            raise ValueError("Composed captured-source dependency mismatch")
        dependencies = {"schema_version": "nutriomics-review-dependencies-v1", "records": dependency_rows}
    else:
        dependencies = None
    if dependencies is None and any(row.get("decision") != "pending" for row in declarations):
        raise ValueError("Composed human decisions lack captured-source dependencies")
    human = validate_review_decisions(db, declarations, dependencies=dependencies)
    previous_human = dict(metadata["review_summary"]["human_decisions"])
    # The immutable zero-human v1 builder predates the optional source map.
    previous_human.setdefault("source_dependencies_bound", False)
    if human != previous_human:
        raise ValueError("Composed human review summary differs from actual decisions")
    if metadata.get("submission_ready") is not False or metadata.get("status") == "submission_ready":
        raise ValueError("Composition alone cannot establish submission readiness")


def build(base, candidate, output, release_id, *, expected_base_manifest_sha256,
          expected_candidate_filemap_sha256, decisions=None, expected_decisions_sha256=None,
          classifications=None, expected_classifications_sha256=None,
          review_dependencies=None, expected_review_dependencies_sha256=None):
    """Build a new, self-contained candidate; never modify either immutable input.

    ``candidate`` is the materialized80-reference package root (base/,supplement/).
    Full-file copies/readback and actual effect-row counts are build-time work.
    This function must run as one separately budgeted, sequential operator job.
    """
    base, candidate, output = (Path(p).resolve() for p in (base, candidate, output))
    staging = output.with_name("." + output.name + ".composing")
    for source in (base, candidate):
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("Composed output must be separate from immutable inputs")
    if output.exists() or staging.exists():
        raise FileExistsError("New immutable composed output already exists")
    manifest = _pinned_json(safe_path(base, "manifest.json"), expected_base_manifest_sha256)
    if not release_id or release_id == manifest.get("release_id"):
        raise ValueError("A new distinct composed release ID is required")
    _filemap, bindings, inputs, status, records, selectors = _load_candidate(candidate, expected_candidate_filemap_sha256)
    if status["immutable_base_release_id"] != manifest["release_id"]:
        raise ValueError("Corrected candidate references another immutable base")
    base_binding = bindings.get("base/nutriomics.sqlite3", {})
    if base_binding.get("sha256") != manifest["database_sha256"]:
        raise ValueError("Candidate/native base SQLite hash mismatch")
    declaration_file, classification_file = decisions, classifications
    dependency_file = review_dependencies
    dependencies = _review_dependencies(records, dependency_file, expected_review_dependencies_sha256)
    declarations, human = _decisions(records, declaration_file, expected_decisions_sha256, dependencies)
    classifications = _classifications(records, classification_file, expected_classifications_sha256)
    declarations.sort(key=lambda r: (r["record_kind"], str(r["record_id"])))
    classifications.sort(key=lambda r: (r["record_kind"], str(r["record_id"])))
    if dependencies is not None:
        dependencies = {**dependencies, "records": sorted(dependencies["records"], key=lambda r: (r["record_kind"], str(r["record_id"])))}
    source_paths = [base / "manifest.json", base / "nutriomics.sqlite3", candidate / "filemap.json", *inputs.values(),
                    *[Path(p) for p in (declaration_file, classification_file, dependency_file) if p is not None]]
    source_before = {str(p): fingerprint(p) for p in source_paths}
    parent_bindings = {r["path"]: r for r in manifest.get("files", [])}
    rights_binding = parent_bindings.get("rights-policy.json")
    if not rights_binding:
        raise ValueError("Native public source rights policy missing")
    policy = _pinned_json(safe_path(base, "rights-policy.json"), rights_binding["sha256"])
    policies = {(r["series_accession"], r["analysis_version"]): r for r in policy["analyses"]}
    if len(policies) != len(policy["analyses"]):
        raise ValueError("Duplicate native quantitative rights policies")
    for selector in selectors:
        right = policies.get((selector["series_accession"], selector["analysis_version"]), {})
        if right.get("export_allowed") is not True or right.get("id") != selector["rights_policy_id"]:
            raise ValueError("Quantitative selector is denied or has mismatched native source rights")
    ancestor = output.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    estimated = base_binding["bytes"] + sum(r["bytes"] for r in manifest.get("files", []))
    if shutil.disk_usage(ancestor).free < estimated + 2 * 1024**3:
        raise ValueError("Insufficient permanent disk space for composed release")
    staging.mkdir(parents=True)
    db = None
    try:
        _phase(staging, release_id, "copy_native_database_and_verify_full_sha")
        _copy_verified(safe_path(base, "nutriomics.sqlite3"), staging / "nutriomics.sqlite3", base_binding)
        _phase(staging, release_id, "compose_hash_bound_science_metadata")
        db = sqlite3.connect(staging / "nutriomics.sqlite3")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA cache_size=-65536")
        original = {(r[0], str(r[1])): json.loads(r[2]) for r in db.execute("SELECT kind,id,metadata_json FROM science_record")}
        changed = _validate_corrections(original, records, _json(inputs["correction-ledger.json"]))
        expected_selectors = {(r["series_accession"], r["feature_type"], r["contrast"], r["analysis_version"]): r["total"] for r in selectors}
        native_selectors = {(r[0], r[1], r[2], r[3]): r[4] for r in db.execute("SELECT series_accession,feature_type,contrast,analysis_version,total FROM effect_selector")}
        if expected_selectors != native_selectors or len(expected_selectors) != len(selectors):
            raise ValueError("Corrected effect catalog differs from native selector keys/counts")
        planned_exposures, exposure_ledger = _plan_exposure_nodes(db, records)
        denied = _install_science(db, records, selectors, declarations, classifications, dependencies, planned_exposures)
        updated_nodes = _sync_cached_records(db, original, records)
        for entry in exposure_ledger["records"]:
            if entry["node_id"] in updated_nodes:
                entry["inherited_node_sha256"] = entry["node_sha256"]
                entry["node_sha256"] = digest(dict(db.execute("SELECT * FROM node WHERE id=?", (entry["node_id"],)).fetchone()))
        exposure_ledger["auxiliary_indexes"] = _insert_exposure_nodes(db, records, planned_exposures, denied)
        _validate_exposure_projection(db, records, exposure_ledger)
        db.execute("INSERT OR REPLACE INTO meta VALUES('exposure_node_ledger',?)", (canonical(exposure_ledger),))
        for key in ("graph_performance", "performance"):
            row = db.execute("SELECT value_json FROM meta WHERE key=?", (key,)).fetchone()
            if row:
                value = json.loads(row[0])
                if key == "performance" and value.get("core_fingerprint"):
                    raise ValueError("A public composition may not inherit a private/path-bound core index")
                value.update(indexed_for_release=release_id, rebound_from_release_id=value.get("indexed_for_release"),
                             rebind_basis="Inherited row IDs, labels, relationships and measurements preserved; canonical isolated exposure registrations and their read indexes are recorded in exposure_node_ledger")
                db.execute("UPDATE meta SET value_json=? WHERE key=?", (canonical(value), key))
        previous = _json(base / "dataset-metadata.json")
        contract = build_release_contract(db, release_id=release_id, scope="public", previous_metadata=previous, manifest=manifest)
        human_summary = {"expert_review_status": "pending" if human["decisions"].get("pending", 0) else "decisions_recorded",
            "core_records": human["expected_records"], "expert_approved_records": human["expert_approved"],
            "approval_count_status": "record_hash_bound_offline_declarations",
            "human_decisions": human, "independent_review": False,
            "source_classified_records": len(classifications), "source_checks_are_human_approval": False,
            "reason": "Human declarations remain separate from canonical payloads; unresolved/excluded/correction-required records are not accepted."}
        composition = {"schema_version": SCHEMA_VERSION,
            "immutable_base_release_id": manifest["release_id"], "base_database_sha256": manifest["database_sha256"],
            "base_manifest_sha256": expected_base_manifest_sha256,
            "candidate_release_id": status["release_id"], "candidate_filemap_sha256": expected_candidate_filemap_sha256,
            "authoritative_science_sha256": sha256(inputs["release-science.sqlite3"]),
            "science_record_set_sha256": digest([[k, i, digest(r)] for (k, i), r in sorted(records.items())]),
            "human_decisions_sha256": expected_decisions_sha256,
            "source_classifications_sha256": expected_classifications_sha256,
            "human_decision_set_sha256": digest(declarations),
            "source_classification_set_sha256": digest(classifications),
            "review_dependencies_sha256": expected_review_dependencies_sha256,
            "review_dependency_set_sha256": digest(dependencies["records"]) if dependencies is not None else None,
            "exposure_node_ledger_sha256": digest(exposure_ledger),
            "canonical_exposure_nodes": exposure_ledger["registered"],
            "isolated_exposure_nodes_added": exposure_ledger["added"],
            "biological_edges_added": 0,
            "effect_key_fields": KEY_FIELDS, "effect_rows_modified": 0, "matrix_reanalysis_performed": False,
            "human_approval_inferred": False}
        contract.update(review_summary=human_summary, expert_review=human_summary["expert_review_status"],
            status="candidate_pending_expert_review", submission_ready=False,
            immutable_base_release_id=manifest["release_id"], candidate_composition_release_id=status["release_id"],
            composition=composition)
        install_release_contract(db, contract)
        created = datetime.now(UTC).isoformat()
        db.execute("INSERT OR REPLACE INTO meta VALUES('created_at',?)", (canonical(created),))
        if denied:
            raise ValueError("Protected scientific data mutation attempted")
        db.commit()
        _phase(staging, release_id, "copy_verified_public_attachments")
        # These attachments contain unchanged source-specific rights and values.
        base_files = {r["path"]: r for r in manifest.get("files", [])}
        copied = []
        for name, binding in sorted(base_files.items()):
            allowed = (name.startswith("effects/") or name in {"rights-policy.json", "source-catalog.json", "clinical-endpoints.json", "exclusions.json", "reference-payload-audit.json"}
                       or (name.startswith("tables/") and name.endswith(".csv.gz") and Path(name).name.removesuffix(".csv.gz") not in REBUILT_TABLES))
            if allowed:
                _copy_verified(safe_path(base, name), safe_path(staging, name), binding)
                copied.append(name)
        if "rights-policy.json" not in copied:
            raise ValueError("Native public source rights policy missing")
        for row in selectors:
            for reference in row["files"]:
                target = safe_path(staging, reference["path"].removeprefix("base/"))
                if not target.is_file() or target.stat().st_size != reference["bytes"]:
                    raise ValueError("Native effect attachment missing from composed release")
        for name, path in inputs.items():
            if name in {"correction-ledger.json", "producer-provenance.json", "rights-machine-audit.json"}:
                _copy_verified(path, staging / "composition" / name, bindings["supplement/" + name])
        _write(staging / "composition/human-decisions.json", declarations)
        _write(staging / "composition/exposure-node-ledger.json", exposure_ledger)
        _write(staging / "composition/source-classifications.json", {"schema_version": CLASSIFICATION_SCHEMA,
            "automatic_expert_approval": False, "records": classifications})
        if dependencies is not None:
            _write(staging / "composition/review-dependencies.json", dependencies)
        build_review_package(db, staging / "review", decisions=declarations, dependencies=dependencies)
        _phase(staging, release_id, "export_current_metadata")
        for name in sorted(REBUILT_TABLES):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
                _csv(staging / "tables" / (name + ".csv.gz"), db.execute('SELECT * FROM "' + name + '"'))
        (staging / "schema.sql").write_text("\n".join(r[0] + ";" for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")), encoding="utf-8")
        dictionary = {"release_id": release_id, "effect_identity": KEY_FIELDS,
            "canonical_science": "science_record; hashes in curation_record_digest; human decisions/classifications are separate",
            "review_semantics": "source_checked is not human approval; exclusion/correction dispositions remain explicit, not deleted numerical values",
            "tables": {}}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            dictionary["tables"][name] = [dict(zip(["cid", "name", "sqlite_type", "not_null", "default", "primary_key_order"], r)) for r in db.execute("PRAGMA table_info(" + quoted + ")")]
        _write(staging / "data-dictionary.json", dictionary)
        _write(staging / "release-contract.json", contract)
        dataset = metadata_with_contract(db, previous)
        _write(staging / "dataset-metadata.json", dataset)
        _write(staging / "coverage.json", {"release_id": release_id, "counts": contract["counts"], "review_summary": human_summary})
        preservation = {"release_id": release_id, "base_database_sha256": manifest["database_sha256"],
            "method": "Full SHA-identical byte copy before metadata changes, then SQLite authorizer denies all non-metadata writes",
            "denied_writes": denied, "protected_value_writes": 0, "corrected_records": len(changed),
            "changed_record_keys": [list(k) for k in sorted(changed)], "cached_nodes_updated": updated_nodes,
            "exposure_nodes_added": exposure_ledger["added"], "biological_edges_added": 0,
            "exposure_node_ledger_sha256": digest(exposure_ledger),
            "effect_selectors_verified": len(selectors), "effect_rows": contract["counts"]["effect"],
            "expert_semantic_accuracy": "not_inferred"}
        _write(staging / "quantitative-preservation.json", preservation)
        _phase(staging, release_id, "sqlite_quick_check")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Composed SQLite integrity check failed")
        _phase(staging, release_id, "sqlite_foreign_key_check")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Composed SQLite integrity/foreign-key check failed")
        _phase(staging, release_id, "validate_composed_service_contract")
        actual_science = {(r[0], str(r[1])): json.loads(r[2]) for r in db.execute("SELECT kind,id,metadata_json FROM science_record")}
        if actual_science != records:
            raise ValueError("Composed authoritative records changed")
        current = {**contract, "created_at": created, "database_sha256": "pending",
            "composition": composition, "builder_sha256": sha256(Path(__file__)),
            "journal_submission_ready": False, "published": False, "doi": None,
            "quantitative_preservation": preservation}
        validate_service_composition(db, dataset, current)
        db.close()
        db = None
        _phase(staging, release_id, "hash_closed_database")
        current["database_sha256"] = sha256(staging / "nutriomics.sqlite3")
        if {str(p): fingerprint(p) for p in source_paths} != source_before or any(_nonempty_wal(p) for p in source_paths):
            raise ValueError("Immutable composition input changed during build")
        _phase(staging, release_id, "complete_candidate_pending_review")
        current["files"] = [{"path": p.relative_to(staging).as_posix(), "bytes": p.stat().st_size,
                             "sha256": sha256(p)} for p in sorted(staging.rglob("*")) if p.is_file()]
        _write(staging / "manifest.json", current)
        os.replace(staging, output)
        return current
    except BaseException as exc:
        if db is not None:
            db.close()
        _write(staging / "build-failure.json", {"phase": "failed", "error_type": type(exc).__name__,
                                              "message": "Composition failed; staging not activated"})
        raise
