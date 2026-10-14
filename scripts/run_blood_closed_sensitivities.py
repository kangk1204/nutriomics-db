"""Finish serialized DNAm checks with explicit compositional closure."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from freeze_omics_bundle import freeze, sha256


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    work = parser.parse_args().root.resolve()
    project = work.parents[1]
    status = {"pid": os.getpid(), "status": "waiting_for_reference_estimation", "jobs": []}

    def update() -> None:
        status["updated_utc"] = datetime.now(timezone.utc).isoformat()
        path = work / "closed_composition_status.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(status, indent=2))
        temporary.replace(path)

    while True:
        source = work / "postprocessing_status.json"
        previous = json.loads(source.read_text())
        if previous["status"] in {"completed", "completed_with_gaps"}:
            break
        update()
        time.sleep(10)
    # Repair only publisher failures after a zero scientific-program exit.
    # Never alter the original producer-status or any completed result bytes.
    snapshots = work / "producer_status_snapshots"
    snapshots.mkdir(exist_ok=True)
    original_sha = sha256(source)
    original_copy = snapshots / (original_sha + ".json")
    if not original_copy.exists():
        original_copy.write_bytes(source.read_bytes())
    repaired = previous.copy()
    repaired["completion_repair"] = {"original_status_path": str(original_copy),
        "original_status_sha256": original_sha, "publisher_sha256": sha256(work / "scripts/freeze_omics_bundle.py"),
        "reason": "Accept scalar jsonlite result filename; preserve metadata and executed scientific source"}
    for job in repaired["jobs"]:
        if job["status"] == "failed_completion_gate" and job.get("returncode") == 0:
            output = "pathways-v1" if job["name"].endswith("-pathways") else None
            if output:
                freeze(Path(job["input"]) / output)
                job["status"] = "completed"
                job["completion_repair_utc"] = datetime.now(timezone.utc).isoformat()
    repaired["status"] = "completed" if all(j["status"] == "completed" for j in repaired["jobs"]) else "completed_with_gaps"
    path = work / "postprocessing_repaired_status.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(repaired, indent=2))
    temporary.replace(path)
    environment = {**os.environ, "R_LIBS_USER": str(work / "r-library") + ":" + str(project / "toolchain/r-library"),
                   "OPENBLAS_NUM_THREADS": "4", "OMP_NUM_THREADS": "4", "R_THREADS": "1"}
    for folder in ["gse107205-between-arm-v4", "direct-plus-idat-v1"]:
        input_directory = work / "outputs" / folder
        name = folder + "-closed-composition-v2"
        item = {"name": name, "status": "running", "started_utc": datetime.now(timezone.utc).isoformat()}
        status["jobs"].append(item)
        status["status"] = "running"
        update()
        if not (input_directory / "blood-composition-v1/DONE.json").exists():
            item.update({"status": "excluded", "reason": "Reference estimates did not pass completion gate"})
            update()
            continue
        script = work / "scripts/analyze_blood_composition_closed_sensitivity.R"
        source_sha = sha256(script)
        snapshot = work / "executed_sources" / name / source_sha
        snapshot.mkdir(parents=True, exist_ok=True)
        for file in [script, work / "scripts/bridge_omics_helpers.R"]:
            shutil.copyfile(file, snapshot / file.name)
        item.update({"script_sha256": source_sha, "source_snapshot": str(snapshot)})
        log_path = work / "logs" / (name + ".log")
        with log_path.open("w") as log:
            process = subprocess.Popen([str(project / "toolchain/r-nutrigenomics/bin/Rscript"), str(script),
                                       "--input-dir", str(input_directory)], env=environment, cwd=work,
                                       stdout=log, stderr=subprocess.STDOUT)
            item.update({"pid": process.pid, "log": str(log_path)})
            update()
            code = process.wait()
        item.update({"returncode": code, "status": "completed" if code == 0 else "failed",
                     "finished_utc": datetime.now(timezone.utc).isoformat()})
        if code == 0:
            try:
                assert sha256(script) == source_sha, "Source mutated while job active"
                freeze(input_directory / "blood-composition-closed-v2")
            except Exception as error:
                item.update({"status": "failed_completion_gate", "reason": str(error)})
        update()
    status["status"] = "completed" if all(j["status"] == "completed" for j in status["jobs"]) else "completed_with_gaps"
    update()


if __name__ == "__main__":
    main()
