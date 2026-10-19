"""Read-only byte, sample-scope and declared-model audit of frozen analyses.

This audit never reruns R, changes a scientific record, or grants human approval.
Files are resolved inside explicit search roots and matched by full SHA256,
not by basename alone. Missing files remain unavailable, never silently passed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

HASH = re.compile(r"^[a-f0-9]{64}$")
SKIP_DIRS = {".git", "node_modules", ".venv", ".venv-win", "__pycache__", ".cache"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def basename(value):
    if isinstance(value, dict):
        value = value.get("filename")
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def _finding(code, message, severity="review"):
    return {"code": code, "severity": severity, "message": message}


class ArtifactResolver:
    """Bounded, no-symlink traversal; full file hashing cached by stable stat."""
    def __init__(self, roots, requested_names, max_hash_bytes=64 * 1024**3):
        self.roots = [Path(r).resolve(strict=True) for r in roots]
        if any(not r.is_dir() for r in self.roots):
            raise ValueError("search roots must be directories")
        self.index = defaultdict(list)
        self.cache = {}
        self.hashed_bytes = 0
        self.max_hash_bytes = max_hash_bytes
        requested = set(requested_names)
        for root in self.roots:
            for folder, directories, files in os.walk(root, followlinks=False):
                directories[:] = [n for n in directories if n not in SKIP_DIRS and not (Path(folder) / n).is_symlink()]
                for name in requested.intersection(files):
                    p = (Path(folder) / name).resolve()
                    if p.is_file() and any(p.is_relative_to(r) for r in self.roots):
                        self.index[name].append(p)
        for name in self.index:
            self.index[name] = sorted(set(self.index[name]), key=str)

    def digest(self, path):
        before = path.stat()
        key = (str(path), before.st_size, before.st_mtime_ns)
        if key in self.cache:
            return self.cache[key]
        if self.max_hash_bytes and self.hashed_bytes + before.st_size > self.max_hash_bytes:
            return None
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for part in iter(lambda: stream.read(4 * 1024**2), b""):
                h.update(part)
        after = path.stat()
        self.hashed_bytes += before.st_size
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("artifact changed during read: " + str(path))
        self.cache[key] = h.hexdigest()
        return self.cache[key]

    def verify(self, spec):
        result = {**spec, "status": "unavailable", "verified_path": None}
        expected = spec.get("sha256")
        if not HASH.fullmatch(expected or ""):
            result["status"] = "expected_hash_missing_or_invalid"
            return result
        names = spec.get("filenames") or [spec.get("filename")]
        paths = sorted({p for n in names for p in self.index.get(n, [])}, key=str)
        declared = Path(spec.get("declared_path") or "")
        exact_bound_path = None
        if declared.is_absolute():
            candidate = declared.resolve()
            if candidate.is_file() and any(candidate.is_relative_to(r) for r in self.roots):
                exact_bound_path = candidate
                paths = [candidate]
        result["candidate_paths_count"] = len(paths)
        if not paths:
            return result
        attempts, budget = [], False
        for path in paths:
            try:
                if spec.get("bytes") is not None and path.stat().st_size != spec["bytes"]:
                    attempts.append({"path": str(path), "status": "byte_size_mismatch", "bytes": path.stat().st_size})
                    continue
                actual = self.digest(path)
                if actual is None:
                    budget = True
                    continue
                if actual == expected:
                    return {**result, "status": "verified", "verified_path": str(path), "actual_sha256": actual,
                            "resolved_by": "full_content_hash_not_basename", "bytes": path.stat().st_size}
                attempts.append({"path": str(path), "status": "sha256_mismatch", "actual_sha256": actual})
            except (OSError, ValueError) as exc:
                attempts.append({"path": str(path), "status": "read_failed_or_changed", "error": str(exc)})
        status = "not_checked_hash_budget" if budget else (
            "mismatch_or_unreadable" if exact_bound_path else "expected_content_unavailable")
        return {**result, "status": status,
                "attempts": attempts[:20], "attempts_truncated": len(attempts) > 20}


def _manifest_specs(items, role):
    out = []
    for item in items or []:
        if item.get("sha256"):
            name = basename(item.get("path") or item.get("filename"))
            if name:
                out.append({"role": role, "filename": name, "sha256": item["sha256"], "bytes": item.get("bytes"),
                            "declared_path": item["path"] if isinstance(item.get("path"), str) else None})
        if item.get("input_file_sha256"):
            out.append({"role": role, "filename": basename(item.get("input_file")),
                        "sha256": item["input_file_sha256"], "bytes": item.get("input_compressed_bytes")})
    return out


def artifact_specs(analysis, source_audit=None):
    meta = analysis.get("metadata") or {}
    source_audit = source_audit or {}
    script = basename(meta.get("script_path") or analysis.get("source_script_path") or source_audit.get("source_file"))
    specs = [{"role": "producer", "filename": script,
              "sha256": analysis.get("source_script_sha256") or meta.get("script_sha256")}]
    specs.append({"role": "environment_lock", "filename": basename(meta.get("environment_lock_path") or analysis.get("environment_lock_path")) or "r-environment-linux-64.lock.txt",
                  "sha256": analysis.get("environment_lock_sha256") or meta.get("environment_lock_sha256") or meta.get("environment_lock_file_current_sha256")})
    if meta.get("helper_sha256"):
        helper = basename(meta.get("helper_path")) or ("gse54643_methylation_helpers.R" if analysis.get("series_accession") == "GSE54643" else "bridge_omics_helpers.R")
        specs.append({"role": "helper", "filename": helper, "sha256": meta["helper_sha256"]})
    if meta.get("overlay_lock_sha256"):
        specs.append({"role": "overlay_lock", "filename": basename(meta.get("overlay_lock_path")) or "r-overlay-lock.txt", "sha256": meta["overlay_lock_sha256"]})
    specs += _manifest_specs(analysis.get("input_manifest") or meta.get("input_manifest"), "input")
    specs += _manifest_specs(analysis.get("output_manifest"), "output")
    if not any(s["role"] == "input" for s in specs) and meta.get("input_file_sha256"):
        specs += _manifest_specs([meta], "input")
    unique = {}
    for spec in specs:
        unique[(spec["role"], spec["filename"], spec.get("sha256"))] = spec
    return list(unique.values())


def load_science(*, science_json=None, database=None):
    if bool(science_json) == bool(database):
        raise ValueError("supply exactly one science JSON or read-only SQLite")
    if science_json:
        payload = json.loads(Path(science_json).read_text(encoding="utf-8"))
        records = payload.get("science_records", [])
        release = payload.get("release_id")
    else:
        path = Path(database).resolve(strict=True)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            records = [dict(r) for r in db.execute("SELECT * FROM science_record WHERE kind='analysis_run' ORDER BY id")]
            r = db.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()
            release = json.loads(r[0]) if r else None
    analyses = []
    for row in records:
        if row.get("kind") != "analysis_run":
            continue
        value = row.get("metadata_json")
        value = json.loads(value) if isinstance(value, str) else (value or row.get("record") or {})
        analyses.append({"record_id": str(row["id"]), "value": value})
    return release, analyses


def _read_tsv(path):
    if Path(path).stat().st_size > 20 * 1024**2:
        raise ValueError("sample audit TSV exceeds 20 MiB")
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        rows = list(reader)
        return reader.fieldnames or [], rows


def sample_audit(analysis, verified):
    """Recompute observed file scopes. No person-level independence is inferred."""
    meta = analysis.get("metadata") or {}
    result, findings = [], []
    person_fields = ("person_id", "subject_id", "subject_candidate", "person", "participant_id")
    sample_fields = ("sample_accession", "sample_key", "sample_id", "geo_sample_accession", "sample_label")
    files = [s for s in verified if s["status"] == "verified" and s["role"] == "output"
             and s["filename"] in {"sample_metadata.tsv", "sample_qc.tsv", "person_metadata.tsv", "closed_cell_proportions.tsv"}]
    for spec in files:
        fields, rows = _read_tsv(spec["verified_path"])
        selected = rows
        if "included" in fields:
            if any(r["included"].lower() not in {"true", "false", "1", "0"} for r in rows):
                findings.append(_finding("unrecognized_inclusion_flag", spec["filename"], "error"))
            selected = [r for r in rows if r["included"].lower() in {"true", "1"}]
        person_field = next((f for f in person_fields if f in fields), None)
        sample_field = next((f for f in sample_fields if f in fields), None)
        people = {r[person_field] for r in selected if r[person_field] not in {"", "NA"}} if person_field else None
        item = {"filename": spec["filename"], "sha256": spec["sha256"], "rows": len(rows), "included_rows": len(selected),
                "person_field": person_field, "observed_people": len(people) if people is not None else None,
                "sample_field": sample_field, "scope": "person_rows" if spec["filename"] == "person_metadata.tsv" else "assay_rows",
                "fields": fields}
        if spec["filename"] == "sample_qc.tsv" and "included" not in fields:
            item["inclusion_scope"] = "QC diagnostics only; no inclusion/exclusion flag"
        if people is not None and meta.get("independent_people") is not None and len(people) != meta["independent_people"]:
            findings.append(_finding("person_count_disagrees_with_verified_table", f"{spec['filename']}: file={len(people)}, declared={meta['independent_people']}", "error"))
        if (item["scope"] == "assay_rows" and (spec["filename"] != "sample_qc.tsv" or "included" in fields)
                and meta.get("sample_count") is not None and len(selected) != meta["sample_count"]):
            findings.append(_finding("assay_count_disagrees_with_verified_table", f"{spec['filename']}: included={len(selected)}, declared={meta['sample_count']}", "error"))
        if sample_field:
            ids = [r[sample_field] for r in selected]
            item["duplicate_sample_ids"] = len(ids) - len(set(ids))
            if item["duplicate_sample_ids"]:
                findings.append(_finding("duplicate_included_sample_id", spec["filename"], "error"))
        if person_field:
            excluded = {r[person_field] for r in rows if r not in selected}
            item["excluded_people"] = sorted(excluded)
            if "included" in fields and meta.get("excluded_people") is not None and excluded != set(map(str, meta["excluded_people"])):
                findings.append(_finding("excluded_people_disagree_with_verified_qc", spec["filename"], "error"))
            if "included" in fields and excluded.intersection(people or set()):
                findings.append(_finding("partial_person_exclusion", "A person has included and excluded assays; check declared paired exclusion policy.", "error"))
            dimensions = []
            for alternatives in [("arm", "meal", "dose_kcal", "diet_arm"), ("study_day", "visit", "period"), ("endpoint", "time_hours", "timepoint", "timepoint_candidate")]:
                field = next((f for f in alternatives if f in fields), None)
                if field:
                    dimensions.append(field)
            cells = [(r[person_field], *[r[f] for f in dimensions]) for r in selected]
            item["cell_dimensions"] = [person_field, *dimensions]
            item["duplicate_person_condition_cells"] = len(cells) - len(set(cells)) if dimensions else None
            if dimensions and item["duplicate_person_condition_cells"]:
                findings.append(_finding("duplicate_person_condition_cells", spec["filename"] + ": check mapped condition/visit semantics", "error"))
            if spec["filename"] == "sample_metadata.tsv" and meta.get("samples_per_person"):
                counts = dict(Counter(r[person_field] for r in selected))
                if counts != meta["samples_per_person"]:
                    findings.append(_finding("samples_per_person_disagree", spec["filename"], "error"))
        if analysis.get("series_accession") == "GSE54325" and "channel_source_names_json" in fields:
            directions = []
            for r in rows:
                names = json.loads(r["channel_source_names_json"])
                labels = json.loads(r["channel_labels_json"])
                def val(d, k):
                    v = d.get(k)
                    return v[0] if isinstance(v, list) and v else v
                directions.append(val(labels, "label_ch1") == "Cy3" and val(labels, "label_ch2") == "Cy5"
                                  and bool(re.search("beginning|time 0", str(val(names, "source_name_ch1")), re.IGNORECASE))
                                  and "8 weeks" in str(val(names, "source_name_ch2")).lower())
            item["two_color_directions_verified"] = all(directions)
            if not all(directions):
                findings.append(_finding("two_color_channel_direction_mismatch", spec["filename"], "error"))
        result.append(item)
    if not result:
        findings.append(_finding("sample_tables_unavailable", "No hash-verified sample/QC/person table available; counts and mapping are not independently checked."))
    elif not any(x["person_field"] for x in result):
        findings.append(_finding("person_mapping_unavailable", "Verified output rows exist but no participant mapping; assay counts do not verify independent people."))
    return result, findings


def model_findings(analysis, producer_text):
    """Specific contradictions/limits, with no claim of automated expert review."""
    meta = analysis.get("metadata") or {}
    findings = []
    text = " ".join(str(meta.get(k, "")) for k in ("design", "design_formula", "person_block", "block_variable", "normalization", "analysis_role"))
    assays, people = meta.get("sample_count"), meta.get("independent_people")
    valid_counts = all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in (assays, people))
    if not valid_counts:
        findings.append(_finding("person_assay_counts_missing_or_invalid", "Positive integer participant/model-assay counts are not both declared; no aggregate is synthesized."))
    if (valid_counts and assays > people
            and not re.search(r"person|subject|paired|duplicateCorrelation|repeat", text, re.IGNORECASE)):
        findings.append(_finding("repeated_assay_dependency_not_accounted_in_declaration", "Repeated assays exceed people but no paired/person dependency declaration is identifiable."))
    multiplicity = str(meta.get("multiple_testing", ""))
    if re.search(r"within.*contrast", multiplicity, re.IGNORECASE) and not re.search(r"study|across.*contrast|joint", multiplicity, re.IGNORECASE):
        findings.append(_finding("within_contrast_fdr_only", "Source declares BH within contrast only; study-wide primary-contrast FDR is not established. Preserve as exploratory/historical unless separately corrected."))
    if not multiplicity and "p.adjust" in producer_text:
        findings.append(_finding("multiplicity_scope_in_source_not_metadata", "Producer contains p.adjust, but metadata omits the hypothesis family; code presence is not a curated primary family."))
    if "regions-fixed" in analysis.get("analysis_version", ""):
        findings.append(_finding("fixed_tiles_not_segmented_dmr", "feature_type dmr represents prespecified 1kb tiles (>=3 tested CpGs), not discovered or validated segmented DMRs.", "limitation"))
    if meta.get("derived_analysis"):
        findings.append(_finding("postprocessor_inherits_parent_models", "Current producer adjusts multiplicity; the fitted model and sample mapping must be traced through the hash-verified parent metadata/results.", "limitation"))
    if analysis.get("series_accession") == "GSE54643":
        if "beta_summary$standard_error" in producer_text:
            findings.append(_finding("beta_se_descriptive_m_test_separate", "Reported beta SE is sample SD of paired beta deltas / sqrt(n), whereas P/FDR use a moderated M-value model. It is not SE from a moderated beta-scale fit; do not imply a same-model beta CI."))
        findings.append(_finding("processed_signals_no_idat_qc", "Source is a processed methylated/unmethylated/detection matrix; raw IDAT background/dye QC and GSM crosswalk are unavailable.", "limitation"))
    if analysis.get("series_accession") == "GSE54325":
        direction = meta.get("channel_direction", {})
        if direction != {"Cy3": "baseline", "Cy5": "8 weeks"}:
            findings.append(_finding("two_color_direction_declaration_missing_or_different", "Expected paired Cy5 8-week / Cy3 baseline orientation requires source/sample verification.", "error"))
        findings.append(_finding("two_color_array_count_not_biological_sample_count", "Seven arrays contain seven paired ratios; they are not seven separate before/after biological specimens.", "limitation"))
    if meta.get("cell_composition_adjusted") is True:
        findings.append(_finding("estimated_composition_sensitivity_not_direct_causal_effect", "Blood composition estimates may mediate the response and projection uncertainty is not propagated; this is a sensitivity association.", "limitation"))
    design = meta.get("design_audit", {})
    if design:
        rank, cols, people = design.get("rank"), design.get("columns"), design.get("people")
        if all(isinstance(x, int) for x in (rank, cols, people)):
            if rank != cols or people <= cols:
                findings.append(_finding("declared_design_rank_or_residual_df_invalid", "Stored design audit is rank deficient or has no residual degrees of freedom.", "error"))
            if design.get("residual_df") != people-cols:
                findings.append(_finding("declared_residual_df_inconsistent", "Stored residual_df differs from people minus columns.", "error"))
    findings.append(_finding("rds_model_matrices_not_recomputed", "This Python audit does not execute R or independently reconstruct RDS model matrices, contrasts, normalization or statistical assumptions.", "limitation"))
    return findings


def audit_analyses(records, *, roots, source_audit=None, release_id=None, expected_count=None, max_hash_bytes=64 * 1024**3):
    source_by_key = {(r.get("series_accession"), r.get("analysis_version")): r for r in (source_audit or {}).get("analyses", [])}
    plans = [(r, artifact_specs(r["value"], source_by_key.get((r["value"].get("series_accession"), r["value"].get("analysis_version"))))) for r in records]
    resolver = ArtifactResolver(roots, {s["filename"] for _, specs in plans for s in specs if s["filename"]}, max_hash_bytes)
    results = []
    for row, specs in plans:
        a = row["value"]
        verified = [resolver.verify(s) for s in specs]
        findings = []
        if not any(s["role"] == "input" for s in specs):
            findings.append(_finding("input_manifest_not_declared", "No hash-bound primary/parent input manifest is available in this analysis record."))
        if not any(s["role"] == "output" for s in specs):
            findings.append(_finding("output_manifest_not_declared", "No hash-bound output manifest is available; file/result completeness is not established."))
        text_parts = []
        for spec in verified:
            if spec["status"] == "verified" and spec["role"] in {"producer", "helper"}:
                text_parts.append(Path(spec["verified_path"]).read_text(encoding="utf-8"))
            if spec["status"] == "mismatch_or_unreadable":
                findings.append(_finding("artifact_content_mismatch", spec["role"] + ": " + spec["filename"], "error"))
        sample_tables, sample_findings = sample_audit(a, verified)
        findings.extend(sample_findings)
        findings.extend(model_findings(a, "\n".join(text_parts)))
        parents = []
        for spec in verified:
            if spec["role"] == "input" and spec["filename"] == "analysis_metadata.json" and spec["status"] == "verified":
                parent = json.loads(Path(spec["verified_path"]).read_text(encoding="utf-8"))
                parents.append({"sha256": spec["sha256"], "accession": parent.get("accession"), "analysis_version": parent.get("analysis_version"),
                                "declared_source_sha256": parent.get("script_sha256"), "n_people": parent.get("independent_people"), "n_assays": parent.get("sample_count"),
                                "validation_scope": "parent metadata bytes verified; R model not recomputed"})
        meta = a.get("metadata") or {}
        scope_incomplete = any(f["code"] in {
            "input_manifest_not_declared", "output_manifest_not_declared",
            "sample_tables_unavailable", "person_mapping_unavailable",
        } for f in findings)
        results.append({"record_id": row["record_id"], "record_sha256": hashlib.sha256(canonical(a).encode()).hexdigest(),
                        "series_accession": a.get("series_accession"), "analysis_version": a.get("analysis_version"),
                        "artifacts": verified, "artifact_status_counts": dict(Counter(s["status"] for s in verified)),
                        "sample_tables": sample_tables, "parent_metadata_lineage": parents,
                        "declared_methods": {k: meta.get(k) for k in ("design", "design_formula", "design_columns", "person_block", "block_variable", "normalization", "multiple_testing", "effect_uncertainty", "analysis_role", "channel_direction", "excluded_people", "design_audit")},
                        "findings": findings, "expert_review_status": "pending", "scientific_approval": False,
                        "machine_status": "failed" if any(f["severity"] == "error" for f in findings) else
                            "artifact_verified_with_scientific_limits" if not scope_incomplete and all(s["status"] == "verified" for s in verified) else "partial_unavailable_evidence"})
    count_ok = expected_count is None or len(results) == expected_count
    return {"schema_version": "nutriomics-analysis-input-audit-v1", "created_at": datetime.now(UTC).isoformat(),
            "release_id": release_id, "analysis_count": len(results), "expected_analysis_count": expected_count,
            "analysis_count_matches": count_ok, "expert_review_status": "pending", "scientific_approval": False,
            "journal_submission_ready": False, "analyses": results,
            "summary": {"machine_statuses": dict(Counter(r["machine_status"] for r in results)),
                        "artifact_statuses": dict(Counter(s["status"] for r in results for s in r["artifacts"])),
                        "finding_codes": dict(Counter(f["code"] for r in results for f in r["findings"])),
                        "hashed_bytes": resolver.hashed_bytes,
                        "source_producer_verified": sum(any(s["role"] == "producer" and s["status"] == "verified" for s in r["artifacts"]) for r in results)},
            "scope": "Full-byte integrity and independently counted hash-verified sample tables; no R rerun or expert biological/statistical approval",
            "status": "failed" if not count_ok or any(r["machine_status"] == "failed" for r in results) else
                "partial_unavailable_evidence" if any(r["machine_status"] == "partial_unavailable_evidence" for r in results) else "artifact_verified_with_scientific_limits"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--science-json", type=Path)
    group.add_argument("--database", type=Path)
    p.add_argument("--producer-source-audit", type=Path)
    p.add_argument("--search-root", type=Path, action="append", required=True)
    p.add_argument("--expected-analysis-count", type=int, default=14)
    p.add_argument("--max-hash-gib", type=float, default=64)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    release, records = load_science(science_json=args.science_json, database=args.database)
    source = json.loads(args.producer_source_audit.read_text(encoding="utf-8")) if args.producer_source_audit else None
    result = audit_analyses(records, roots=args.search_root, source_audit=source, release_id=release,
                            expected_count=args.expected_analysis_count, max_hash_bytes=int(args.max_hash_gib*1024**3))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": result["status"], "summary": result["summary"]}, ensure_ascii=False))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
