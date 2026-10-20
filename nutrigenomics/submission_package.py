"""Portable submission supplements; immutable scientific matrices are never rewritten.

The default validation reuses a pinned full-file verification receipt only when
every source stat/WAL identity is unchanged. Assembly is a separate explicit step
which streams and hashes every copied byte before atomic publication.
"""
from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
import zipfile
from pathlib import Path, PurePosixPath

KEY_FIELDS = ["series_accession", "feature_type", "feature_id", "contrast", "analysis_version"]
PRIVATE = re.compile(r"(?:^|[\s\"'])(?:/(?:home|Users|srv|mnt|dev|tmp)/|[A-Za-z]:[\\/])|https?://(?:127\.0\.0\.1|100\.\d+\.\d+\.\d+)")
SHA = re.compile(r"^[0-9a-f]{64}$")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    assert_portable(value)
    Path(path).write_text(canonical(value) + "\n", encoding="utf-8")


def safe_path(root, name):
    rel = PurePosixPath(name)
    if not name or rel.is_absolute() or ".." in rel.parts or "\\" in name or ":" in name or str(rel) != name:
        raise ValueError("Nonportable or unsafe file path")
    root = Path(root).resolve()
    path = root.joinpath(*rel.parts)
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Symlink in package/source path")
    if not path.resolve().is_relative_to(root):
        raise ValueError("File escapes declared root")
    return path


def fingerprint(path):
    path = Path(path).resolve()
    def state(item):
        if not item.exists():
            return None
        s = item.stat()
        return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
    wal = Path(str(path) + "-wal")
    # An empty WAL contributes no SQLite pages, but retain the raw preflight elsewhere.
    return [str(path), state(path), state(wal) if wal.exists() and wal.stat().st_size else None]


def assert_portable(value):
    text = canonical(value)
    if PRIVATE.search(text):
        raise ValueError("Private operational path/host in portable metadata")
    if "\ufffd" in text:
        raise ValueError("Actual Unicode replacement character; do not invent a repair")


def inherited_files(release, receipt_path, receipt_sha256, expected_manifest_sha256):
    release = Path(release).resolve()
    if sha256(receipt_path) != receipt_sha256:
        raise ValueError("Pinned verification receipt changed")
    receipt = _read(receipt_path)
    if not all(receipt.get(k) is True for k in ("passed", "complete", "all_manifest_files_hash_verified", "source_files_unchanged")):
        raise ValueError("Prior full verification was incomplete")
    if sha256(release / "manifest.json") != expected_manifest_sha256:
        raise ValueError("Pinned base manifest changed")
    manifest = _read(release / "manifest.json")
    if manifest.get("release_id") != receipt["public_release_id"] or not isinstance(manifest.get("files"), list):
        raise ValueError("Prior verification release mismatch")
    states = receipt["source_fingerprints_after"]
    mpath = str(release / "manifest.json")
    if mpath in states and fingerprint(mpath) != states[mpath]:
        raise ValueError("Source manifest identity changed")
    files = list(manifest["files"])
    if not any(r["path"] == "manifest.json" for r in files):
        files.append({"path": "manifest.json", "bytes": Path(mpath).stat().st_size, "sha256": sha256(mpath)})
    seen = set()
    for row in files:
        name = row["path"]
        if name in seen or not SHA.fullmatch(row["sha256"]):
            raise ValueError("Duplicate file or invalid hash")
        seen.add(name)
        path = safe_path(release, name)
        if not path.is_file() or path.stat().st_size != row["bytes"]:
            raise ValueError("Source file missing or size changed: " + name)
        if name != "manifest.json" and fingerprint(path) != states.get(str(path)):
            raise ValueError("Verified source identity changed: " + name)
    return manifest, files


