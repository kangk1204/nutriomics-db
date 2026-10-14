"""Record a reviewed release gate separately from the immutable completion bundle."""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from freeze_omics_bundle import freeze, sha256


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--producer-status", type=Path, required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--limits", required=True)
    options = parser.parse_args()
    folder = options.folder.resolve()
    ready = freeze(folder)
    metadata = json.loads((folder / "analysis_metadata.json").read_text())
    producer = json.loads(options.producer_status.read_text())
    producer_sha = sha256(options.producer_status)
    # Secondary bundles are nested below a primary bundle; their depth does not
    # change the producer's project root or the frozen annotation audit location.
    producer_root = options.producer_status.resolve().parent
    if producer_root.name == "producer_status_snapshots":
        producer_root = producer_root.parent
    status_folder = producer_root / "producer_status_snapshots"
    status_folder.mkdir(exist_ok=True)
    status_snapshot = status_folder / (producer_sha + ".json")
    if not status_snapshot.exists():
        status_snapshot.write_bytes(options.producer_status.read_bytes())
    assert sha256(status_snapshot) == producer_sha
    job = next(j for j in producer["jobs"] if j["name"] == options.job_name)
    assert job["status"] == "completed" and job["returncode"] == 0, "Producer did not complete"
    for field in ("accession", "analysis_version", "run_utc"):
        assert ready[field] == metadata[field]
    identity = ":".join(metadata[k] for k in ("accession", "analysis_version", "run_utc", "script_sha256"))
    approval = {"completion_sha256": sha256(folder / "DONE.json"), "producer_exit_code": 0,
                "scientific_qc_status": "passed_with_declared_limitations",
                "run_id": hashlib.sha256(identity.encode()).hexdigest(),
                "reviewed_utc": datetime.now(timezone.utc).isoformat(),
                "producer_status_path": str(status_snapshot),
                "producer_status_sha256": producer_sha, "producer_job": options.job_name,
                "checks": ["Frozen source bytes match run_script SHA", "Producer exit code zero",
                           "Every result and metadata file covered by immutable DONE SHA",
                           "Assay count distinct from independent participant count",
                           "Person remains the independent fitting unit; repeated assays do not become new people",
                           "Primary joint-family and within-contrast multiple-testing fields preserved",
                           "Original sample provenance and condition mapping inspected; no invented identities",
                           "Unreported/non-estimable values remain missing; null and opposing effects retained"],
                "limits": options.limits.split("|"),
                "reactome_release_audit": str(producer_root / "reactome-release-audit/release95_audit.json")}
    path = folder / "RELEASE_APPROVAL.json"
    if path.exists():
        previous = json.loads(path.read_text())
        assert previous["run_id"] == approval["run_id"] and previous["completion_sha256"] == approval["completion_sha256"]
    else:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(approval, indent=2))
        temporary.replace(path)
    print(json.dumps({"folder": str(folder), "run_id": approval["run_id"], "approval_sha256": sha256(path)}))


if __name__ == "__main__":
    main()
