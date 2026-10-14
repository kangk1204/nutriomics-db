"""Ordered server validation with durable stage receipts; no implicit deployment."""
import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json


def run(root, source, api_pid, candidate_id="nutriomics-data-20261014-candidate-v3"):
    reports = root / "research/publication/20261014/validation"
    reports.mkdir(parents=True, exist_ok=True)
    candidate = root / "data/publication/releases" / candidate_id
    state = {"status": "running", "phase": "waiting_candidate"}
    atomic_json(reports / "state.json", state)
    while not candidate.exists():
        build = json.loads((root / "research/publication/20261014/state.json").read_text())
        if build.get("status") == "failed":
            raise RuntimeError("candidate build failed")
        time.sleep(5)
    py = root / "source/.venv/bin/python"
    cases = root / "research/performance/20261010/cases.json"
    manifest_path = source / "performance-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source_directory"] = str(source)
    known = {r["path"] for r in manifest["files"]}
    for relative in ("nutrigenomics/bridge.py", "nutrigenomics/analysis_downloads.py", "nutrigenomics/download_budget.py", "tests/test_bridge_index.py", "tests/test_parity_checkpoint.py"):
        if relative not in known:
            manifest["files"].append({"path": relative})
    for row in manifest["files"]:
        row["sha256"] = sha256(source / row["path"])
        row["bytes"] = (source / row["path"]).stat().st_size
    atomic_json(manifest_path, manifest)
    common = {"release_id": "explorer-20261010-perf-v2", "source_manifest_sha256": sha256(manifest_path),
              "case_sha256": json.loads(cases.read_text())["case_sha256"]}
    env = os.environ.copy()
    env.update({"NUTRIGENOMICS_DATA_DIR": str(root / "data"), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "8"})
    def stage(name, args):
        state.update(phase=name)
        atomic_json(reports / "state.json", state)
        started = time.monotonic()
        process_env = dict(env)
        if name == "tests":
            test_data = reports / "test-data"
            (test_data / "db").mkdir(parents=True, exist_ok=True)
            process_env.update({"NUTRIGENOMICS_DATA_DIR": str(test_data),
                               "NUTRIGENOMICS_DATABASE_URL": "sqlite:///" + str(test_data / "db/test.sqlite3"),
                               "NUTRIGENOMICS_EXPLORER_DATABASE": str(test_data / "fixture/explorer.sqlite"),
                               "NUTRIGENOMICS_EXPLORER_CORE_DATABASE": str(test_data / "db/test.sqlite3")})
        with (reports / (name + ".log")).open("w") as log:
            result = subprocess.run([str(py), *args], cwd=source, env=process_env, stdout=log, stderr=subprocess.STDOUT, check=False)
        atomic_json(reports / (name + "-receipt.json"), {"returncode": result.returncode, "seconds": time.monotonic() - started})
        if result.returncode:
            raise RuntimeError(name + " failed; inspect its durable receipt and log")
        return time.monotonic() - started
    excluded_tests = [{"path": "tests/test_primary_affinity_record.py", "reason": "Private DTI capture tools are outside the NutriOmics Data Descriptor scope; separately covered in their own workflow."}]
    seconds = stage("tests", ["-m", "pytest", "-q", "--ignore=tests/test_primary_affinity_record.py", "--junitxml=" + str(reports / "tests.xml")])
    atomic_json(reports / "tests.json", {**common, "verified_at": datetime.now(UTC).isoformat(), "platform": "linux", "returncode": 0, "seconds": seconds, "excluded_test_scope": excluded_tests})
    stage("public-value-parity", ["-m", "scripts.validate_publication_values", "--release", str(candidate), "--core", str(root / "backups/publication-20261014/core-consistent.sqlite3"), "--output", str(reports / "public-value-parity.json")])
    stage("benchmark", ["-m", "scripts.benchmark_explorer_performance", "benchmark", "--base", "http://127.0.0.1:8020", "--spec", str(cases), "--output", str(reports), "--runs", "100", "--concurrent", "--server-pid", str(api_pid), "--source-manifest", str(manifest_path)])
    stage("data-parity", ["-m", "scripts.verify_explorer_performance_parity", "--parent", str(root / "data/explorer/releases/explorer-20261009-v7/explorer.sqlite"), "--candidate", str(root / "data/explorer/releases/explorer-20261010-perf-v2/explorer.sqlite"), "--baseline", str(root / "research/performance/20261010/baseline-v7"), "--measured", str(reports), "--base", "http://127.0.0.1:8020", "--cases", str(cases), "--manifest", str(manifest_path), "--output", str(reports / "data-parity.json")])
    stage("exports", ["-m", "scripts.verify_explorer_release_flows", "exports", "--base", "http://127.0.0.1:8020", "--measured", str(reports), "--cases", str(cases), "--manifest", str(manifest_path), "--output", str(reports)])
    state.update(status="server_checks_complete", phase="actual_browser_compatibility_and_human_review_pending")
    atomic_json(reports / "state.json", state)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--api-pid", type=int, required=True)
    p.add_argument("--candidate-id", default="nutriomics-data-20261014-candidate-v3")
    a = p.parse_args()
    try:
        run(a.root.resolve(), a.source.resolve(), a.api_pid, a.candidate_id)
    except Exception as exc:
        atomic_json(a.root / "research/publication/20261014/validation/state.json", {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
        raise