def apply_corrections(records, proposals, proofs):
    records = copy.deepcopy(records)
    positions = {(r["kind"], str(r["id"])): i for i, r in enumerate(records)}
    if len(positions) != len(records):
        raise ValueError("Duplicate science record key")
    ledger, seen = [], set()
    if proposals.get("automatic_expert_approval") is not False:
        raise ValueError("Corrections cannot grant expert approval")
    for patch in proposals.get("patches", []):
        key = (patch["record_kind"], str(patch["record_id"]))
        if key in seen or key not in positions or not patch.get("proof_ids"):
            raise ValueError("Unknown/duplicate/unproved correction")
        seen.add(key)
        for proof_id in patch["proof_ids"]:
            if proof_id not in proofs or not proofs[proof_id].get("url", "").startswith("https://"):
                raise ValueError("Correction lacks source proof URL")
        wrapper = records[positions[key]]
        value = wrapper["record"]
        if digest(value) != patch["old_record_sha256"]:
            raise ValueError("Correction old record hash mismatch")
        if patch.get("classification") not in {"mechanical_source_link", "exact_byte_restoration", "unrecoverable_encoding_missingness", "additive_source_method_annotation"}:
            raise ValueError("Unrecognized correction class")
        for change in patch["changes"]:
            path = change["path"]
            if change.get("operation") != "replace" or not path or any(not isinstance(k, (str, int)) or isinstance(k, bool) for k in path):
                raise ValueError("Invalid correction path/operation")
            if any("approval" in str(k) or "review_status" in str(k) for k in path):
                raise ValueError("Mechanical correction cannot approve science")
            if patch["classification"] == "additive_source_method_annotation":
                old, new = change["old_value"], change["new_value"]
                if path != ["metadata"] or not isinstance(old, dict) or not isinstance(new, dict) or "source_method_notes" in old or set(new) != set(old) | {"source_method_notes"} or any(new[k] != v for k, v in old.items()):
                    raise ValueError("Method annotation must preserve every existing metadata value")
                notes = new["source_method_notes"]
                if not isinstance(notes, list) or not notes:
                    raise ValueError("Source method notes must be a nonempty list")
                for note in notes:
                    if not isinstance(note, dict) or note.get("actor") != "AI_precheck" or note.get("expert_review_status") != "pending" or note.get("scientific_approval") is not False or not note.get("source_url", "").startswith("https://") or not SHA.fullmatch(note.get("source_file_sha256", "")) or not all(isinstance(note.get(k), str) and note[k] for k in ["id", "source_locator", "source_fact", "interpretation_limit"]):
                        raise ValueError("Method note requires source proof and unapproved AI status")
            elif patch["classification"] == "mechanical_source_link":
                if str(path[-1]).lower() not in {"url", "source_url", "publication_url", "primary_publication_url"} or not isinstance(change["new_value"], str) or not change["new_value"].startswith("https://"):
                    raise ValueError("Source-link patch may change only an HTTPS source URL")
            elif not any(proofs[p].get("restored_value_sha256") == digest(change["new_value"]) for p in patch["proof_ids"]):
                raise ValueError("Byte restoration requires an exact restored-value proof")
            parent = value
            for k in path[:-1]:
                parent = parent[k]
            if parent[path[-1]] != change["old_value"]:
                raise ValueError("Correction old value mismatch")
            parent[path[-1]] = change["new_value"]
        if value != patch["proposed_record"] or digest(value) != patch["new_record_sha256"]:
            raise ValueError("Correction new record hash/proposal mismatch")
        if patch.get("expert_review_status") != "pending":
            raise ValueError("Correction status must remain pending")
        assert_portable(value)
        ledger.append({**{k: patch[k] for k in ["record_kind", "record_id", "old_record_sha256", "new_record_sha256", "classification", "changes", "proof_ids", "reason"]}, "expert_review_status": "pending"})
    return records, ledger


