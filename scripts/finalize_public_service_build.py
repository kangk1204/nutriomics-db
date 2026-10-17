"""Finish an unactivated, stopped private build. Never repair an immutable release."""
import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from nutrigenomics.explorer_build import canonical, sha256
from nutrigenomics.publication import _csv, _write_review
from nutrigenomics.source_updates import atomic_json


def finalize(staging, parent, output):
    staging, parent, output = (Path(p).resolve() for p in (staging, parent, output))
    if output.exists() or staging != output.with_name(output.name + ".building"):
        raise ValueError("only a private .building directory may be resumed")
    parent_manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(staging / "nutriomics.sqlite3")) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA cache_size=-262144")
        metadata = {r[0]: json.loads(r[1]) for r in db.execute("SELECT * FROM meta")}
        metadata.update(created_at=datetime.now(UTC).isoformat(),
                        dataset_release_id=parent_manifest["release_id"], service_build_id=metadata["release_id"])
        if (metadata.get("public_service_parent") != parent_manifest["release_id"] or
                metadata.get("public_service_parent_database_sha256") != parent_manifest["database_sha256"]):
            raise ValueError("staging parent binding mismatch")
        quality = metadata["quality"]
        atomic_json(staging / "build-audit/quality-before-key-normalization.json", quality)
        keys = {tuple(r) for r in db.execute("SELECT series_accession,contrast,analysis_version FROM contrast_selector")}
        absent = []
        for row in db.execute("SELECT metadata_json FROM science_record WHERE kind='contrast'"):
            value = json.loads(row[0])
            key = tuple(value.get(k) for k in ("series_accession", "contrast", "analysis_version"))
            if key not in keys:
                absent.append(list(key))
        quality["contrast_coverage"]["curated_without_public_values"] = absent
        db.execute("UPDATE meta SET value_json=? WHERE key='quality'", (canonical(quality),))
        for key in ("created_at", "dataset_release_id", "service_build_id"):
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, canonical(metadata[key])))
        db.commit()
        print(json.dumps({"phase": "staging_reference_audit_corrected", "withheld_selectors": len(absent)}), flush=True)
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("staging integrity failure")
        atomic_json(staging / "dataset-metadata.json", metadata)
        atomic_json(staging / "quality-report.json", {"release_id": metadata["release_id"], **quality})
        _write_review(staging, [dict(r) for r in db.execute("SELECT * FROM science_record ORDER BY kind,id")],
                      json.loads((staging / "source-catalog.json").read_text(encoding="utf-8")),
                      json.loads((staging / "review/analysis-audit.json").read_text(encoding="utf-8")))
        for table in ("node", "meta", "food_alias", "node_domain", "edge_domain", "science_record", "composition_assessment", "contrast_selector"):
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            _csv(staging / "tables" / (table + ".csv.gz"), columns, db.execute("SELECT * FROM " + table))
        (staging / "schema.sql").write_text("\n".join(r[0] + ";" for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")), encoding="utf-8")
    print(json.dumps({"phase": "verify_completed_files"}), flush=True)
    files = [{"path": p.relative_to(staging).as_posix(), "bytes": p.stat().st_size,
              "sha256": sha256(p)} for p in sorted(staging.rglob("*")) if p.is_file() and p != staging / "manifest.json"]
    database_hash = next(r["sha256"] for r in files if r["path"] == "nutriomics.sqlite3")
    atomic_json(staging / "manifest.json", {**parent_manifest, "release_id": metadata["release_id"],
                "database_sha256": database_hash, "public_counts": metadata["counts"], "files": files,
                "build_resume": "Private build resumed after controlled stop; key normalization audit retained"})
    with (staging / "nutriomics.sqlite3").open("r+b") as f:
        os.fsync(f.fileno())
    staging.rename(output)
    return {"release_id": metadata["release_id"], "database_sha256": database_hash,
            "eligible_foods": quality["composition"]["eligible_foods"], "scientific_review": "pending"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("staging", "parent", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(finalize(args.staging, args.parent, args.output)))
