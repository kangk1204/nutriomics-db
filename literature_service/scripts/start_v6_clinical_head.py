"""Launch one audited clinical head in the isolated CPU checkout."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("outcome", "evidence"), required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Only the isolated Linux v6 checkout may launch this job")
    from nutri_literature.db import digest
    from nutri_literature.extraction_v6 import source_manifest
    from nutri_literature.features_v6 import source_hashes

    audit = json.loads(args.audit.read_bytes())
    if not audit.get("passed") or audit.get("audited_training_sha256") != digest(args.data.read_bytes()):
        raise ValueError("Clinical training requires its exact passed audit")
    home, models = ROOT / "runtime", ROOT / "runtime/v6/models"
    out = models / ("clinical-v6-" + args.kind)
    if (out / "manifest.json").exists():
        artifact = json.loads((out / "manifest.json").read_bytes())
        if (artifact["feature_source_hashes"] != source_hashes("clinical")
                or artifact["audit_sha256"] != digest(args.audit.read_bytes())):
            raise ValueError("Completed clinical artifact differs; use a new model root")
        print(json.dumps({"already_completed": True, "out": str(out)}))
        return
    receipt = home / ("v6/clinical-" + args.kind + "-launch.json")
    if receipt.exists():
        record = json.loads(receipt.read_bytes())
        command = Path("/proc") / str(record["pid"]) / "cmdline"
        if command.exists() and b"train-clinical-head" in command.read_bytes():
            print(json.dumps({"already_running": True, **record}))
            return
    log = home / ("logs/v6-clinical-" + args.kind + ".log")
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "LITERATURE_PROCESSING_LOCK_HOME": str(home / "locks"),
           "LITERATURE_NLP_LOCK": str(home / "locks/nlp-cpu.lock")}
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[name] = "1"
    cmd = [sys.executable, "-u", "-m", "nutri_literature.cli_v6", "--home", str(home),
           "--models", str(models), "train-clinical-head", "--kind", args.kind,
           "--data", str(args.data), "--audit", str(args.audit), "--out", str(out)]
    with log.open("ab") as stream:
        job = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    record = {"pid": job.pid, "kind": args.kind, "log": str(log),
              "at": datetime.now(timezone.utc).isoformat(), "rules_version": source_manifest()[1],
              "audit_sha256": digest(args.audit.read_bytes()),
              "default_engine": "heuristic-v1", "sealed_tests_opened": False}
    receipt.write_text(json.dumps(record), encoding="utf-8")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