def rights_audit(catalog, policy, manifest):
    admitted, excluded = [], []
    seen = set()
    for source in catalog:
        if source["id"] in seen:
            raise ValueError("Duplicate source ID")
        seen.add(source["id"])
        licensed = isinstance(source.get("license"), str) and source["license"].lower() not in {"", "unknown", "redistribution-unconfirmed"}
        source_url = source.get("source_url") or source.get("url") or source.get("original_url")
        terms_url = source.get("terms_url") or source.get("rights_url")
        if source["id"].startswith("PubMed-candidates:"):
            terms_url = "https://www.nlm.nih.gov/databases/download.html"
        elif source["id"].startswith("CTD:"):
            terms_url = "https://ctdbase.org/about/legal.jsp"
        elif source["id"].startswith("USDA-FDC:"):
            terms_url = "https://fdc.nal.usda.gov/api-guide/"
        elif source["id"].startswith("SCREEN:"):
            terms_url = "https://www.encodeproject.org/about/data-use-policy/"
        elif source.get("license") == "CC-BY-4.0":
            terms_url = terms_url or "https://creativecommons.org/licenses/by/4.0/"
        row = {"source_id": source["id"], "source_license": source.get("license"),
               "terms_url": terms_url or source_url, "source_url": source_url,
               "human_final_rights_approval": "pending"}
        if source.get("export_allowed") is True:
            if not licensed:
                raise ValueError("Source allowed without a declared rights basis")
            admitted.append({**row, "machine_decision": "permitted_by_recorded_source_policy"})
        else:
            excluded.append({**row, "machine_decision": "exclude_values_keep_citation_only",
                             "reason": "Explicit denial or redistribution not established; no blanket open license"})
    analyses, excluded_analyses, analysis_keys = {}, [], set()
    for row in policy.get("analyses", []):
        key = (row["series_accession"], row["analysis_version"])
        if key in analysis_keys:
            raise ValueError("Invalid/duplicate quantitative source rights")
        analysis_keys.add(key)
        if row.get("export_allowed") is not True:
            excluded_analyses.append({"series_accession": key[0], "analysis_version": key[1], "machine_decision": "exclude_quantitative_results", "terms_url": row.get("terms_url"), "human_final_rights_approval": "pending"})
            continue
        if row.get("own_results_license") != "CC-BY-4.0" or not row.get("terms_url", "").startswith("https://"):
            raise ValueError("Allowed quantitative source lacks declared license/terms")
        analyses[key] = row
    layers = manifest.get("candidate_layers", {})
    grl = layers.get("graphreg_rights", {})
    if layers.get("graphreg", {}).get("admitted", 0) and (grl.get("export_allowed") is not True or grl.get("scientific_approval") is not False):
        raise ValueError("GraphRegLR rights/prediction distinction missing")
    return {"schema_version": "submission-rights-machine-audit-v1", "machine_policy_checks_passed": True,
            "human_final_rights_approval": "pending", "blanket_third_party_license": False,
            "admitted_policy_sources": admitted, "excluded_value_sources": excluded,
            "quantitative_analysis_policies": len(analyses),
            "excluded_quantitative_analysis_policies": excluded_analyses,
            "graphreg": {**grl, "evidence_kind": "cell_line_computational_prediction_not_dietary_efficacy"},
            "literature": {**layers.get("literature", {}), "abstracts_and_automatic_assertions": "withheld"}}, analyses


