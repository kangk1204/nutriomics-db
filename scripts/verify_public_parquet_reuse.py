"""Bounded public Parquet/CSV/independent-DuckDB reuse without service changes.

Run the supervisor in a separate operator directory. It imports engines in
separate finite subprocesses, then validates one rights-approved small source
file and emits a small public-only sample. This is not a matrix-wide replay.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.metadata
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path

KEY = ["series_accession", "feature_type", "feature_id", "contrast", "analysis_version"]
STATS = ["effect_estimate", "standard_error", "p_value", "q_value", "n_people", "n_samples"]
LIMIT = 128


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def fp(path):
    s = Path(path).stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def safe(root, relative):
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or "\\" in relative or ":" in relative:
        raise ValueError("Nonportable file reference")
    root = Path(root).resolve()
    result = root / p
    if result.is_symlink() or not result.resolve().is_relative_to(root) or not result.is_file():
        raise ValueError("Missing or escaping source")
    return result


def compare_rows(left, right):
    """Exact Python types/values; NULL, zero, empty strings and bools differ."""
    if len(left) != len(right):
        raise ValueError("Row count mismatch")
    for index, (a, b) in enumerate(zip(left, right, strict=True)):
        if set(a) != set(b):
            raise ValueError("Column set mismatch")
        for key in a:
            if type(a[key]) is not type(b[key]) or a[key] != b[key]:
                raise ValueError(f"Typed value mismatch at row{index},column{key}")


def plan(root, expected_sha):
    root = Path(root).resolve()
    mapping = safe(root, "filemap.json")
    if sha(mapping) != expected_sha:
        raise ValueError("Public package filemap changed")
    bindings = {r["path"]: r for r in json.loads(mapping.read_text(encoding="utf-8"))["files"]}
    files = sorted((r for r in bindings.values() if r["path"].startswith("base/effects/") and r["path"].endswith(".parquet")), key=lambda r: (r["bytes"], r["path"]))
    if not files:
        raise ValueError("No public Parquet declared")
    selected = files[0]
    if selected["bytes"] > 8 * 1024**2:
        raise ValueError("No sufficiently small bounded source")
    pair = {"parquet": selected, "csv": bindings[selected["path"].removesuffix(".parquet") + ".csv.gz"]}
    states = {}
    for r in pair.values():
        p = safe(root, r["path"])
        states[r["path"]] = fp(p)
        if p.stat().st_size != r["bytes"] or sha(p) != r["sha256"] or fp(p) != states[r["path"]]:
            raise ValueError("Selected public source hash changed")
    for name in ["base/rights-policy.json", "supplement/release-science.sqlite3"]:
        p = safe(root, name)
        if p.stat().st_size != bindings[name]["bytes"] or sha(p) != bindings[name]["sha256"]:
            raise ValueError("Public metadata rights binding changed")
        states[name] = fp(p)
    policy = json.loads(safe(root, "base/rights-policy.json").read_text(encoding="utf-8"))
    allowed = {(r["series_accession"], r["analysis_version"]): r for r in policy["analyses"] if r.get("export_allowed") is True}
    with sqlite3.connect(safe(root, "supplement/release-science.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        selectors = []
        for series, feature, contrast, version, references, policy_id in db.execute("SELECT series_accession,feature_type,contrast,analysis_version,files_json,rights_policy_id FROM effect_catalog"):
            if any(r["path"] == selected["path"] for r in json.loads(references)):
                right = allowed.get((series, version))
                if not right or right["id"] != policy_id or right.get("own_results_license") != "CC-BY-4.0":
                    raise ValueError("Sample source redistribution is not established")
                selectors.append({"series_accession": series, "feature_type": feature, "contrast": contrast,
                                  "analysis_version": version, "rights_policy_id": policy_id, "terms_url": right["terms_url"]})
    if not selectors:
        raise ValueError("No rights-approved native selector for selected file")
    return {"root": str(root), "filemap_sha256": expected_sha, "files": pair, "source_fingerprints": states,
            "selectors": selectors, "rows_limit": LIMIT, "license": "CC-BY-4.0",
            "cpu_affinity_maximum": 4, "query_threads_requested": 4,
            "memory_limit_bytes": 16 * 1024**3, "source_data_modified": False}


def limits():
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "4"
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (16 * 1024**3, 16 * 1024**3))
        if hasattr(os, "sched_getaffinity"):
            os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:4])
        os.nice(10)


def worker(mode, folder):
    limits()
    folder = Path(folder)
    value = json.loads((folder / "operator-plan.json").read_text(encoding="utf-8"))
    started, cpu = time.monotonic(), time.process_time()
    receipt = {"mode": mode, "passed": False, "scope": "Selected small native public file/first128rows; not matrix-wide validation"}
    try:
        if mode == "import-arrow":
            import faulthandler
            faulthandler.dump_traceback_later(20)
            import pyarrow.parquet as pq
            faulthandler.cancel_dump_traceback_later()
            receipt["pyarrow"] = importlib.metadata.version("pyarrow")
            receipt["module"] = pq.__name__
        elif mode == "read-arrow":
            import pyarrow.parquet as pq
            path = safe(value["root"], value["files"]["parquet"]["path"])
            pf = pq.ParquetFile(path)
            batch = next(pf.iter_batches(batch_size=LIMIT, use_threads=False))
            rows = batch.to_pylist()
            selector_keys = {(r["series_accession"], r["feature_type"], r["contrast"], r["analysis_version"]) for r in value["selectors"]}
            for row in rows:
                if (row["series_accession"], row["feature_type"], row["contrast"], row["analysis_version"]) not in selector_keys or row["feature_id"] is None:
                    raise ValueError("Source five-part key differs from public catalog")
                for field in ("p_value", "q_value"):
                    if row[field] is not None and not (math.isfinite(row[field]) and 0 <= row[field] <= 1):
                        raise ValueError("Nonfinite/out-of-range reported probability")
            with gzip.open(safe(value["root"], value["files"]["csv"]["path"]), "rt", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                csv_rows = [next(reader) for _ in rows]
            converted = []
            for source, raw in zip(rows, csv_rows, strict=True):
                row = {}
                for name, source_value in source.items():
                    if source_value is None:
                        if raw[name] != "":
                            raise ValueError("CSV missingness differs from typed source")
                        row[name] = None
                    else:
                        row[name] = type(source_value)(raw[name])
                converted.append(row)
            compare_rows(rows, converted)
            pq.write_table(batch.to_table() if hasattr(batch, 'to_table') else __import__('pyarrow').Table.from_batches([batch]), folder / "public-sample.parquet")
            write(folder / "arrow-rows.json", rows)
            receipt.update(rows=len(rows), schema={f.name: str(f.type) for f in batch.schema},
                csv_typed_agreement=True, five_part_keys_verified=True,
                statistical_missing_counts={k: sum(r[k] is None for r in rows) for k in STATS},
                all_column_missing_counts={k: sum(r[k] is None for r in rows) for k in rows[0]},
                actual_statistical_rows=[{k: r[k] for k in [*KEY, *STATS]} for r in rows[:3]],
                source_file_rows=pf.metadata.num_rows, source_row_groups=pf.metadata.num_row_groups,
                full_source_rows_read=False, sample_sha256=sha(folder / "public-sample.parquet"))
        elif mode == "read-duckdb":
            import duckdb
            imported = time.monotonic()
            path = safe(value["root"], value["files"]["parquet"]["path"])
            with duckdb.connect(":memory:", config={"threads": "4", "memory_limit": "1GB", "enable_external_access": "true"}) as db:
                db.execute("SET preserve_insertion_order=true")
                cursor = db.execute("SELECT * FROM read_parquet(?) LIMIT ?", [str(path), LIMIT])
                columns = [r[0] for r in cursor.description]
                rows = [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]
            compare_rows(json.loads((folder / "arrow-rows.json").read_text(encoding="utf-8")), rows)
            receipt.update(duckdb=duckdb.__version__, import_seconds=imported - started, rows=len(rows),
                           all_columns_typed_agreement=True, five_part_keys_verified=True)
        else:
            raise ValueError("Unknown worker")
        receipt["passed"] = True
    except Exception as exc:  # noqa: BLE001 -- finite operator boundary stores complete native import/read failures.
        receipt["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
    receipt.update(elapsed_seconds=time.monotonic() - started, cpu_seconds=time.process_time() - cpu)
    if os.name == "posix":
        import resource
        receipt["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    write(folder / (mode + ".json"), receipt)
    return 0 if receipt["passed"] else 1


def process_sample(pid):
    root = Path("/proc") / str(pid)
    if not root.exists():
        return None
    result = {}
    for name in ["status", "io", "wchan"]:
        try:
            raw = (root / name).read_text()
            result[name] = raw if name != "status" else "\n".join(r for r in raw.splitlines() if r.startswith(("State:", "VmRSS:", "VmHWM:", "Threads:", "Cpus_allowed_list:")))
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            result[name] = None
    return result


def supervise(root, output, expected_sha, timeout):
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(Path(root).resolve()):
        raise ValueError("Use a new operator directory outside immutable sources")
    output.mkdir(parents=True)
    value = plan(root, expected_sha)
    write(output / "operator-plan.json", value)
    phases = []
    for mode in ["import-arrow", "read-arrow", "read-duckdb"]:
        args = [sys.executable, "-X", "importtime", str(Path(__file__).resolve()), "--worker", mode, "--output", str(output)]
        began = time.monotonic()
        samples = []
        with (output / (mode + ".stdout.log")).open("wb") as out, (output / (mode + ".stderr.log")).open("wb") as err:
            child = subprocess.Popen(args, stdout=out, stderr=err)
            timed_out = False
            while child.poll() is None:
                samples.append({"elapsed_seconds": time.monotonic() - began, "process": process_sample(child.pid) if os.name == "posix" else None})
                if time.monotonic() - began > timeout:
                    timed_out = True
                    child.kill()
                    break
                time.sleep(.5)
            code = child.wait(timeout=10)
        phase = {"mode": mode, "exit_code": code, "timeout_seconds": timeout, "timed_out": timed_out,
                 "elapsed_seconds": time.monotonic() - began, "resource_samples": samples}
        phases.append(phase)
        write(output / "progress.json", {"phases": phases})
        if code:
            break
    stable = all(fp(safe(root, p)) == old for p, old in value["source_fingerprints"].items())
    receipt = {"schema_version": "public-parquet-reuse-v1", "passed": len(phases) == 3 and all(r["exit_code"] == 0 for r in phases) and stable,
        "original_sources_unchanged": stable, "phases": phases,
        "scope": "PyArrow import and actual first128rows in one approved small file, CSV agreement, independent DuckDB typed agreement",
        "matrix_wide_validation": False, "prior_failed_attempt_replaced": False,
        "service_changes": False, "publication_ready": False}
    write(output / "parquet-reuse.json", receipt)
    return 0 if receipt["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["import-arrow", "read-arrow", "read-duckdb"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--filemap-sha256")
    parser.add_argument("--phase-timeout", type=int, default=90)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.output)
    return supervise(args.root, args.output, args.filemap_sha256, args.phase_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
