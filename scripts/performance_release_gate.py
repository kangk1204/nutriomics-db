"""Gate deployment using fresh, hash-bound measurements, never prior release reports."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bounded_duration(value, maximum):
    # Missing/NaN telemetry is not evidence of a fast successful operation.
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= maximum


def gate(directory, release_id, source_manifest, cases_path):
    directory = Path(directory)
    source = json.loads(Path(source_manifest).read_text(encoding="utf-8"))
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    names = {c["id"]: c for c in cases["cases"]}
    failures = []
    encoded = json.dumps(cases["cases"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != cases["case_sha256"]:
        failures.append("frozen_cases_changed")
    reports = {}
    for name in ("performance", "data-parity", "exports", "browser", "compatibility", "tests"):
        path = directory / (name + ".json")
        if not path.is_file():
            failures.append("missing:" + name)
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        reports[name] = report
        if report.get("release_id") != release_id:
            failures.append("release:" + name)
        if report.get("source_manifest_sha256") != sha(source_manifest):
            failures.append("code:" + name)
        if report.get("case_sha256") != cases["case_sha256"]:
            failures.append("cases:" + name)
        if not report.get("verified_at") or datetime.fromisoformat(report["verified_at"]) < datetime.fromisoformat(cases["frozen_at"]):
            failures.append("stale:" + name)
    performance = reports.get("performance", {})
    if performance.get("runs_per_case") != 100:
        failures.append("warm_sample_count")
    rows = {r["id"]: r for r in performance.get("results", [])}
    if set(rows) != set(names):
        failures.append("missing_warm_cases")
    for name, c in names.items():
        row = rows.get(name, {})
        samples = row.get("samples", [])
        if len(samples) != 100 or any(s.get("status") != 200 or s.get("error") or s.get("validation_errors") or s.get("release_id") != release_id for s in samples):
            failures.append("warm_correctness:" + name)
        durations = sorted(s.get("seconds", float("inf")) for s in samples)
        if not durations or not all(math.isfinite(v) and v >= 0 for v in durations) or durations[math.ceil(.95 * len(durations))-1] > c["target_s"]:
            failures.append("warm_latency:" + name)
    concurrent = performance.get("concurrent", {})
    if set(concurrent) != set(names):
        failures.append("missing_concurrent_cases")
    for name, c in names.items():
        row = concurrent.get(name, {})
        samples = row.get("samples", [])
        if len(samples) != 100 or any(s.get("status") != 200 or s.get("error") or s.get("validation_errors") or s.get("release_id") != release_id for s in samples):
            failures.append("concurrent_correctness:" + name)
        durations = sorted(s.get("seconds", float("inf")) for s in samples)
        if not durations or not all(math.isfinite(v) and v >= 0 for v in durations) or durations[math.ceil(.95 * len(durations))-1] > (5 if c["target_s"] == 3 else 2):
            failures.append("concurrent_latency:" + name)
    parity = reports.get("data-parity", {})
    if parity.get("food_count") != 50 or parity.get("composition_count") != 1200 or parity.get("mismatches") != [] or parity.get("all_original_tables_equal") is not True or parity.get("all_frozen_responses_equal") is not True:
        failures.append("data_parity")
    exports = reports.get("exports", {})
    if set(exports.get("formats", {})) != {"html", "json", "csv"} or not all(bounded_duration(v.get("seconds"), 10) and v.get("status") == 200 for v in exports.get("formats", {}).values()) or exports.get("offline_browser_pass") is not True or exports.get("maximum_selection_verified") is not True or exports.get("validation_errors") != []:
        failures.append("exports")
    browser = reports.get("browser", {})
    if browser.get("passed") is not True or not browser.get("measurements") or any(not bounded_duration(v.get("data_to_frame_ms"), 2000) for v in browser.get("measurements", [])):
        failures.append("browser")
    if reports.get("compatibility", {}).get("passed") is not True or reports.get("tests", {}).get("returncode") != 0 or reports.get("tests", {}).get("platform") != "linux":
        failures.append("compatibility_or_tests")
    for row in source.get("files", []):
        path = (Path(source["source_directory"]) / row["path"]).resolve()
        if not path.is_relative_to(Path(source["source_directory"]).resolve()) or not path.is_file() or sha(path) != row["sha256"]:
            failures.append("source_changed:" + row["path"])
    return {"passed": not failures, "failures": failures, "release_id": release_id, "source_manifest_sha256":sha(source_manifest), "report_hashes":{name:sha(directory/(name+".json")) for name in reports}, "case_sha256":cases["case_sha256"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    args = parser.parse_args()
    result = gate(args.directory, args.release_id, args.source_manifest, args.cases)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
