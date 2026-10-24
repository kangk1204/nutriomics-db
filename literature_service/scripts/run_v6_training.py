"""Resume isolated Linux v6 training; keep every legacy artifact and default v1."""

import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = Path("/home/keunsoo/projects/nutriomics-literature-v6-development")
LEGACY = Path("/home/keunsoo/projects/nutriomics-literature-v5-development/runtime")


def main():
    if os.name != "posix" or ROOT.resolve() != EXPECTED:
        raise ValueError("This runner is restricted to the isolated v6 Linux development directory")
    home = ROOT / "runtime"
    preparation = json.loads((home / "v6/data-preparation.json").read_bytes())
    audit_dir = ROOT / Path(preparation["audited_directory"].replace("\\", "/"))
    audit = audit_dir / "audit.json"
    models = home / "v6/models"
    models.mkdir(parents=True, exist_ok=True)
    for name in ("gene", "genes", "gene-snapshot.json"):
        destination = models / name
        if not destination.exists():
            destination.symlink_to(LEGACY / "models" / name, target_is_directory=name in {"gene", "genes"})
    if not (models / "learned-v5").exists():
        (models / "learned-v5").symlink_to(LEGACY / "models/learned-profiles", target_is_directory=True)
    sources = home / "v6/clinical-base-sources"
    sources.mkdir(parents=True, exist_ok=True)
    for name in ("rct-train.txt", "rct-dev.txt", "rct-test.txt", "ebm_nlp_2_00.tar.gz"):
        if not (sources / name).exists():
            (sources / name).symlink_to(LEGACY / "sources" / name)
    env = {
        **os.environ,
        "LITERATURE_HOME": str(home),
        "LITERATURE_V6_MODEL_HOME": str(models),
        "LITERATURE_NLP_LOCK": "/home/keunsoo/projects/nutriomics-literature/runtime/locks/nlp-cpu.lock",
        "LITERATURE_PROCESSING_LOCK_HOME": "/home/keunsoo/projects/nutriomics-literature/runtime/locks",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }
    prefix = [
        str(ROOT / ".venv/bin/python"),
        "-u",
        "-m",
        "nutri_literature.cli_v6",
        "--home",
        str(home),
        "--models",
        str(models),
    ]
    stages = [
        (
            "clinical_base",
            models / "clinical/manifest.json",
            [
                "train-clinical-base",
                "--sources",
                str(sources),
                "--benchmarks",
                str(home / "public_benchmarks"),
                "--audit",
                str(audit),
                "--out",
                str(models / "clinical"),
            ],
        ),
        (
            "relation_features",
            home / "v6/features/manifest.json",
            [
                "prepare-features",
                "--data",
                str(audit_dir / "relation-train.json"),
                "--audit",
                str(audit),
                "--out",
                str(home / "v6/features"),
            ],
        ),
        (
            "relation_heads",
            models / "learned-v6/manifest.json",
            [
                "train",
                "--features",
                str(home / "v6/features"),
                "--audit",
                str(audit),
                "--out",
                str(models / "learned-v6"),
            ],
        ),
    ]
    state = home / "v6/training-state.json"
    logs = home / "logs/v6-training"
    logs.mkdir(parents=True, exist_ok=True)

    def save(stage, status, **extra):
        value = {
            "stage": stage,
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
            "default_engine": "heuristic-v1",
            "scientific_accuracy_measured": False,
            "sealed_final_opened": False,
            **extra,
        }
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        temporary.replace(state)
        print(json.dumps(value), flush=True)

    for name, complete, args in stages:
        if complete.exists():
            save(name, "already_present")
            continue
        save(name, "running")
        with (logs / (name + ".log")).open("a", encoding="utf-8") as log:
            result = subprocess.run(prefix + args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode or not complete.exists():
            save(name, "failed", exit_code=result.returncode, log=str(logs / (name + ".log")))
            return 1
        save(name, "completed")
    save(
        "relation_training",
        "completed",
        clinical_heads="not trained by this runner",
        development_evaluation="pending",
    )
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    from nutri_literature.locking import lock

    with lock(ROOT / "runtime/locks/v6-training-runner.lock"):
        sys.exit(main())
