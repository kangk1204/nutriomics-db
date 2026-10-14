"""Prepare a repository-neutral deposit plan; never upload or invent a DOI."""
import argparse
import json
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json


def prepare(release, code_archive, output, *, verify_files=False):
    release, code_archive, output = map(Path, (release, code_archive, output))
    metadata = json.loads((release / "dataset-metadata.json").read_text(encoding="utf-8"))
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    if manifest["release_id"] != metadata["release_id"]:
        raise ValueError("metadata and manifest release mismatch")
    files, seen = [], set()
    for row in manifest["files"]:
        path = (release / row["path"]).resolve()
        if not path.is_relative_to(release.resolve()) or row["path"] in seen:
            raise ValueError("unbounded or duplicate deposit file")
        seen.add(row["path"])
        if not path.is_file() or path.stat().st_size != row["bytes"]:
            raise ValueError("deposit file absent or changed: " + row["path"])
        if verify_files and sha256(path) != row["sha256"]:
            raise ValueError("deposit file hash mismatch: " + row["path"])
        files.append({**row, "record": "data", "hash_verified_now": verify_files})
    # The manifest itself is outside its own recursive hash list.
    if "manifest.json" not in seen:
        path = release / "manifest.json"
        files.append({"path": path.name, "bytes": path.stat().st_size,
                      "sha256": sha256(path), "record": "data", "hash_verified_now": True})
    if not code_archive.is_file():
        raise ValueError("tested code archive is required")
    files.append({"path": code_archive.name, "bytes": code_archive.stat().st_size,
                  "sha256": sha256(code_archive), "record": "code", "hash_verified_now": True})
    total = sum(r["bytes"] for r in files if r["record"] == "data")
    record_limit = 50_000_000_000  # Conservative planning limit; verify repository policy before uploading.
    groups, group, size = [], [], 0
    for row in files:
        if row["record"] != "data":
            continue
        if row["bytes"] > record_limit:
            raise ValueError("single file exceeds planned repository limit; partition before deposit")
        if group and size + row["bytes"] > record_limit:
            groups.append(group); group, size = [], 0
        group.append(row["path"]); size += row["bytes"]
    if group:
        groups.append(group)
    draft = {
        "release_id": metadata["release_id"], "status": "deposit_preparation_only",
        "published": False, "data_version_doi": None, "code_version_doi": None,
        "title": "NutriOmics: food composition and human dietary-intervention omics context and results",
        "resource_type": "dataset", "creators": None, "official_affiliations": None,
        "funding": None, "institutional_ethics_determination": None,
        "counts": metadata["counts"], "data_bytes": total,
        "planned_data_file_groups": groups, "planned_limit_bytes_per_record": record_limit,
        "record_level_license": "requires_final_rights_assessment; no blanket third-party license",
        "own_curation_and_derived_output_license": "CC-BY-4.0", "own_code_license": "MIT",
        "source_terms": "rights-policy.json and source-catalog.json",
        "public_scope": metadata["scope"],
        "reuse_note": "Quantitative records represent features × contrasts × analysis versions, not participants. Human review remains pending until a hash-bound review is attached.",
        "relationships_to_fill": ["data version DOI ↔ code version DOI", "version DOI ↔ manuscript DOI", "exact version DOI ↔ latest concept DOI"],
        "must_not_publish_until": ["scientific review completed", "hash-bound final validation completed",
                                  "authors/affiliations/funding/ethics confirmed", "final source rights assessed",
                                  "exact data and code versions confirmed"],
        "all_data_hashes_verified_now": verify_files,
    }
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "deposit-metadata-draft.json", draft)
    atomic_json(output / "upload-files.json", {"release_id": metadata["release_id"], "files": files})
    (output / "DEPOSIT_README.md").write_text(
        "# Deposit preparation\n\nNo files have been uploaded and no DOI has been assigned. "
        "The metadata is a planning object, not an automatically publishable Zenodo API request. "
        "Confirm the journal, repository schema, file limits, rights, author statements and final gate before depositing. "
        "Use the exact version DOI in the manuscript. Preserve the manifest and file names; when several records are needed, "
        "link them through the same release manifest. Values with unconfirmed redistribution remain excluded. "
        "Run with --verify-files immediately before transfer; size checks alone do not establish file integrity.\n",
        encoding="utf-8")
    return {"release_id": metadata["release_id"], "data_bytes": total,
            "data_file_groups": len(groups), "published": False, "output": str(output.resolve())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("release", "code-archive", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--verify-files", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(args.release, args.code_archive, args.output, verify_files=args.verify_files)))
