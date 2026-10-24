"""Wait for this task's evidence fit, then score its independent clinical DAG."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.db import digest
    from nutri_literature.extraction_v6 import source_manifest

    parser = argparse.ArgumentParser()
    parser.add_argument("--fit-pid", type=int, required=True)
    args = parser.parse_args()
    plan = json.loads((ROOT / "runtime/v6/evidence-preparation-official-booleans.json").read_bytes())
    models = Path(plan["models"])
    expected = source_manifest()[1]
    state = ROOT / "runtime/v6/clinical-component-state.json"

    def save(status, **values):
        record = {
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
            "rules_version": expected,
            "default_engine": "heuristic-v1",
            "sealed_test_opened": False,
            "full_engine_evaluation": False,
            **values,
        }
        state.write_text(json.dumps(record))
        print(json.dumps(record), flush=True)

    save("waiting_for_owned_evidence_fit", fit_pid=args.fit_pid)
    deadline = time.monotonic() + 12 * 3600
    command = Path("/proc") / str(args.fit_pid) / "cmdline"
    while command.exists() and b"train-clinical-head" in command.read_bytes():
        if time.monotonic() > deadline:
            save("fit_wait_timed_out")
            return 1
        time.sleep(30)
    path = models / "clinical-v6-evidence/manifest.json"
    if not path.exists():
        save("evidence_fit_failed_or_incomplete")
        return 1
    record = json.loads(path.read_bytes())
    if record["training_split"] != "train" or any(
        digest((path.parent / name).read_bytes()) != sha for name, sha in record["files"].items()
    ):
        save("evidence_manifest_invalid")
        return 1
    if source_manifest()[1] != expected:
        save("scientific_source_changed")
        return 1
    save("running_clinical_component_development", training_papers=len(record["training_pmids"]))
    cmd = [
        sys.executable,
        "-u",
        str(ROOT / "scripts/evaluate_v6_clinical_component.py"),
        "--models",
        str(models),
        "--spec",
        plan["clinical_spec"],
        "--audit",
        str(Path(plan["relation"]) / "audit.json"),
        str(Path(plan["evidence"]) / "audit.json"),
    ]
    with (ROOT / "runtime/logs/v6-clinical-component-development.log").open("ab") as stream:
        result = subprocess.run(cmd, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
    save(
        "completed_clinical_component_development"
        if result.returncode == 0
        else "failed_clinical_component_development",
        returncode=result.returncode,
    )
    return result.returncode


if __name__ == "__main__":
    from nutri_literature.locking import lock

    with lock(ROOT / "runtime/locks/v6-evidence-pipeline.lock"):
        sys.exit(main())
