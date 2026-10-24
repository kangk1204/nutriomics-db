"""Prepare a hash-bound, read-only replay plan. Never copy inputs or execute R."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

RUNS = {
    "gse127530-v1": ("GSE127530", "producer_replay", "analyze_gse127530.R"),
    "gse56960-v1": ("GSE56960", "producer_replay", "analyze_gse56960.R"),
    "gse56960-dose-v3": ("GSE56960", "producer_replay", "analyze_gse56960.R"),
    "gse54325-v1": ("GSE54325", "producer_replay", "analyze_gse54325.R"),
    "gse13466-raw-v2": ("GSE13466", "saved_normalized_model_replay", "analyze_gse13466.R"),
    "gse27385-raw-v2": ("GSE27385", "saved_normalized_model_replay", "analyze_gse27385.R"),
}
REFERENCE_NAMES = {
    "analysis_metadata.json", "DONE.json", "gene_effects.tsv.gz", "pathway_effects.tsv.gz",
    "camera_sensitivity.tsv.gz", "sample_metadata.tsv", "sample_qc.tsv",
    "reactome_membership.tsv", "reactome_snapshot.json", "feature_crosswalk.tsv",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_posix(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("Expected a traversal-free absolute server path")
    return path


def build_plan(inventory: dict, small_inputs: dict, destination: str, code_root: Path) -> dict:
    source = safe_posix(inventory["source_root"])
    target = safe_posix(destination)
    project = source.parent.parent
    if (not target.is_relative_to(project) or target.is_relative_to(source)
            or source.is_relative_to(target)):
        raise ValueError("Destination must be a separate permanent directory within the approved project")
    files = {}
    for directory in inventory.get("directories", []):
        for entry in directory.get("children", []):
            files[entry["path"]] = entry
    for entry in small_inputs.get("files", []):
        files[entry["path"]] = {**files.get(entry["path"], {}), **entry}
    for run in inventory["runs"]:
        for entry in run.get("input_stats", []) + run.get("source_snapshot", []) + run.get("output_stats", []):
            files[entry["path"]] = {**files.get(entry["path"], {}), **entry}
        for kind in ("metadata_file", "done_file"):
            entry = run[kind]; files[entry["path"]] = entry

    copies, runs, issues = {}, [], []

    def add(src: str, dst: PurePosixPath, expected: str, role: str, authority: str, size=None):
        src_path = safe_posix(src)
        if not src_path.is_relative_to(source) or not dst.is_relative_to(target):
            raise ValueError("Copy source/destination escapes its approved root")
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Every planned copy requires an authoritative full SHA256")
        current = files.get(src, {})
        if current.get("exists") is not True:
            issues.append({"path": src, "reason": "source_missing_or_not_in_bounded_inventory"})
        if current.get("sha256") and current["sha256"] != expected:
            raise ValueError("Fresh small-file SHA differs from declared authority: " + src)
        if size is not None and current.get("bytes") is not None and size != current["bytes"]:
            raise ValueError("Source byte size differs from declaration: " + src)
        record = {"source": src, "destination": str(dst), "sha256": expected,
                  "bytes": current.get("bytes", size), "role": role, "hash_authority": authority,
                  "source_hash_checked_in_bounded_inventory": current.get("sha256") == expected,
                  "copy_and_destination_hash_verification_required": True}
        key = str(dst)
        if key in copies and any(copies[key][k] != record[k] for k in ("source", "sha256")):
            raise ValueError("Conflicting destination hashes")
        copies[key] = record
        return str(dst)

    by_name = {PurePosixPath(run["folder"]).name: run for run in inventory["runs"]}
    if set(by_name) != set(RUNS):
        raise ValueError("Exactly the six selected historical analysis runs are required")
    for name, (study, route, producer) in RUNS.items():
        run = by_name[name]; folder = source / "outputs" / name
        if run["folder"] != str(folder):
            raise ValueError("Historical run folder mismatch")
        metadata = run["metadata"]
        if metadata["accession"] != study:
            raise ValueError("Historical study identity mismatch")
        done = {row["path"]: row for row in run["done"]["files"]}
        if done["analysis_metadata.json"]["sha256"] != run["metadata_file"]["sha256"]:
            raise ValueError("Metadata is not bound to its immutable completion receipt")
        reference = target / "reference" / name
        required_names = set(REFERENCE_NAMES)
        if route == "saved_normalized_model_replay":
            required_names.add("reanalysis_inputs.rds")
        for filename, entry in done.items():
            if filename in required_names or filename.startswith("source_snapshot/"):
                # DONE contains actual output hashes; no unsupervised human-approval receipt is copied.
                add(str(folder / filename), reference / filename, entry["sha256"],
                    "historical_reference", str(folder / "DONE.json") + "#files:" + filename,
                    entry.get("bytes"))
        add(str(folder / "DONE.json"), reference / "DONE.json", run["done_file"]["sha256"],
            "historical_completion_receipt", "fresh bounded metadata SHA")
        snapshot = next((s for s in run["source_snapshot"]
                         if PurePosixPath(s["path"]).name == producer), None)
        if not snapshot or snapshot["sha256"] != metadata["script_sha256"]:
            raise ValueError("Exact original producer source is unresolved")
        version = metadata["analysis_version"] + "-archive20261022-v1"
        output = target / ("outputs" if route == "producer_replay" else "archives") / name
        input_records, aliases = [], []
        if route == "producer_replay":
            declared_inputs = metadata.get("input_manifest", [])
            if study == "GSE56960" and sum(r["path"].lower().endswith(".cel.gz") for r in declared_inputs) != 168:
                raise ValueError("Exactly 168 declared CEL inputs are required")
            if study == "GSE54325" and sum(bool(re.search(r"GSM\d+_Volunteer\d+\.txt\.gz$", r["path"])) for r in declared_inputs) != 7:
                raise ValueError("Exactly seven declared two-colour arrays are required")
            for entry in declared_inputs:
                src = safe_posix(entry["path"])
                if not src.is_relative_to(source):
                    candidate = source / "toolchain" / src.name
                    alternate = files.get(str(candidate), {})
                    if (src.parent != project / "toolchain"
                            or alternate.get("sha256") != entry["sha256"]):
                        raise ValueError("External original input has no byte-verified immutable alias")
                    aliases.append({"declared_path": str(src), "selected_path": str(candidate),
                                    "sha256": entry["sha256"], "resolution": "exact_full_hash"})
                    src = candidate
                relative = src.relative_to(source)
                dst = target / relative
                if relative.parts[:2] == ("outputs", "geo-design-audit"):
                    dst = target / "reference" / relative
                input_records.append(add(str(src), dst, entry["sha256"], "producer_input",
                    str(folder / "analysis_metadata.json") + "#input_manifest", entry.get("bytes")))
            command = ["Rscript", "--vanilla", "scripts/" + producer,
                       "--data-dir", str(target / "data"), "--output-dir", str(output),
                       "--analysis-version", version]
            if study in {"GSE56960", "GSE54325"}:
                command += ["--sample-sheet", str(target / "reference" / "outputs" / "geo-design-audit" / study / "sample_design.tsv")]
            if name == "gse56960-dose-v3":
                command += ["--bridge-contrasts", "true"]
            if study == "GSE127530":
                for entry in small_inputs["files"]:
                    src = safe_posix(entry["path"])
                    if str(source / "data" / "raw" / "geo") + "/" in str(src):
                        add(str(src), target / src.relative_to(source), entry["sha256"],
                            "count_source_and_audit_prerequisite", "fresh bounded source SHA", entry["bytes"])
                audit = files[str(folder / "gse127530_counts_audit.json")]
                audit_content = audit.get("content", {})
                if (audit_content.get("status") not in {"identical_payloads", "verified_fixed_cleanup"}
                        or audit_content.get("sample_design", {}).get("geo_sample_crosswalk_status") != "complete"
                        or audit_content.get("selected_analysis_file") != "GSE127530_fixed_combinedCounts.txt.gz"):
                    raise ValueError("Count cleanup and GSM mapping are not verified")
                count_records = {r["name"]: r for r in audit_content.get("files", [])}
                for filename in ("GSE127530_fixed_combinedCounts.txt.gz", "GSE127530_combinedCounts.txt.gz"):
                    current = files.get(str(source / "data" / "raw" / "geo" / study / filename), {})
                    if current.get("sha256") != count_records.get(filename, {}).get("compressed_sha256"):
                        raise ValueError("Fresh count bytes do not match the frozen cleanup audit")
                add(audit["path"], output / "gse127530_counts_audit.json", audit["sha256"],
                    "count_audit_precondition", "fresh bounded count audit SHA", audit["bytes"])
                for entry in small_inputs["files"]:
                    if entry["path"].startswith(str(source / "toolchain") + "/"):
                        add(entry["path"], target / safe_posix(entry["path"]).relative_to(source),
                            entry["sha256"], "historical_environment_lock", "fresh bounded source SHA", entry["bytes"])
        else:
            rds = done["reanalysis_inputs.rds"]; gene = done["gene_effects.tsv.gz"]
            lock = files.get(str(source / "toolchain" / "r-environment-linux-64.lock.txt"))
            if not lock or not lock.get("sha256"):
                raise ValueError("Exact project environment lock is required")
            command = ["Rscript", "--vanilla", "scripts/replay_saved_expression_archive.R",
                "--source-rds", str(reference / "reanalysis_inputs.rds"), "--expected-sha256", rds["sha256"],
                "--parent-metadata", str(reference / "analysis_metadata.json"),
                "--expected-parent-sha256", done["analysis_metadata.json"]["sha256"],
                "--reference-gene-effects", str(reference / "gene_effects.tsv.gz"),
                "--expected-reference-sha256", gene["sha256"], "--q-scope", "study_joint",
                "--environment-lock", str(target / "toolchain" / "r-environment-linux-64.lock.txt"),
                "--expected-environment-sha256", lock["sha256"],
                "--output-dir", str(output), "--archive-version", version]
        runs.append({"study": study, "parent_analysis_version": metadata["analysis_version"],
            "proposed_archive_version": version, "route": route, "cwd": str(target),
            "command_argv": command, "input_destinations": input_records, "input_path_aliases": aliases,
            "historical_source_sha256": metadata["script_sha256"],
            "reference_output": str(reference), "new_output": str(output),
            "original_input_manifest": metadata.get("input_manifest", []),
            "preprocessing_repeated": route == "producer_replay",
            "scientific_approval": False, "expert_review_status": "pending", "executed": False,
            "limits": (["Do not substitute the depositor series matrix for stored raw-RMA expression",
                        "Gene numeric fit replay only; no independent raw normalization or pathway regeneration claimed"]
                       if route == "saved_normalized_model_replay" else
                       ["Preserve all original normalization, block/correlation, channel and FDR rules",
                        "Archive-only instrumentation still requires actual full-table parity after execution"])})
    code = []
    for name in ("analyze_gse127530.R", "analyze_gse56960.R", "analyze_gse54325.R",
                 "expression_archive_helpers.R", "replay_saved_expression_archive.R"):
        path = code_root / "scripts" / name
        code.append({"source": str(path.resolve()), "destination": str(target / "scripts" / name),
                     "sha256": sha256(path), "bytes": path.stat().st_size, "role": "new_archive_code"})
    for run in runs:
        run["command_argv"][0] = str(project / "toolchain" / "r-nutrigenomics" / "bin" / "Rscript")
        run["environment"] = {"R_LIBS_USER": str(project / "toolchain" / "r-library"),
                              "NUTRIGENOMICS_R_LIB": str(project / "toolchain" / "r-library"),
                              "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "R_THREADS": "1"}
    return {"schema_version": "expression-archive-replay-plan-v1", "source_root": str(source),
            "destination_root": str(target), "study_count": 5, "analysis_run_count": 6,
            "copy_manifest": list(copies.values()), "workspace_code_uploads": code,
            "planned_unique_source_copy_bytes": sum(r["bytes"] or 0 for r in copies.values()),
            "runs": runs, "unresolved_inputs": issues, "executed": False,
            "approval_from_this_plan": False, "scientific_approval": False, "expert_review_status": "pending",
            "execution_order": "After DIRECT PLUS and all other heavy R jobs; one run at a time",
            "resource_contract": {"threads": 1, "max_concurrent_heavy_jobs": 1,
                "memory_reserve_gib": 24, "process_group_monitor_required": True},
            "forbidden": ["recursive raw tree copy", "R execution by this preparer", "sealed evaluation access",
                          "overwrite immutable historical data", "transfer historical approval", "change original statistics"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("inventory", "small-inputs", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--destination-root", required=True)
    parser.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError("Use a new plan output directory")
    plan = build_plan(json.loads(args.inventory.read_text(encoding="utf-8-sig")),
                      json.loads(args.small_inputs.read_text(encoding="utf-8-sig")),
                      args.destination_root, args.code_root)
    plan["control_inputs"] = [{"path": str(p.resolve()), "sha256": sha256(p)}
                              for p in (args.inventory, args.small_inputs)]
    args.output_dir.mkdir(parents=True)
    (args.output_dir / "replay-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"runs": len(plan["runs"]), "files": len(plan["copy_manifest"]),
                      "planned_bytes": plan["planned_unique_source_copy_bytes"],
                      "unresolved_inputs": len(plan["unresolved_inputs"]), "executed": False}))


if __name__ == "__main__":
    main()
