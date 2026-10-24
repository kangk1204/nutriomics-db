"""Launch one owned resumable CPU training job and record its PID/log."""

import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def main():
    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Only the isolated v6 development checkout may launch this job")
    import sys

    sys.path.insert(0, str(ROOT))
    from nutri_literature.extraction_v6 import source_manifest

    expected = json.loads(Path("/home/keunsoo/projects/delivery-v6.json").read_bytes())["rules_version"]
    if source_manifest()[1] != expected:
        raise ValueError(
            "Scientific files differ from the verified delivery; update the complete source archive before launching"
        )
    receipt = ROOT / "runtime/v6/training-launch.json"
    if receipt.exists():
        record = json.loads(receipt.read_bytes())
        command = Path("/proc") / str(record["pid"]) / "cmdline"
        if command.exists() and b"scripts/run_v6_training.py" in command.read_bytes():
            print(json.dumps({"already_running": True, **record}))
            return
    log = ROOT / "runtime/logs/v6-training-runner.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        process = subprocess.Popen(
            [str(ROOT / ".venv/bin/python"), "-u", "scripts/run_v6_training.py"],
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
    record = {
        "pid": process.pid,
        "log": str(log),
        "launched_at": datetime.now(timezone.utc).isoformat(),
        "default_engine": "heuristic-v1",
        "training_only": True,
    }
    receipt.write_text(json.dumps(record), encoding="utf-8")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