def _zip_inventory(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate producer archive member")
        result = {}
        for name in names:
            safe_path(Path.cwd(), name.rstrip("/"))
            if not name.endswith("/"):
                result[name] = hashlib.sha256(archive.read(name)).hexdigest()
        return result


def _science_sqlite(path, records, selectors, base_release_id):
    """Small authoritative metadata store, with no copied effect values."""
    db = sqlite3.connect(path)
    try:
        db.executescript("""
            CREATE TABLE science_record(kind TEXT NOT NULL,id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,record_sha256 TEXT NOT NULL,PRIMARY KEY(kind,id));
            CREATE TABLE effect_catalog(series_accession TEXT NOT NULL,feature_type TEXT NOT NULL,
                contrast TEXT NOT NULL,analysis_version TEXT NOT NULL,total INTEGER NOT NULL,
                files_json TEXT NOT NULL,rights_policy_id TEXT NOT NULL,
                PRIMARY KEY(series_accession,feature_type,contrast,analysis_version));
            CREATE TABLE package_meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
        """)
        db.executemany("INSERT INTO science_record VALUES(?,?,?,?)", [(r["kind"], str(r["id"]), canonical(r["record"]), digest(r["record"])) for r in records])
        db.executemany("INSERT INTO effect_catalog VALUES(?,?,?,?,?,?,?)", [(r["series_accession"], r["feature_type"], r["contrast"], r["analysis_version"], r["total"], canonical(r["files"]), r["rights_policy_id"]) for r in selectors])
        db.executemany("INSERT INTO package_meta VALUES(?,?)", [("immutable_base_release_id", canonical(base_release_id)), ("authoritative_science", "true"), ("base_science_record_superseded", "true"), ("five_part_effect_key", canonical(KEY_FIELDS)), ("publication_ready", "false")])
        db.commit()
        observed = {(kind, record_id): (json.loads(payload), sha) for kind, record_id, payload, sha in db.execute("SELECT kind,id,metadata_json,record_sha256 FROM science_record")}
        expected = {(r["kind"], str(r["id"])): (r["record"], digest(r["record"])) for r in records}
        if observed != expected or db.execute("SELECT sum(total) FROM effect_catalog").fetchone()[0] != sum(r["total"] for r in selectors):
            raise ValueError("Authoritative SQLite and typed JSON payloads differ")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Authoritative small SQLite is invalid")
    finally:
        db.close()


def prepare(release, code_archive, original_builder_archive, dictionary, prior_verification,
            expected_prior_sha256, expected_manifest_sha256, output, *, corrections=None, proofs=None,
            expected_code_sha256=None, expected_builder_archive_sha256=None, official_policy_snapshot=None,
            current_reader_archive=None, expected_reader_sha256=None):
    release, output = Path(release).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(release) or release.is_relative_to(output):
        raise ValueError("New output must be separate from the immutable release")
    manifest, inherited = inherited_files(release, prior_verification, expected_prior_sha256, expected_manifest_sha256)
    if bool(current_reader_archive) != bool(expected_reader_sha256):
        raise ValueError("New reader archive and its expected hash are required together")
    auxiliary_sources = [code_archive, original_builder_archive, dictionary]
    if current_reader_archive:
        auxiliary_sources.append(current_reader_archive)
    auxiliary_fingerprints = {str(Path(path).resolve()): fingerprint(path) for path in auxiliary_sources}
    metadata = _read(release / "dataset-metadata.json")
    if metadata["release_id"] != manifest["release_id"] or metadata.get("blanket_third_party_license") is not False:
        raise ValueError("Public release identity/license mismatch")
    def bound_small(name):
        row = next(r for r in inherited if r["path"] == name)
        path = safe_path(release, name)
        if path.stat().st_size > 20 * 1024**2 or sha256(path) != row["sha256"]:
            raise ValueError("Small authoritative source file changed")
        return path
    with gzip.open(bound_small("tables/science_record.csv.gz"), "rt", encoding="utf-8", newline="") as f:
        records = [{"kind": r["kind"], "id": r["id"], "record": json.loads(r["metadata_json"])} for r in csv.DictReader(f)]
    catalog, policy = _read(bound_small("source-catalog.json")), _read(bound_small("rights-policy.json"))
    audit, permissions = rights_audit(catalog, policy, manifest)
    candidate_file = next((r for r in inherited if r["path"] == "tables/candidate_document.csv.gz"), None)
    candidate_count = 0
    if candidate_file:
        with gzip.open(bound_small(candidate_file["path"]), "rt", encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                candidate_count += 1
                payload = json.loads(row["metadata_json"])
                if int(row["candidate_count"]) < 0 or payload.get("text_export_allowed") is not False or payload.get("assertion_visibility") != "withheld" or any(payload.get(k) for k in ["abstract", "full_text", "sentences", "assertions"]):
                    raise ValueError("Withheld literature text/assertions leaked into public export")
    audit["literature"]["citation_rows_checked"] = candidate_count
    if isinstance(corrections, (Path, str)):
        corrections = _read(corrections)
    if isinstance(proofs, (Path, str)):
        proofs = _read(proofs)
    corrections = corrections or {"automatic_expert_approval": False, "patches": []}
    proofs = proofs or {}
    assert_portable(proofs)
    records, ledger = apply_corrections(records, corrections, proofs)
    for row in records:
        assert_portable(row)
    code_archive, original_builder_archive = Path(code_archive), Path(original_builder_archive)
    code_hash, builder_hash = sha256(code_archive), sha256(original_builder_archive)
    if code_hash != (expected_code_sha256 or _read(prior_verification)["exact_code_archive_sha256"]):
        raise ValueError("Exact runtime code archive changed")
    if not expected_builder_archive_sha256 or builder_hash != expected_builder_archive_sha256:
        raise ValueError("Original producer archive not explicitly hash-bound")
    current_code, producer_code = _zip_inventory(code_archive), _zip_inventory(original_builder_archive)
    reader_code = {}
    if current_reader_archive:
        if sha256(current_reader_archive) != expected_reader_sha256:
            raise ValueError("Current workflow reader archive changed")
        reader_code = _zip_inventory(current_reader_archive)
        if reader_code.get("code/nutrigenomics/submission_package.py") != sha256(__file__):
            raise ValueError("Current workflow archive does not contain these exact curation producer bytes")
    if manifest["builder_sha256"] not in producer_code.values():
        raise ValueError("Original release builder bytes absent from producer archive")
    producer_refs = []
    for row in records:
        if row["kind"] == "analysis_run":
            run = row["record"]
            expected = run.get("source_script_sha256") or run.get("metadata", {}).get("script_sha256")
            found = sorted(name for name, value in {**producer_code, **current_code}.items() if value == expected)
            if not expected or not found:
                raise ValueError("Exact quantitative producer script bytes unavailable")
            producer_refs.append({"series_accession": run["series_accession"], "analysis_version": run["analysis_version"],
                                  "producer_script_sha256": expected, "archive_members": found})
    selectors, selector_keys = [], set()
    with gzip.open(bound_small("tables/effect_selector.csv.gz"), "rt", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key = (row["series_accession"], row["analysis_version"])
            selector_key = (row["series_accession"], row["feature_type"], row["contrast"], row["analysis_version"])
            if selector_key in selector_keys:
                raise ValueError("Duplicate native effect selector")
            selector_keys.add(selector_key)
            if key not in permissions or int(row["total"]) < 0:
                raise ValueError("Quantitative selector lacks source-specific permission")
            part = digest(list(key))[:16]
            links = []
            for extension in [".parquet", ".csv.gz"]:
                name = "effects/" + part + extension
                item = next((x for x in inherited if x["path"] == name), None)
                if not item:
                    raise ValueError("Effect partition not in exact release manifest")
                links.append({"path": "base/" + name, "sha256": item["sha256"], "bytes": item["bytes"]})
            selectors.append({**row, "total": int(row["total"]), "files": links, "rights_policy_id": permissions[key]["id"]})
    if sum(r["total"] for r in selectors) != metadata["counts"]["effect"]:
        raise ValueError("Effect catalog total differs from canonical count")
    dictionary_value = _read(dictionary)
    assert_portable(dictionary_value)
    if dictionary_value.get("release_id") != metadata["release_id"]:
        raise ValueError("Dictionary belongs to a different public base")
    staging = output.parent / ("." + output.name + ".partial-" + uuid.uuid4().hex)
    staging.mkdir(parents=True)
    try:
        with (staging / "science-records.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for row in records:
                f.write(canonical({**row, "record_sha256": digest(row["record"])}) + "\n")
        with (staging / "science-records.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["kind", "id", "record_sha256", "metadata_json"])
            for row in records:
                writer.writerow([row["kind"], row["id"], digest(row["record"]), canonical(row["record"])])
        _write(staging / "correction-ledger.json", {"base_release_id": metadata["release_id"], "automatic_expert_approval": False, "patches": ledger, "source_proofs": proofs})
        _write(staging / "effect-catalog.json", {"five_part_effect_key": KEY_FIELDS, "selector_grain": [k for k in KEY_FIELDS if k != "feature_id"], "selectors": selectors, "effect_values_duplicated": 0})
        _science_sqlite(staging / "release-science.sqlite3", records, selectors, metadata["release_id"])
        _write(staging / "supplement-data-dictionary.json", {
            "database": "release-science.sqlite3", "scope": "Authoritative corrected science metadata; no effect values copied",
            "science_record": {"primary_key": ["kind", "id"], "metadata_json": "Original typed scientific payload plus exact approved-for-mechanical-application patches; still pending expert review", "record_sha256": "SHA256 of UTF8 compact sorted-key JSON, ensure_ascii=false/allow_nan=false, payload only"},
            "effect_catalog": {"primary_key": ["series_accession", "feature_type", "contrast", "analysis_version"], "total": "Feature rows in original native matrix, not participants", "files_json": "Relative base/effects paths with exact SHA256 and byte lengths; feature_id completes the five-part key inside those files", "rights_policy_id": "Source-specific derived-result permission, not blanket raw-source licensing"},
            "package_meta": {"primary_key": ["key"], "value_json": "Typed JSON release identity and explicit precedence flags"},
            "base_science_record": "Historical and superseded for matching kind/id; never silently prefer base over corrected main"})
        (staging / "REUSE.sql").write_text("-- Open supplement/release-science.sqlite3 as main. Only science metadata in main is authoritative.\n-- Attach the unchanged base by a relative path appropriate to your working directory:\nATTACH DATABASE 'base/nutriomics.sqlite3' AS base;\nSELECT kind,id,record_sha256,json_extract(metadata_json,'$.publication_review_status') AS review_status FROM main.science_record WHERE kind='study';\nSELECT series_accession,feature_type,contrast,analysis_version,total,files_json FROM main.effect_catalog;\n-- Base science_record/graph-cached metadata remain historical and are superseded by main.science_record.\n-- Base composition/graph/effect values are unchanged; no causal/mediation inference follows from a join.\nSELECT food,component,amount,unit,status,basis FROM base.measurement LIMIT 10;\n", encoding="utf-8")
        _write(staging / "rights-machine-audit.json", audit)
        with (staging / "rights-human-decisions.csv").open("w", encoding="utf-8", newline="") as f:
            fields = ["source_id", "source_license", "terms_url", "source_url", "machine_decision", "human_final_rights_approval", "reason"]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows({k: row.get(k) for k in fields} for row in audit["admitted_policy_sources"] + audit["excluded_value_sources"])
        _write(staging / "public-data-dictionary.json", dictionary_value)
        _write(staging / "official-policy-snapshot.json", official_policy_snapshot or {"status": "not_freshly_checked", "human_approval": "pending"})
        _write(staging / "producer-provenance.json", {"exact_legacy_runtime_code_sha256": code_hash, "legacy_runtime_code_files": sum(name.startswith("code/") for name in current_code), "current_workflow_code_sha256": expected_reader_sha256, "current_workflow_code_files": sum(name.startswith("code/") for name in reader_code), "preparation_module_sha256": sha256(__file__), "preparation_module_distinct_from_immutable_runtime_archive": True, "original_producer_archive_sha256": builder_hash, "exact_original_builder_sha256": manifest["builder_sha256"], "analysis_script_references": producer_refs, "full_raw_or_R_reconstruction_rerun": False})
        blockers = ["hash-bound final scientific/food/gold human decisions", "final human source-rights assessment and mixed-base record license", "professor-supplied authors/affiliations/funding/ethics", "repository account and final DOI registration", "HTTPS and verified rollback/operating deployment", "actual offline HTML execution"]
        status = {"schema_version": "submission-candidate-package-v1", "release_id": "nutriomics-submission-candidate-20261020-v1", "immutable_base_release_id": metadata["release_id"], "machine_package_integrity": True, "current_workflow_archive_bound": bool(current_reader_archive), "publication_ready": False, "uploaded": False, "doi": None, "human_rights_approval": "pending", "human_scientific_approval": "pending", "blockers": blockers, "authoritative_curation": ["science-records.jsonl", "science-records.csv", "release-science.sqlite3"], "precedence": "Corrected science records supersede only matching kind/id metadata in the explicitly uncorrected immutable base. Numerical matrices and original base bytes are unchanged.", "corrections": len(ledger), "science_records": len(records), "effect_rows_referenced": sum(r["total"] for r in selectors), "base_full_hash_verification": {"receipt_sha256": expected_prior_sha256, "manifest_sha256": expected_manifest_sha256, "method": "Previously full-hashed files with unchanged current source stat/WAL; small metadata freshly hashed; no20GB rehash"}}
        _write(staging / "package-status.json", status)
        # Record licenses apply to whole repository records. Mixed upstream files
        # therefore remain a distinct unresolved-license draft, never blanket CC BY.
        drafts = {
            "curation": {"upload_type": "dataset", "access_right": "open", "license": "cc-by", "title": "NutriOmics corrected human-intervention curation supplement"},
            "base": {"upload_type": "dataset", "access_right": "open", "license": None, "title": "NutriOmics immutable public base and quantitative results"},
            "code": {"upload_type": "software", "access_right": "open", "license": "mit", "title": "NutriOmics exact runtime and original producer code"}}
        for name, draft in drafts.items():
            draft.update(description="Research-only source-traceable food composition and human dietary-intervention context. Computational predictions and clinical co-reporting are distinct from causal dietary efficacy. Expert review pending.", creators=[], keywords=["nutrigenomics", "DNA methylation", "food composition"], version=status["release_id"])
            _write(staging / ("zenodo-" + name + ".draft.json"), {"metadata": draft})
        (staging / "ZENODO_COMMANDS.md").write_text("# Ready-to-adapt draft commands\n\nOfficial schema: https://developers.zenodo.org/ ; limits: https://help.zenodo.org/docs/deposit/manage-files/ . These commands have NOT been run. Fill professor-owned creators/affiliations and final source-rights license first; do not upload operator-only absolute-path resolvers. Keep base, curation and code records distinct and cross-link their eventual version DOIs.\n\n```sh\n# Store a deposit:write token locally; never put its value in a URL or source file.\n# Create draft only after metadata and hash-bound human gates are resolved:\ncurl -H \"Authorization: Bearer ${ZENODO_ACCESS_TOKEN:?}\" -H \"Content-Type: application/json\" -X POST --data '{}' https://zenodo.org/api/deposit/depositions\n# Use returned draft ID and bucket URL (no fabricated ID/DOI):\ncurl -H \"Authorization: Bearer ${ZENODO_ACCESS_TOKEN:?}\" -H \"Content-Type: application/json\" -X PUT --data-binary @zenodo-curation.draft.json \"https://zenodo.org/api/deposit/depositions/${DRAFT_ID:?}\"\ncurl -H \"Authorization: Bearer ${ZENODO_ACCESS_TOKEN:?}\" --upload-file \"${VERIFIED_ASSEMBLED_ARCHIVE:?}\" \"${RETURNED_BUCKET_URL:?}/nutriomics-curation.zip\"\n```\n\nNo publish command is supplied: inspect draft metadata, file checksums, record licenses and release binding before publication. Assemble/ZIP packaging is a separate optional full-byte copy, not performed by prepare. If using individual files, preserve the filemap paths or provide an explicit reversible download-name mapping.\n", encoding="utf-8")
        (staging / "README.md").write_text("# NutriOmics submission candidate\n\nThis supplement is authoritative for science metadata after the listed hash-bound corrections. The immutable base SQLite and cached graph metadata are explicitly uncorrected historical inputs. Apply `correction-ledger.json` by exact kind/id/hash or read `science-records.jsonl`/CSV; do not call the base corrected. Matrices keep original five-part keys, values, permissions and relative file references.\n\nNo new full raw-source build or14 R analyses were run. A new public-only environment previously verified reuse of an existing public package; this differs from raw reconstruction.\n\nSource terms remain source-specific. Denied/unconfirmed value sources and literature assertions remain excluded; source-catalog citations do not authorize redistribution. GraphRegLR is optional cell-line computation, not a food-health effect.\n\nZenodo official developer documentation specifies50GB/record and100files; draft metadata intentionally has no creators and no final mixed-base license until professor decisions. Nothing is uploaded, no DOI is invented. Set creators and the reviewed base license locally, then validate official API fields before creating a draft. Use bucket streaming for large files, not the old100MB endpoint.\n\n`python -m scripts.prepare_submission_package assemble --package PACKAGE --expected-manifest-sha256 SHA --roots-json OPERATOR_ROOTS --destination NEW_DIRECTORY` verifies every copied byte and publishes only a complete atomic directory. Roots are local operator-only aliases public_base/code/producer/supplement; never deposit an absolute-path resolver. No source copies are made by `prepare`.\n", encoding="utf-8")
        filemap = [{"path": "base/" + r["path"], "source_alias": "public_base", "source_path": r["path"], "bytes": r["bytes"], "sha256": r["sha256"], "role": "immutable_uncorrected_base"} for r in inherited]
        for alias, path in [("code", code_archive), ("producer", original_builder_archive)]:
            filemap.append({"path": "code/" + ("runtime.zip" if alias == "code" else "original-producer.zip"), "source_alias": alias, "source_path": path.name, "bytes": path.stat().st_size, "sha256": sha256(path), "role": alias})
        if current_reader_archive:
            path = Path(current_reader_archive)
            filemap.append({"path": "code/current-reader.zip", "source_alias": "reader", "source_path": path.name, "bytes": path.stat().st_size, "sha256": expected_reader_sha256, "role": "current_reader_and_curation_producer"})
        for path in sorted(staging.iterdir()):
            filemap.append({"path": "supplement/" + path.name, "source_alias": "supplement", "source_path": path.name, "bytes": path.stat().st_size, "sha256": sha256(path), "role": "authoritative_supplement"})
        package = {"schema_version": "portable-submission-filemap-v1", "release_id": status["release_id"], "files": filemap, "repository": {"recommended": "Zenodo", "official_schema_url": "https://developers.zenodo.org/", "planning_bytes_per_record": 50_000_000_000, "planning_files_per_record": 100}, "publication_ready": False}
        assert_portable(package)
        assert len(filemap) <= 100 and sum(r["bytes"] for r in filemap) <= 50_000_000_000
        _write(staging / "filemap.json", package)
        # Fail closed if an immutable source or auxiliary archive changed while
        # metadata was read. This is not a claim of cross-filesystem atomicity.
        inherited_files(release, prior_verification, expected_prior_sha256, expected_manifest_sha256)
        if any(fingerprint(path) != before for path, before in auxiliary_fingerprints.items()):
            raise ValueError("Auxiliary package source changed during preparation")
        os.replace(staging, output)
        return {**status, "filemap_sha256": sha256(output / "filemap.json"), "file_count": len(filemap), "referenced_bytes": sum(r["bytes"] for r in filemap)}
    except BaseException:
        _write(staging / "FAILED.json", {"complete": False, "publication_ready": False})
        raise


def assemble(package, expected_manifest_sha256, roots, destination):
    """Explicit potentially-large copy; verify complete bytes and finalize atomically."""
    package, destination = Path(package).resolve(), Path(destination).resolve()
    if sha256(package / "filemap.json") != expected_manifest_sha256:
        raise ValueError("Pinned portable manifest changed")
    manifest = _read(package / "filemap.json")
    roots = {**roots, "supplement": str(package)}
    if destination.exists() or any(destination.is_relative_to(Path(p).resolve()) or Path(p).resolve().is_relative_to(destination) for p in roots.values()):
        raise ValueError("Assembly target must be new and separate from all sources")
    staging = destination.parent / ("." + destination.name + ".partial-" + uuid.uuid4().hex)
    staging.mkdir(parents=True)
    seen = set()
    try:
        for row in manifest["files"]:
            if row["path"] in seen or row["source_alias"] not in roots:
                raise ValueError("Duplicate target or unresolved source alias")
            seen.add(row["path"])
            source = safe_path(roots[row["source_alias"]], row["source_path"])
            target = safe_path(staging, row["path"])
            before = fingerprint(source)
            if not source.is_file() or source.stat().st_size != row["bytes"] or before[2] is not None:
                raise ValueError("Missing/changed/WAL-bearing assembly source")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            if target.stat().st_size != row["bytes"] or sha256(target) != row["sha256"] or fingerprint(source) != before:
                raise ValueError("Partial/changed copy; assembly is not publishable")
        _write(staging / "ASSEMBLY.json", {"complete": True, "filemap_sha256": expected_manifest_sha256, "publication_ready": False})
        os.replace(staging, destination)
    except BaseException:
        _write(staging / "FAILED.json", {"complete": False, "publication_ready": False})
        raise
    return {"complete": True, "files": len(seen), "publication_ready": False}
