"""Detached task-owned sequential training/evaluation; no default-engine changes."""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    base = ROOT / "runtime/v6/refinement-r2"
    base.mkdir(parents=True, exist_ok=True)
    train = ROOT / "runtime/v6/training-data/recovery/inference-official-booleans/train-audited"
    role_manifest = ROOT / "runtime/v6/refinement-r1/clinical-roles/manifest.json"
    commands = [
        (
            "training",
            [
                sys.executable,
                "-u",
                str(ROOT / "scripts/train_v6_refinement.py"),
                "--task",
                "boundary",
                "--data",
                str(train / "relation-train.json"),
                "--audit",
                str(train / "audit.json"),
                "--output",
                str(base / "boundary"),
                "--upstream-role-manifest",
                str(role_manifest),
            ],
        ),
        (
            "evaluation",
            [
                sys.executable,
                "-u",
                str(ROOT / "scripts/evaluate_v6_clinical_boundary.py"),
                "--models",
                str(ROOT / "runtime/v6/refinement-r1/frozen-models"),
                "--roles",
                str(role_manifest.parent),
                "--boundary",
                str(base / "boundary"),
                "--spec",
                str(train.parent / "development-confirmed/spec.json"),
                "--baseline",
                str(ROOT / "runtime/v6/validation/clinical-baseline-for-r1.json"),
                "--output",
                str(base / "development"),
            ],
        ),
    ]
    (base / "commands.json").write_text(json.dumps(commands))
    for stage, command in commands:
        (base / "state.json").write_text(json.dumps({"status": stage, "command": command}))
        with (ROOT / ("runtime/logs/v6-r2-" + stage + ".log")).open("ab") as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            (base / "state.json").write_text(
                json.dumps({"status": "failed", "stage": stage, "returncode": result.returncode})
            )
            return result.returncode
    (base / "state.json").write_text(
        json.dumps(
            {"status": "completed_development", "default_engine": "heuristic-v1", "sealed_test_opened": False}
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
