"""Publish an immutable completion manifest after a successful omics job."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def freeze(folder: Path) -> dict:
    metadata = json.loads((folder / "analysis_metadata.json").read_text())
    ready_path = folder / "DONE.json"
    if ready_path.exists():
        ready = json.loads(ready_path.read_text())
        for item in ready["files"]:
            assert sha256(folder / item["path"]) == item["sha256"], item["path"]
        return ready
    snapshot = folder / "source_snapshot"
    snapshot.mkdir(exist_ok=True)
    sources = []
    for name in ("script", "helper"):
        if metadata.get(name + "_path"):
            original = Path(metadata[name + "_path"])
            expected = metadata[name + "_sha256"]
            archived = snapshot / original.name
            if archived.exists():
                assert sha256(archived) == expected, "Frozen source differs from executed source"
            else:
                assert sha256(original) == expected, "Executed source has changed before archival"
                shutil.copyfile(original, archived)
            sources.append({"path": str(archived.relative_to(folder)), "sha256": expected})
    # jsonlite auto_unbox serializes a length-one R vector as a JSON string.
    # Both forms designate file names, never individual characters.
    result_files = metadata.get("result_files")
    if result_files is None:
        # Older expression producers use the importer's two documented names.
        # Both must exist; this is not permission to guess arbitrary outputs.
        legacy = ("gene_effects.tsv.gz", "pathway_effects.tsv.gz")
        if not all((folder / name).is_file() for name in legacy):
            raise ValueError("Producer omitted result_files and its legacy outputs are incomplete")
        result_files = list(legacy)
    if isinstance(result_files, str):
        result_files = [result_files]
    if not isinstance(result_files, list) or not all(isinstance(n, str) for n in result_files):
        raise ValueError("result_files must be a filename or a list of filenames")
    summaries = {}
    for filename in result_files:
        if Path(filename).is_absolute() or ".." in Path(filename).parts:
            raise ValueError("Result file must stay inside its bundle")
        path = folder / filename
        contrasts: dict[str, dict] = {}
        with gzip.open(path, "rt", newline="") as source:
            for row in csv.DictReader(source, delimiter="\t"):
                item = contrasts.setdefault(row["contrast"], {"rows": 0, "joint_fdr_lt_005": 0,
                                                               "n_people": set(), "n_samples": set()})
                item["rows"] += 1
                for field in ("n_people", "n_samples"):
                    if field in row:
                        item[field].add(row[field])
                for field in ("p_value", "q_value"):
                    value = row.get(field, "NA")
                    if value not in ("NA", "", "NaN"):
                        numeric = float(value)
                        assert math.isfinite(numeric) and 0 <= numeric <= 1, (filename, field, numeric)
                q = row.get("q_value", "NA")
                if q not in ("NA", "", "NaN") and float(q) < .05:
                    item["joint_fdr_lt_005"] += 1
        summaries[filename] = {k: {field: sorted(v) if isinstance(v, set) else v
                                  for field, v in item.items()} for k, item in contrasts.items()}
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name not in ("DONE.json", "DONE.json.tmp"):
            files.append({"path": str(path.relative_to(folder)), "bytes": path.stat().st_size,
                          "sha256": sha256(path)})
    ready = {"status": "completed", "accession": metadata["accession"],
             "analysis_version": metadata["analysis_version"], "run_utc": metadata["run_utc"],
             "published_utc": datetime.now(timezone.utc).isoformat(), "files": files,
             "executed_source_snapshots": sources, "result_summary": summaries,
             "annotation_limit": "Annotation identities are frozen; biological release validation is separate evidence and is not inferred from package version"}
    temporary = ready_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(ready, indent=2))
    temporary.replace(ready_path)
    return ready


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    options = parser.parse_args()
    ready = freeze(options.folder.resolve())
    print(json.dumps({"folder": str(options.folder), "analysis_version": ready["analysis_version"],
                      "result_summary": ready["result_summary"], "done_sha256": sha256(options.folder / "DONE.json")}))


if __name__ == "__main__":
    main()
