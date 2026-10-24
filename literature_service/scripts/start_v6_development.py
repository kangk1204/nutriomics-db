"""Launch the owned dependency-aware development batch with a PID receipt."""

import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def main():
    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Only the isolated v6 checkout may launch development evaluation")
    receipt = ROOT / "runtime/v6/development-launch.json"
    if receipt.exists():
        record = json.loads(receipt.read_bytes())
        command = Path("/proc") / str(record["pid"]) / "cmdline"
        if command.exists() and b"scripts/run_v6_development.py" in command.read_bytes():
            print(json.dumps({"already_running": True, **record}))
            return
    log = ROOT / "runtime/logs/v6-development-runner.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        process = subprocess.Popen(
            [sys.executable, "-u", "scripts/run_v6_development.py"],
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=stream,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    record = {
        "pid": process.pid,
        "launched_at": datetime.now(timezone.utc).isoformat(),
        "log": str(log),
        "development_only": True,
        "sealed_tests": False,
    }
    receipt.write_text(json.dumps(record), encoding="utf-8")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
