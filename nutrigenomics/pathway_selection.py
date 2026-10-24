"""Explicit internal-only selection of immutable, versioned pathway corrections.

This overlay is neither an effect-table replacement nor a publication approval.
It cannot select gene/CpG results or recover the historical missing RNG state.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA = "nutriomics-internal-pathway-selection-v1"
KEY = ("series_accession", "feature_type", "feature_id", "contrast", "analysis_version")
PARENTS = {
    2: ("gse56960-rma-limma-v1-rebuild20261017-v2", "engine_contrast", 18),
    11: ("gse56960-rma-limma-dose-change-v3-rebuild20261017-v2", "analysis_family", 12),
}
SUFFIX = "-pathway-checkpoint20261023-v2"


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _hash(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("An exact SHA256 is required")
    return value


def _read_json(path: Path, expected: str) -> dict:
    if file_sha256(path) != _hash(expected):
        raise ValueError("Input artifact hash mismatch")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _path(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or name.startswith("/") or ".." in Path(name).parts):
        raise ValueError("A portable relative artifact name is required")
    result = (root / name).resolve(strict=True)
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError("Artifact escapes its immutable root")
    return result


def _pending(value: dict) -> None:
    if (value.get("scientific_approval") is not False
            or value.get("expert_review_status") != "pending"
            or value.get("historical_reproducibility_resolved") is not False
            or value.get("public_results_modified") is not False
            or value.get("new_fgsea_execution_performed") is not False):
        raise ValueError("Only the pending, unpromoted checkpoint candidate is supported")


def _bh(values: list, family_size: int) -> list:
    ordered = sorted((p, index) for index, p in enumerate(values) if p is not None)
    answer = [None] * len(values)
    running = 1.0
    for rank in range(len(ordered), 0, -1):
        p, index = ordered[rank - 1]
        running = min(running, p * family_size / rank)
        answer[index] = running
    return answer


def _same_numbers(first: list, second: list) -> bool:
    return len(first) == len(second) and all(
        (a is None and b is None) or (a is not None and b is not None
                                     and math.isclose(a, b, rel_tol=1e-13, abs_tol=1e-15))
        for a, b in zip(first, second, strict=True)
    )


def _validate_table(table: pa.Table, run: dict) -> list[str]:
    required = set(KEY) | {
        "p_value", "q_value", "q_value_contrast", "q_value_analysis_family",
        "standard_error", "effect_estimate", "leading_edge", "fgsea_log2err",
        "metadata_json", "status", "n_people", "n_samples", "pathway_size",
    }
    if not required.issubset(table.column_names) or table.num_rows != run["rows"]:
        raise ValueError("Incomplete pathway table or row count")
    data = table.to_pydict()
    keys = list(zip(*(data[name] for name in KEY), strict=True))
    if len(set(keys)) != len(keys) or any(any(v is None or v == "" for v in k) for k in keys):
        raise ValueError("Missing or duplicate five-part analysis key")
    if (set(data["feature_type"]) != {"pathway"}
            or set(data["series_accession"]) != {"GSE56960"}
            or set(data["analysis_version"]) != {run["analysis_version"]}):
        raise ValueError("Only the exact new GSE56960 pathway version may be selected")
    contrasts = sorted(set(data["contrast"]))
    if len(contrasts) != run["contrasts"]:
        raise ValueError("Contrast scope mismatch")
    for name in ("effect_estimate", "p_value", "q_value", "q_value_contrast",
                 "q_value_analysis_family", "fgsea_log2err"):
        if any(x is not None and not math.isfinite(x) for x in data[name]):
            raise ValueError("Nonfinite statistic")
        if (name.startswith("q_value") or name == "p_value") and any(
                x is not None and not 0 <= x <= 1 for x in data[name]):
            raise ValueError("P/FDR outside [0,1]")
    if any(x is not None for x in data["standard_error"]):
        raise ValueError("NES effect SE must stay missing; fgsea log2err is separate")
    if not _same_numbers(_bh(data["p_value"], table.num_rows), data["q_value_analysis_family"]):
        raise ValueError("Analysis-family BH differs")
    for contrast in contrasts:
        indexes = [i for i, value in enumerate(data["contrast"]) if value == contrast]
        p_values = [data["p_value"][i] for i in indexes]
        # fgsea's engine padj uses the nonmissing P values within each contrast.
        expected = _bh(p_values, sum(p is not None for p in p_values))
        if not _same_numbers(expected, [data["q_value_contrast"][i] for i in indexes]):
            raise ValueError("Engine within-contrast BH differs")
    family = run["primary_q_family"]
    primary = "q_value_contrast" if family == "engine_contrast" else "q_value_analysis_family"
    if data["q_value"] != data[primary]:
        raise ValueError("Primary FDR family changed")
    if "q_value_study_family" in data and (
            family != "analysis_family" or data["q_value_study_family"] != data[primary]):
        raise ValueError("Historical family alias changed meaning")
    for i, text in enumerate(data["metadata_json"]):
        metadata = json.loads(text)
        status = ("measured" if data["effect_estimate"][i] is not None
                  and data["p_value"][i] is not None else "not_estimable")
        if (metadata.get("historical_original_checkpoint_recovered") is not False
                or metadata.get("primary_q_family") != family
                or metadata.get("leading_edge") != data["leading_edge"][i]
                or data["status"][i] != status):
            raise ValueError("Row context/null status/provenance differs")
    if "null_counts" in run and run["null_counts"] != {
            name: table[name].null_count for name in table.column_names}:
        raise ValueError("Null counts differ from immutable parent manifest")
    return contrasts


def build_overlay(parquet_root: Path, parent_manifest: Path, diagnostic_receipts: dict[int, Path],
                  output: Path, *, parquet_manifest_sha256: str,
                  parent_manifest_sha256: str) -> dict:
    """Validate existing immutable files and write a new small selector, never effects."""
    root = Path(parquet_root).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(root):
        raise ValueError("Use a new overlay file outside the immutable candidate")
    pm_path = root / "manifest.json"
    packaged = _read_json(pm_path, parquet_manifest_sha256)
    parent = _read_json(parent_manifest, parent_manifest_sha256)
    if (packaged.get("schema") != "nutriomics-pathway-parquet-candidate-v1"
            or parent.get("schema") != "nutriomics-pathway-checkpoint-candidate-v1"
            or packaged.get("parent_manifest_sha256") != parent_manifest_sha256
            or parent.get("parent_release_id") != packaged.get("parent_release_id")):
        raise ValueError("Parent/candidate manifest binding differs")
    _pending(parent)
    _pending(packaged)
    p_runs = {r["analysis_id"]: r for r in parent["runs"]}
    q_runs = {r["analysis_id"]: r for r in packaged["runs"]}
    if (len(parent["runs"]) != 2 or len(packaged["runs"]) != 2
            or set(p_runs) != set(PARENTS) or set(q_runs) != set(PARENTS)
            or set(diagnostic_receipts) != set(PARENTS)):
        raise ValueError("Exactly the two registered pathway analyses/receipts are required")
    selectors, checks, hashes = [], [], [(pm_path, parquet_manifest_sha256),
                                       (Path(parent_manifest), parent_manifest_sha256)]
    membership_hashes, package_versions = set(), set()
    for aid, (old_version, family, expected_contrasts) in PARENTS.items():
        p, q = p_runs[aid], q_runs[aid]
        if (p["series_accession"] != "GSE56960" or p["parent_analysis_version"] != old_version
                or p["analysis_version"] != old_version + SUFFIX
                or q["analysis_version"] != p["analysis_version"]
                or p["parent_record_sha256"] != q["parent_record_sha256"]
                or p["primary_q_family"] != family or q["fdr_family"] != family
                or p["contrasts"] != expected_contrasts or q["contrasts"] != expected_contrasts
                or p["rows"] != q["rows"] or q["exact_parquet_roundtrip"] is not True):
            raise ValueError("Registered parent/version/contrast/FDR scope differs")
        _hash(p["parent_record_sha256"])
        artifact = _path(root, q["path"])
        if artifact.stat().st_size != q["bytes"] or file_sha256(artifact) != q["sha256"]:
            raise ValueError("Pathway Parquet hash/size mismatch")
        hashes.append((artifact, q["sha256"]))
        table = pq.read_table(artifact)
        row_run = {**q, "primary_q_family": family}
        contrasts = _validate_table(table, row_run)
        d_path = Path(diagnostic_receipts[aid])
        d_sha = p["input_artifacts"]["diagnostic_receipt"]["sha256"]
        diagnostic = _read_json(d_path, d_sha)
        hashes.append((d_path, d_sha))
        repeated = diagnostic.get("checks", {}).get("same_checkpoint_twice", {})
        if (repeated.get("exact_result_objects_identical") is not True
                or repeated.get("exact_rng_traces_identical") is not True
                or diagnostic.get("reference_exact_historical_t_available") is not False
                or diagnostic.get("scientific_approval") is not False
                or diagnostic.get("expert_review_status") != "pending"
                or diagnostic.get("upstream_sources_unchanged") is not True
                or diagnostic["raw_results_sha256"] != p["input_artifacts"]["raw_results"]["sha256"]
                or diagnostic["source_inputs"]["checkpoint"]["sha256"] != p["input_artifacts"]["checkpoint"]["sha256"]):
            raise ValueError("Two-loop checkpoint proof does not bind the parent artifacts")
        mappings = [r for r in diagnostic["upstream_sources"]["checkpoint"]
                    if r["role"] == "pathway_mapping"]
        if len(mappings) != 1:
            raise ValueError("An exact Reactome membership artifact is required")
        membership_hashes.add(_hash(mappings[0]["sha256"]))
        package_versions.add(diagnostic["environment"]["package_versions"]["reactome.db"])
        reference = {"relative_name": q["path"], "sha256": _hash(q["sha256"]),
                     "artifact_key": "sha256:" + q["sha256"], "bytes": q["bytes"]}
        selectors.append({
            "parent_analysis_id": aid, "series_accession": "GSE56960", "feature_type": "pathway",
            "parent_analysis_version": old_version, "analysis_version": q["analysis_version"],
            "parent_record_sha256": p["parent_record_sha256"], "contrasts": contrasts,
            "rows": q["rows"], "primary_q_family": family,
            "q_threshold_changes": p["q_threshold_changes"], "artifact": reference,
            "diagnostic_receipt_sha256": d_sha,
            "checkpoint_sha256": p["input_artifacts"]["checkpoint"]["sha256"],
            "raw_results_sha256": p["input_artifacts"]["raw_results"]["sha256"],
            "historical_pathway_source_sha256": p["input_artifacts"]["original_pathways"]["sha256"],
        })
        checks.append({"analysis_id": aid, "rows": table.num_rows,
                       "distinct_five_part_keys": table.num_rows, "contrasts": len(contrasts),
                       "null_counts": {n: table[n].null_count for n in table.column_names}})
    if len(membership_hashes) != 1 or len(package_versions) != 1:
        raise ValueError("The two runs do not share the declared exact pathway membership")
    if sum(s["rows"] for s in selectors) != packaged["rows"]:
        raise ValueError("Candidate row total differs")
    if any(file_sha256(path) != digest for path, digest in hashes):
        raise ValueError("An immutable input changed during validation")
    overlay = {
        "schema": SCHEMA, "parent_release_id": packaged["parent_release_id"],
        "parent_manifest_sha256": parent_manifest_sha256,
        "parquet_manifest_sha256": parquet_manifest_sha256,
        "selection_state": "internal_technical_only_pending_scientific_review",
        "scientific_approval": False, "expert_review_status": "pending", "public_allowed": False,
        "public_results_modified": False, "new_fgsea_execution_performed": False,
        "historical_reproducibility_resolved": False,
        "new_checkpoint_two_loop_reproducible": True,
        "historical_limitation": "Exact historical moderated-t/RNG checkpoint was not preserved; "
                                 "the original quantitative pathway reproduction remains unresolved.",
        "selection_limitation": "Explicit parallel new-version selection only; not final approval, "
                                "a release promotion, added participants or replacement gene/CpG rows.",
        "reactome": {"package": "reactome.db", "package_version": next(iter(package_versions)),
                     "exact_membership_artifact_sha256": next(iter(membership_hashes)),
                     "data_release": None, "data_release_status": "unknown",
                     "membership_proof": "hash-bound two-loop diagnostic receipt; not a new RDS read"},
        "rows": sum(s["rows"] for s in selectors),
        "row_count_definition": "pathway x contrast x new analysis version; parallel candidate rows",
        "q_threshold_changes": sum(s["q_threshold_changes"] for s in selectors),
        "selectors": selectors, "validation": checks,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pathway-selection-", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(overlay, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Same-directory hard-link creation is atomic and refuses an existing name.
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return overlay


class PathwaySelection:
    """Opt-in reader. Public/publication selection is intentionally unavailable."""

    def __init__(self, overlay_path: Path, artifact_root: Path, *, expected_sha256: str):
        self.overlay = _read_json(Path(overlay_path), expected_sha256)
        self.root = Path(artifact_root).resolve(strict=True)
        if (self.overlay.get("schema") != SCHEMA
                or self.overlay.get("public_allowed") is not False
                or self.overlay.get("new_checkpoint_two_loop_reproducible") is not True):
            raise ValueError("Invalid pending internal selector overlay")
        _pending(self.overlay)
        rows = self.overlay.get("selectors", [])
        if len(rows) != 2 or {r.get("parent_analysis_id") for r in rows} != set(PARENTS):
            raise ValueError("Unexpected correction selector scope")
        for row in rows:
            old, family, contrasts = PARENTS[row["parent_analysis_id"]]
            if (row.get("series_accession") != "GSE56960" or row.get("feature_type") != "pathway"
                    or row.get("parent_analysis_version") != old
                    or row.get("analysis_version") != old + SUFFIX
                    or row.get("primary_q_family") != family
                    or len(set(row.get("contrasts", []))) != contrasts
                    or len(row.get("contrasts", [])) != contrasts):
                raise ValueError("Unexpected correction version/type/contrast scope")
            artifact = row["artifact"]
            if artifact["artifact_key"] != "sha256:" + _hash(artifact["sha256"]):
                raise ValueError("Immutable artifact key differs")
            _path(self.root, artifact["relative_name"])

    def resolve(self, selector: dict, *, purpose: str) -> dict:
        names = ("series_accession", "feature_type", "contrast", "analysis_version")
        if any(not isinstance(selector.get(n), str) or not selector[n] for n in names):
            raise ValueError("An explicit series/type/contrast/version selector is required")
        requested = {name: selector[name] for name in names}
        for row in self.overlay["selectors"]:
            if (requested["series_accession"] == row["series_accession"]
                    and requested["feature_type"] == "pathway"
                    and requested["analysis_version"] in {
                        row["parent_analysis_version"], row["analysis_version"]}):
                if requested["contrast"] not in row["contrasts"]:
                    raise KeyError("Contrast is outside the exact pathway correction scope")
                if purpose != "internal_technical":
                    raise PermissionError("Pending correction cannot serve public/publication queries")
                return {"selection_status": "new_checkpoint_version_selected",
                        "requested": requested,
                        "selected": {**requested, "analysis_version": row["analysis_version"]},
                        "artifact": dict(row["artifact"]), "parent_record_sha256": row["parent_record_sha256"],
                        "expert_review_status": "pending", "public_allowed": False,
                        "historical_reproducibility_resolved": False}
        return {"selection_status": "unaffected_use_existing_reader", "requested": requested,
                "selected": requested.copy(), "artifact": None}

    def read(self, selector: dict, *, purpose: str, feature_id: str | None = None) -> pa.Table:
        resolution = self.resolve(selector, purpose=purpose)
        if resolution["artifact"] is None:
            raise ValueError("This selector must use the existing gene/CpG/other-version reader")
        artifact = resolution["artifact"]
        path = _path(self.root, artifact["relative_name"])
        if path.stat().st_size != artifact["bytes"] or file_sha256(path) != artifact["sha256"]:
            raise ValueError("Selected immutable Parquet changed")
        filters = [(name, "=", value) for name, value in resolution["selected"].items()]
        if feature_id is not None:
            filters.append(("feature_id", "=", feature_id))
        table = pq.read_table(path, filters=filters)
        if file_sha256(path) != artifact["sha256"]:
            raise ValueError("Selected immutable Parquet changed during reading")
        return table
