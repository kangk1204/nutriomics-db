"""Serial permanent-server execution with job logs and durable status snapshots."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    options = parser.parse_args()
    work = options.root.resolve()
    project = work.parents[1]
    executable = project / "toolchain/r-nutrigenomics/bin/Rscript"
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    environment = {**os.environ, "R_LIBS_USER": str(work / "r-library") + ":" + str(project / "toolchain/r-library"),
                   "NUTRIGENOMICS_R_LIB": str(project / "toolchain/r-library"),
                   "OPENBLAS_NUM_THREADS": "4", "OMP_NUM_THREADS": "4", "R_THREADS": "1"}
    jobs = [
        ("gse98645-paired-camera", "analyze_gse98645.R", ["--root", str(work)]),
        ("gse13466-raw", "analyze_gse13466.R", ["--root", str(work), "--raw-cdf", "true", "--output-dir", str(work / "outputs/gse13466-raw-v2")]),
        ("gse27385-raw", "analyze_gse27385.R", ["--root", str(work), "--raw-cel", "true", "--output-dir", str(work / "outputs/gse27385-raw-v2")]),
        ("gse56960-dose", "analyze_gse56960.R", ["--data-dir", str(project / "data"), "--output-dir", str(work / "outputs/gse56960-dose-v2"), "--bridge-contrasts", "true"]),
        ("gse107205-between-arms", "analyze_methylation_geo.R", ["--accession", "GSE107205", "--data-dir", str(project / "data"), "--output-dir", str(work / "outputs/gse107205-between-arm-v3"), "--bridge-contrasts", "true"]),
        ("direct-plus", "analyze_direct_plus.R", ["--root", str(work)]),
    ]
    status = {"pid": os.getpid(), "project": str(project), "work": str(work), "jobs": []}

    def update() -> None:
        status["updated_utc"] = datetime.now(timezone.utc).isoformat()
        temporary = work / "batch_status.json.tmp"
        temporary.write_text(json.dumps(status, indent=2))
        temporary.replace(work / "batch_status.json")

    # Earlier interactive staged analyses must finish before this serial queue.
    while True:
        active = []
        for proc in Path("/proc").iterdir():
            if proc.name.isdigit() and int(proc.name) != os.getpid():
                try:
                    command = (proc / "cmdline").read_bytes().decode(errors="replace")
                    if str(work / "scripts/analyze_") in command:
                        active.append(int(proc.name))
                except OSError:
                    pass
        if not active:
            break
        status.update({"status": "waiting_for_existing_staged_analysis", "existing_pids": active})
        update()
        time.sleep(5)
    for name, script, arguments in jobs:
        log_path = logs / (name + ".log")
        item = {"name": name, "script": script, "arguments": arguments, "status": "running",
                "started_utc": datetime.now(timezone.utc).isoformat(), "log": str(log_path)}
        status["jobs"].append(item)
        status.update({"status": "running", "current_job": name})
        update()
        with log_path.open("w") as log:
            process = subprocess.Popen([str(executable), str(work / "scripts" / script)] + arguments,
                                       cwd=work, env=environment, stdout=log, stderr=subprocess.STDOUT)
            item["pid"] = process.pid
            update()
            code = process.wait()
        item.update({"returncode": code, "status": "completed" if code == 0 else "failed",
                     "finished_utc": datetime.now(timezone.utc).isoformat()})
        update()
    status["status"] = "completed" if all(j["returncode"] == 0 for j in status["jobs"]) else "completed_with_failures"
    status.pop("current_job", None)
    update()


if __name__ == "__main__":
    main()
