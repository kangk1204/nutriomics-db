"""Run the approved DNAm pathway checks after the serial primary batch."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import time
from freeze_omics_bundle import freeze
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    options = parser.parse_args()
    work = options.root.resolve()
    project = work.parents[1]
    environment = {**os.environ, "R_LIBS_USER": str(work / "r-library") + ":" + str(project / "toolchain/r-library"),
                   "OPENBLAS_NUM_THREADS": "4", "OMP_NUM_THREADS": "4", "R_THREADS": "1"}
    status = {"pid": os.getpid(), "status": "waiting_for_primary_batch", "jobs": []}

    def update() -> None:
        status["updated_utc"] = datetime.now(timezone.utc).isoformat()
        temporary = work / "postprocessing_status.json.tmp"
        temporary.write_text(json.dumps(status, indent=2))
        temporary.replace(work / "postprocessing_status.json")

    while True:
        batch = json.loads((work / "batch_status.json").read_text())
        if batch["status"] in {"completed", "completed_with_failures"}:
            break
        update()
        time.sleep(10)
    executable = project / "toolchain/r-nutrigenomics/bin/Rscript"
    # Completion is published only after the serial worker has closed all output
    # files; never use an old analysis_metadata.json as a completion signal.
    for folder in ["gse98645-acute-v1", "gse98645-chronic-v1", "gse13466-raw-v2", "gse27385-raw-v2", "gse56960-dose-v2", "gse107205-between-arm-v3", "direct-plus-idat-v1"]:
        directory = work / "outputs" / folder
        if (directory / "analysis_metadata.json").exists():
            try:
                freeze(directory)
            except Exception as error:
                status["jobs"].append({"input": str(directory), "status": "failed",
                                       "reason": "Cannot freeze completed source/output bundle: " + str(error)})
    input_directory = work / "outputs/gse107205-between-arm-v3"
    if (input_directory / "analysis_metadata.json").exists():
        item = {"input": str(input_directory), "name": "qc-pair-exclusion", "status": "running"}
        status["jobs"].append(item)
        status["status"] = "running"
        update()
        with (work / "logs/gse107205-qc-sensitivity.log").open("w") as log:
            process = subprocess.Popen([str(executable), str(work / "scripts/analyze_methylation_qc_sensitivity.R"),
                                        "--input-dir", str(input_directory)], cwd=work, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT)
            item["pid"] = process.pid
            update()
            code = process.wait()
        item.update({"returncode": code, "status": "completed" if code == 0 else "failed"})
        if code == 0:
            freeze(input_directory / "qc-pair-exclusion-v1")
        update()
    for folder in ["gse107205-between-arm-v3", "direct-plus-idat-v1"]:
        input_directory = work / "outputs" / folder
        item = {"input": str(input_directory), "status": "running"}
        status["jobs"].append(item)
        status["status"] = "running"
        update()
        if not (input_directory / "analysis_metadata.json").exists():
            item.update({"status": "excluded", "reason": "Primary analysis did not complete; no pathway result fabricated"})
            update()
            continue
        log_path = work / "logs" / (folder + "-pathways.log")
        with log_path.open("w") as log:
            process = subprocess.Popen([str(executable), str(work / "scripts/analyze_methylation_pathways.R"),
                                        "--input-dir", str(input_directory)], cwd=work, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT)
            item["pid"] = process.pid
            item["log"] = str(log_path)
            update()
            code = process.wait()
        item.update({"returncode": code, "status": "completed" if code == 0 else "failed"})
        if code == 0:
            freeze(input_directory / "pathways-v1")
        update()
    status["status"] = "completed" if all(j["status"] == "completed" for j in status["jobs"]) else "completed_with_gaps"
    update()


if __name__ == "__main__":
    main()
