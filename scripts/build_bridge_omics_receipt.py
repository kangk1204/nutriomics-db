"""Describe released and pending omics artifacts without inferring completion."""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from freeze_omics_bundle import sha256


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    work = parser.parse_args().root.resolve()
    names = ["gse13466-raw-v2", "gse27385-raw-v2", "gse98645-acute-studyfamily-v2",
             "gse98645-chronic-studyfamily-v2", "gse56960-dose-v3",
             "gse107205-between-arm-v4", "direct-plus-idat-v1"]
    rows = []
    for name in names:
        folder = work / "outputs" / name
        candidates = [(folder, "primary")]
        for subdirectory in ["pathways-v1", "regions-fixed1000bp-v1", "dmrcate-sensitivity-v1",
                             "blood-composition-closed-v2"]:
            if (folder / subdirectory).exists():
                candidates.append((folder / subdirectory, "secondary"))
        for artifact, role in candidates:
            item = {"path": str(artifact), "role": role,
                    "computational_review": "pending", "manual_expert_review": "pending"}
            for filename in ["analysis_metadata.json", "DONE.json", "RELEASE_APPROVAL.json"]:
                if (artifact / filename).exists():
                    document = json.loads((artifact / filename).read_text())
                    item[filename] = {"sha256": sha256(artifact / filename)}
                    if filename == "analysis_metadata.json":
                        for field in ["accession", "analysis_version", "run_utc", "independent_people", "sample_count"]:
                            item[field] = document.get(field)
                    elif filename == "DONE.json":
                        item["result_summary"] = document["result_summary"]
                        item["result_rows"] = sum(record["rows"] for table in document["result_summary"].values() for record in table.values())
                        item["admissibility"] = "observed_result_rows" if item["result_rows"] else "no_admissible_results"
                        item["completion_status"] = document["status"]
                        item["frozen_source_snapshots"] = [
                            {"original_permanent_path": str(artifact / record["path"]),
                             "sha256": record["sha256"],
                             "currently_verified": sha256(artifact / record["path"]) == record["sha256"]}
                            for record in document["executed_source_snapshots"]]
                    else:
                        item["computational_review"] = document["scientific_qc_status"]
                        item["limits"] = document["limits"]
                        producer = Path(document["producer_status_path"])
                        item["immutable_producer_status"] = {
                            "original_permanent_path": str(producer),
                            "sha256": document["producer_status_sha256"],
                            "currently_verified": producer.exists() and sha256(producer) == document["producer_status_sha256"],
                            "job": document["producer_job"], "run_id": document["run_id"]}
                        declared = Path(document["reactome_release_audit"])
                        resolved = declared if declared.exists() else work / "reactome-release-audit/release95_audit.json"
                        item["reactome_audit_resolution"] = {
                            "original_declared_path": str(declared), "resolved_sidecar_path": str(resolved),
                            "sha256": sha256(resolved) if resolved.exists() else None,
                            "ready_approval_bytes_rewritten": False}
            for filename in ["pathway_status.json", "dmr_status.json"]:
                if (artifact / filename).exists():
                    item[filename] = {"sha256": sha256(artifact / filename),
                                      "statuses": json.loads((artifact / filename).read_text())}
            rows.append(item)
    progress = {}
    for filename in ["batch_status.json", "completion_repair_status.json", "postprocessing_status.json",
                     "postprocessing_repaired_status.json", "closed_composition_status.json",
                     "closed_composition_retry_status.json"]:
        if (work / filename).exists():
            progress[filename] = json.loads((work / filename).read_text())
    receipt = {"generated_utc": datetime.now(timezone.utc).isoformat(), "permanent_root": str(work),
               "artifacts": rows, "progress": progress,
               "limits": ["No primary-bundle mutation or production deployment by this worker",
                          "Effect row count is not participant count; assay scopes are explicit",
                          "GSE98645 56 acute plus 28 chronic assays share 14 baselines; unique modeled union 70 of 77 public assays",
                          "No clinical benefit or personal expression/CpG prediction inferred from omics direction",
                          "Original provenance paths remain in the permanent expansion directory; production relocation mapping belongs to the deploying consumer",
                          "Unclosed blood-composition-v1 artifacts are archived and not eligible for release"]}
    path = work / "omics_execution_receipt.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, indent=2))
    temporary.replace(path)
    print(json.dumps({"path": str(path), "sha256": sha256(path), "artifacts": len(rows)}))


if __name__ == "__main__":
    main()
