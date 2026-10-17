"""Audit all permitted producer runs and freeze exact replay prerequisites.

No original input/output is modified. Missing historical code/environment cannot
be called an exact reproduction. This command prepares work, never approves it.
"""
import argparse
import json
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.publication import record_hash
from nutrigenomics.source_updates import atomic_json


def parsed(value, default):
    return json.loads(value) if isinstance(value, str) else value if value is not None else default


def prepare(audit_path, policy_path, project_root, output):
    root, output = Path(project_root).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("replay package already exists")
    if not output.is_relative_to(root):
        raise ValueError("replay output must stay inside the approved project")
    data = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    permissions = {(r["series_accession"], r["analysis_version"]): r
                   for r in json.loads(Path(policy_path).read_text(encoding="utf-8"))["analyses"]
                   if r.get("export_allowed") is True}
    output.mkdir(parents=True)
    runs = []
    for run in data["runs"]:
        key = (run["series_accession"], run["analysis_version"])
        if key not in permissions:
            continue
        folder = output / record_hash(list(key))[:16]
        folder.mkdir()
        original_metadata = parsed(run.get("metadata_json"), {})
        producers = parsed(run.get("output_manifest_json"), [])
        code = []
        for role in ("source_script", "environment_lock"):
            expected = run.get(role + "_sha256")
            candidates = [Path(run.get(role + "_path") or "__absent__")]
            candidates += [Path(r["path"]) for r in producers if r.get("sha256") == expected]
            recovered = next((p.resolve() for p in candidates if p.is_file()
                              and p.resolve().is_relative_to(root) and sha256(p) == expected), None)
            if recovered:
                target = folder / (role + recovered.suffix)
                target.write_bytes(recovered.read_bytes())
            code.append({"role": role, "expected_sha256": expected,
                         "exact_copy": str(target.relative_to(output)) if recovered else None,
                         "status": "exact_historical_copy" if recovered else "historical_bytes_unresolved"})
        inputs = parsed(run.get("input_manifest_json"), None) or parsed(run.get("input_manifest"), None) or original_metadata.get("input_manifest", [])
        input_rows = []
        for entry in inputs:
            name = entry.get("path") or entry.get("input_file")
            path = Path(name) if name else None
            present = bool(path and path.is_absolute() and path.resolve().is_relative_to(root) and path.is_file())
            input_rows.append({"path": str(path.relative_to(root)) if path and path.is_absolute() and path.is_relative_to(root) else name,
                               "expected_sha256": entry.get("sha256", entry.get("input_file_sha256")),
                               "exists": present, "bytes": path.stat().st_size if present else None,
                               "hash_verified_this_run": False})
        issues = [r["role"] + "_historical_bytes_unresolved" for r in code if not r["exact_copy"]]
        if not input_rows or any(not r["exists"] for r in input_rows):
            issues.append("input_path_resolution_required")
        record = {"series_accession": key[0], "analysis_version": key[1], "code": code,
                  "inputs": input_rows, "declared_metadata": original_metadata,
                  "status": "preflight_needs_resolution" if issues else "ready_for_input_hash_verification",
                  "issues": issues, "executed": False, "comparison_passed": False,
                  "exact_reproduction_claim_allowed": False,
                  "migration_policy": "A changed recipe or environment requires a new analysis version; never overwrite this producer."}
        atomic_json(folder / "preflight.json", record)
        runs.append(record)
    result = {"schema_version": 1, "scope": "all permitted analysis runs",
              "analysis_count": len(runs), "runs": runs, "executed_count": 0,
              "max_threads": 16, "concurrent_heavy_jobs": 1,
              "scientific_review": "pending", "clean_reproduction_completed": False}
    atomic_json(output / "replay-manifest.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("audit", "policy", "project-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.audit, args.policy, args.project_root, args.output)
    print(json.dumps({"analyses": report["analysis_count"], "executed": 0,
                      "needs_resolution": sum(bool(r["issues"]) for r in report["runs"])}))
