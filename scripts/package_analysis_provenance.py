"""Package permitted producer metadata and sample design, preserving original hashes."""
import argparse
import csv
import json
import re
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.publication import portable, record_hash
from nutrigenomics.source_updates import atomic_json

SAMPLE_FIELDS = {
    "sample_accession", "geo_sample_accession", "sample_key", "sample_label", "count_label",
    "subject_id", "subject_candidate", "subject_field", "subject_parse_source", "person_id",
    "study_day", "visit", "visit_code", "timepoint", "timepoint_candidate", "time_hours",
    "source_time_text", "time_audit", "condition", "condition_id", "condition_candidate",
    "meal", "dose_kcal", "diet_arm", "arm", "endpoint", "period", "status_group", "platform_id",
    "channel_labels_json", "channel_timepoints_json", "paired_channel_design_json",
    "inclusion_status", "inclusion_reason", "sample_qc_flag", "detection_failure_fraction",
}


def package(audit, policy, release, project_root, output):
    data = json.loads(Path(audit).read_text(encoding="utf-8"))
    permissions = {(r["series_accession"], r["analysis_version"]): r
                   for r in json.loads(Path(policy).read_text(encoding="utf-8"))["analyses"]}
    release, project_root, output = (Path(p).resolve() for p in (release, project_root, output))
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    destination = output
    output = destination.with_name(destination.name + ".building")
    if destination.exists() or output.exists():
        raise FileExistsError("provenance supplement already exists")
    output.mkdir(parents=True)
    files, excluded = [], []
    for run in data["runs"]:
        key = (run["series_accession"], run["analysis_version"])
        rights = permissions.get(key, {})
        if rights.get("export_allowed") is not True:
            excluded.append({"analysis": list(key), "reason": "source_output_permission_unconfirmed"})
            continue
        part = output / record_hash(list(key))[:16]
        outputs = run["output_manifest_json"]
        outputs = json.loads(outputs) if isinstance(outputs, str) else outputs
        for entry in outputs:
            source = Path(entry["path"]).resolve()
            role = ("analysis_metadata" if source.name in {"analysis_metadata.json", "reactome_snapshot.json"}
                    else "sample_design" if source.name in {"sample_metadata.tsv", "person_metadata.tsv"}
                    else "archived_analysis_code" if source.suffix == ".R"
                    else "qc_figure" if source.suffix == ".pdf" and "qc" in source.name.casefold()
                    else None)
            if role is None:
                continue
            if not source.is_relative_to(project_root) or not source.is_file() or sha256(source) != entry["sha256"]:
                raise ValueError("producer attachment missing or changed: " + source.name)
            part.mkdir(parents=True, exist_ok=True)
            target = part / source.name
            record = {"analysis": list(key), "role": role, "source_filename": source.name,
                      "original_sha256": entry["sha256"], "rights_policy_id": rights["id"],
                      "scientific_review": "pending", "transformations": []}
            if role == "sample_design":
                with source.open(encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f, delimiter="\t")
                    columns = [c for c in reader.fieldnames or [] if c in SAMPLE_FIELDS]
                    if not columns:
                        excluded.append({"analysis": list(key), "filename": source.name,
                                         "reason": "no_public_sample_design_columns"})
                        continue
                    rows = [{c: row[c] for c in columns} for row in reader]
                    record.update(rows=len(rows), columns=columns,
                                  excluded_columns=[c for c in reader.fieldnames if c not in columns],
                                  row_unit="source design-table rows; not independent participants")
                with target.open("w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=columns, delimiter="\t")
                    writer.writeheader(); writer.writerows(rows)
                record["transformations"].append("retain declared source sample/group/time/QC fields; omit raw metadata blobs and other columns")
            elif role == "analysis_metadata":
                atomic_json(target, portable(json.loads(source.read_text(encoding="utf-8"))))
                record["transformations"].append("remove operational paths and private keys; preserve model/statistical values")
            elif role == "archived_analysis_code":
                text = source.read_text(encoding="utf-8")
                public = re.sub(r'"/home/[^/]+/projects/[^/]+/expansion/\d{4}-\d{2}-\d{2}"',
                                'Sys.getenv("NUTRIOMICS_ANALYSIS_ROOT", unset="expansion")', text)
                for quote in ('"', "'"):
                    public = public.replace(quote + str(project_root) + quote,
                                            'Sys.getenv("NUTRIOMICS_PROJECT_ROOT", unset=".")')
                target.write_text(public, encoding="utf-8")
                record["transformations"].append("replace quoted laboratory path defaults with NUTRIOMICS_ANALYSIS_ROOT; analytical code preserved; clean R reproduction still required")
                record["license"] = "MIT"
            else:
                target.write_bytes(source.read_bytes())
            record.update(path=str(target.relative_to(output)).replace("\\", "/"),
                          bytes=target.stat().st_size, sha256=sha256(target))
            files.append(record)
    result = {"release_id": manifest["release_id"], "parent_database_sha256": manifest["database_sha256"],
              "files": files, "exclusions": excluded, "scientific_review": "pending",
              "clean_biological_reproduction": "not_completed",
              "license": "CC-BY-4.0 for own permitted metadata/QC; MIT for own code; upstream terms retained"}
    atomic_json(output / "manifest.json", result)
    (output / "README.md").write_text(
        "# Analysis provenance supplement\n\nThis supplement is linked to the exact parent release and database hash. "
        "The original producer hashes and the separately transformed public hashes are retained. "
        "Sample-table rows are not participants: arrays, channels, visits and source subject labels require scientific review. "
        "Legacy sample tables do not establish fitted-model inclusion unless that field is present. "
        "Raw sample metadata/covariates and matrices must be reacquired from their original repository when required. "
        "Set the project root explicitly when rerunning archived code. This package does not certify clean biological reanalysis, "
        "independent review, or scientific correctness of a pending result.\n", encoding="utf-8")
    output.rename(destination)
    return {"release_id": manifest["release_id"], "files": len(files), "scientific_review": "pending"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("audit", "policy", "release", "project-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.audit, args.policy, args.release, args.project_root, args.output)))
