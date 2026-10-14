"""Stream all public Parquet values against SQLite and optionally the frozen source."""
import argparse
import json
import time
from collections import defaultdict
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from nutrigenomics.explorer_build import canonical, decode, read_only, sha256
from nutrigenomics.publication import (
    EFFECT_FIELDS,
    INT_EFFECT,
    KEY_FIELDS,
    REAL_EFFECT,
    TEXT_EFFECT,
    effect_id,
    portable,
)
from nutrigenomics.source_updates import atomic_json


def validate(release, output, core=None):
    release, output = Path(release), Path(output)
    manifest = json.loads((release / "manifest.json").read_text())
    checked, errors, parts, mismatch_count = 0, [], [], 0
    last_checkpoint = time.monotonic()
    fields = (*TEXT_EFFECT, *REAL_EFFECT, *INT_EFFECT)
    with ExitStack() as stack:
        public = stack.enter_context(read_only(release / "nutriomics.sqlite3"))
        public.execute("PRAGMA cache_size=-262144")
        public.execute("BEGIN")
        origin = stack.enter_context(read_only(Path(core))) if core else None
        if origin:
            origin.execute("PRAGMA cache_size=-524288")
            origin.execute("BEGIN")
        for path in sorted((release / "effects").glob("*.parquet")):
            part_count = 0
            for batch in pq.ParquetFile(path).iter_batches(batch_size=256, use_threads=False):
                rows = batch.to_pylist()
                ids = [r["id"] for r in rows]
                stored = {r["id"]: dict(r) for r in public.execute("SELECT * FROM effect WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)}
                source = {}
                if origin:
                    groups = defaultdict(list)
                    for row in rows:
                        groups[(row["series_accession"], row["analysis_version"], row["contrast"], row["feature_type"])].append(row["feature_id"])
                    for key, features in groups.items():
                        for row in origin.execute("SELECT * FROM study_effect WHERE series_accession=? AND analysis_version=? AND contrast=? AND feature_type=? AND feature_id IN (" + ",".join("?" for _ in features) + ")", [*key, *features]):
                            source[effect_id(row)] = dict(row)
                for row in rows:
                    candidate = stored.get(row["id"])
                    if not candidate or any(row[k] != candidate[k] for k in EFFECT_FIELDS):
                        mismatch_count += 1
                        if len(errors) < 50:
                            errors.append({"id": row["id"], "reason": "parquet_sqlite_mismatch"})
                    if origin:
                        original = source.get(row["id"])
                        if not original or any(row[k] != original[k] for k in fields) or canonical(portable(decode(original["metadata_json"]))) != row["metadata_json"]:
                            mismatch_count += 1
                            if len(errors) < 50:
                                errors.append({"id": row["id"], "reason": "source_value_or_metadata_mismatch"})
                checked += len(rows)
                part_count += len(rows)
                if time.monotonic() - last_checkpoint >= 30:
                    atomic_json(output, {"status": "in_progress", "release_id": manifest["release_id"],
                                "database_sha256": manifest["database_sha256"], "checked_rows": checked,
                                "mismatch_count": mismatch_count, "first_errors": errors,
                                "completed_partitions": parts, "current_partition": path.name,
                                "current_partition_rows": part_count, "source_compared": origin is not None})
                    last_checkpoint = time.monotonic()
            parts.append({"file": path.name, "rows": part_count, "sha256": sha256(path)})
            atomic_json(output, {"status": "in_progress", "release_id": manifest["release_id"], "checked_rows": checked, "first_errors": errors, "completed_partitions": parts})
        total = public.execute("SELECT count(*) FROM effect").fetchone()[0]
    if checked != total:
        mismatch_count += 1
        errors.append({"reason": "partition_scope_mismatch", "parquet_rows": checked, "sqlite_rows": total})
    report = {"status": "passed" if not errors else "failed", "passed": not errors,
              "verified_at": datetime.now(UTC).isoformat(), "release_id": manifest["release_id"],
              "database_sha256": manifest["database_sha256"], "checked_rows": checked,
              "source_compared": origin is not None, "five_part_key": list(KEY_FIELDS),
              "partitions": parts, "first_errors": errors, "mismatch_count": mismatch_count,
              "expert_review": "pending", "validation_scope": "value preservation, not biological validity"}
    atomic_json(output, report)
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--release", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--core", type=Path)
    a = p.parse_args()
    result = validate(a.release, a.output, a.core)
    print(json.dumps({k: result[k] for k in ("status", "checked_rows", "source_compared")}))
    raise SystemExit(0 if result["passed"] else 1)
