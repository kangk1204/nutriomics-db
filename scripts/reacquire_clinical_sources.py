"""Acquire immutable clinical-source snapshots; changes never approve a curation."""
import argparse
import json
import shutil
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json, fetch_source


def reacquire(registry, output, *, check_latest=False):
    output = Path(output)
    spec = json.loads(Path(registry).read_text(encoding="utf-8"))
    records = []
    identifiers = [s["id"] for s in spec["sources"]]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Duplicate source IDs in registry")
    for source in spec["sources"]:
        entry = dict(source)
        expected = entry.get("expected_sha256")
        if check_latest:
            entry.pop("expected_sha256", None)
        snapshot = fetch_source(entry, output / "snapshots")
        matching = snapshot["sha256"] == expected
        if matching and not check_latest:
            destination = output / source["filename"]
            if destination.exists() and sha256(destination) != expected:
                raise ValueError("Existing named source changed; preserve and investigate")
            if not destination.exists():
                shutil.copyfile(snapshot["object_path"], destination)
        records.append({**snapshot, "fixed_curation_sha256": expected,
            "matches_fixed_curation": matching,
            "status": "content_unchanged" if matching else "changed_requires_source_rights_and_scientific_review",
            "automatic_expert_approval": False, "automatic_publication": False})
        atomic_json(output / "download-report.json", {"sources": records, "complete": False,
            "automatic_expert_approval": False, "automatic_publication": False})
    report = {"sources": records, "complete": True, "check_latest": check_latest,
        "fixed_source_matches": sum(r["matches_fixed_curation"] for r in records),
        "automatic_expert_approval": False, "automatic_publication": False,
        "next_step": "Only unchanged fixed sources may reproduce this curation; changed sources require a new reviewed version"}
    atomic_json(output / "download-report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("config/clinical_source_downloads.v1.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-latest", action="store_true")
    args = parser.parse_args()
    result = reacquire(args.registry, args.output, check_latest=args.check_latest)
    print(json.dumps({"sources": len(result["sources"]), "fixed_source_matches": result["fixed_source_matches"],
                      "automatic_expert_approval": False, "automatic_publication": False}))
