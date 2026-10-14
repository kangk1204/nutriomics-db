"""Run approved DNAm pathway, region and composition checks serially."""
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
    jobs = []
    for folder in ["gse107205-between-arm-v4", "direct-plus-idat-v1"]:
        for name, filename, output in [
            ("pathways", "analyze_methylation_pathways.R", "pathways-v1"),
            ("fixed-regions", "analyze_methylation_regions.R", "regions-fixed1000bp-v1"),
            ("dmrcate", "analyze_dmrcate_sensitivity.R", "dmrcate-sensitivity-v1"),
            ("blood-composition", "analyze_blood_composition_sensitivity.R", "blood-composition-v1"),
        ]:
            jobs.append((folder, name, filename, output))
    for folder, name, filename, output in jobs:
        input_directory = work / "outputs" / folder
        item = {"input": str(input_directory), "name": folder + "-" + name, "status": "running"}
        status["jobs"].append(item)
        status["status"] = "running"
        update()
        if not (input_directory / "DONE.json").exists():
            item.update({"status": "excluded", "reason": "Primary analysis did not pass completion gate; no result fabricated"})
            update()
            continue
        if name == "blood-composition":
            available = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
            budget = (64 if folder.startswith("direct") else 12) * 1024 ** 3
            item["memory_budget_bytes"] = budget
            item["mem_available_bytes"] = available
            if available < budget:
                item.update({"status": "excluded", "reason": "Available memory below explicit reference-cell-estimation budget"})
                update()
                continue
        script = work / "scripts" / filename
        source_sha = sha256(script)
        snapshot = work / "executed_sources" / item["name"] / source_sha
        snapshot.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(script, snapshot / filename)
        helper = work / "scripts/bridge_omics_helpers.R"
        shutil.copyfile(helper, snapshot / helper.name)
        item["source_snapshot"] = str(snapshot)
        item["script_sha256"] = source_sha
        log_path = work / "logs" / (item["name"] + ".log")
        with log_path.open("w") as log:
            process = subprocess.Popen([str(project / "toolchain/r-nutrigenomics/bin/Rscript"), str(script),
                                        "--input-dir", str(input_directory), "--root", str(work)], cwd=work, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT)
            item["pid"] = process.pid
            item["log"] = str(log_path)
            update()
            code = process.wait()
        item.update({"returncode": code, "status": "completed" if code == 0 else "failed"})
        if code == 0:
            try:
                assert sha256(script) == source_sha, "Source mutated while job active"
                freeze(input_directory / output)
            except Exception as error:
                item.update({"status": "failed_completion_gate", "reason": str(error)})
        update()
    status["status"] = "completed" if all(j["status"] == "completed" for j in status["jobs"]) else "completed_with_gaps"
    update()


if __name__ == "__main__":
    main()
