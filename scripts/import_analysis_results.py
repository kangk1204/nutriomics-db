from __future__ import annotations

import argparse
import gzip
import json
import math
import os
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import AnalysisRun, GeoSeries, StudyEffect
from nutrigenomics.settings import DATABASE_URL, PROJECT_ROOT

DEFAULT_RESULT_FILES = ("gene_effects.tsv.gz", "pathway_effects.tsv.gz")
DEFAULT_AUXILIARY_FILES = ("sample_metadata.tsv", "sample_qc.tsv", "voom_mean_variance.pdf")
LEGACY_GSE127530_INTERVENTION = (
    "Mixed high-fat challenge meal; supplementation visit-to-RNA mapping is unresolved. "
    "These contrasts do not estimate supplementation-specific effects."
)
STANDARD_RESULT_COLUMNS = {
    "feature_type",
    "feature_id",
    "feature_label",
    "contrast",
    "tissue",
    "timepoint",
    "intervention",
    "dose",
    "duration",
    "effect_estimate",
    "standard_error",
    "p_value",
    "q_value",
    "effect_unit",
    "status",
    "n_people",
    "n_samples",
    "metadata_json",
}


def _read_rows(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        headers = handle.readline().rstrip("\r\n").split("\t")
        if not headers or len(set(headers)) != len(headers):
            raise ValueError(f"Invalid or duplicate TSV columns in {path}")
        for line_number, line in enumerate(handle, start=2):
            values = line.rstrip("\r\n").split("\t")
            if len(values) != len(headers):
                raise ValueError(f"Invalid field count at {path}:{line_number}")
            yield dict(zip(headers, values, strict=True))


def _optional_float(value: str | None) -> float | None:
    if value is None or value in {"", "NA", "NaN"}:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _optional_int(value: str | None) -> int | None:
    if value is None or value in {"", "NA"}:
        return None
    return int(value)


def _metadata(value: str | None) -> dict:
    if value in {None, "", "NA"}:
        return {}
    payload = json.loads(value)
    return payload if isinstance(payload, dict) else {"value": payload}


def _file_record(path: Path) -> dict[str, str | int]:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def _safe_result_files(metadata: dict[str, Any]) -> list[str]:
    filenames = metadata.get("result_files") or list(DEFAULT_RESULT_FILES)
    # jsonlite auto_unbox encodes a single filename as a scalar string. Normalize
    # its shape in memory while preserving the original signed metadata bytes.
    if isinstance(filenames, str):
        filenames = [filenames]
    if not isinstance(filenames, list) or not filenames:
        raise ValueError("Analysis metadata must list at least one result file")
    if any(
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or name in {".", ".."}
        or not name.endswith((".tsv", ".tsv.gz"))
        for name in filenames
    ):
        raise ValueError("Analysis result files must be TSV basenames inside the output directory")
    if len(set(filenames)) != len(filenames):
        raise ValueError("Analysis metadata lists duplicate result files")
    return filenames


def _provenance_path(value: Any, fallback: Path) -> Path:
    if isinstance(value, str) and value:
        path = Path(value).expanduser()
        if path.is_file():
            return path.resolve()
    return fallback.resolve()


def _import_cache_mib() -> int:
    value = os.environ.get("NUTRIGENOMICS_IMPORT_CACHE_MIB", "512")
    try:
        budget = int(value)
    except ValueError as error:
        raise ValueError("Import cache must be an integer MiB budget") from error
    if not 16 <= budget <= 8192:
        raise ValueError("Import cache must be between 16 and 8192 MiB")
    memory = Path("/proc/meminfo")
    if budget > 512 and memory.is_file():
        available = next(int(line.split()[1]) for line in memory.read_text().splitlines()
                         if line.startswith("MemAvailable:"))
        if available < budget * 1024 * 2:
            raise MemoryError("Available RAM must exceed twice the requested import cache")
    return budget


def _producer_output_matches(value: Any, output_dir: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    original = PurePosixPath(value.replace("\\", "/"))
    if ".." in original.parts:
        return False
    actual = PurePosixPath(output_dir.as_posix()).parts
    suffix = original.parts
    if original.is_absolute():
        if suffix == actual:
            return True
        if "outputs" not in suffix:
            return False
        suffix = suffix[len(suffix) - 1 - list(reversed(suffix)).index("outputs"):]
    return len(suffix) <= len(actual) and actual[-len(suffix):] == suffix


def _verify_output_audit(path: Path, expected_sha: str, output_dir: Path,
                         metadata: dict, approval: dict, accession: str) -> dict:
    """Verify an explicitly pinned supplemental audit, never rewrite old receipts."""
    record = _file_record(path)
    if record["sha256"] != expected_sha:
        raise ValueError("Producer output audit changed from the approved release hash")
    audit = json.loads(path.read_text(encoding="utf-8"))
    identities = {
        "schema_version": 1, "audit_type": "producer_output_attribution",
        "verification_status": "passed_with_declared_limitations", "accession": accession,
        "analysis_version": metadata["analysis_version"],
        "completion_sha256": _file_record(output_dir / "DONE.json")["sha256"],
        "metadata_sha256": _file_record(output_dir / "analysis_metadata.json")["sha256"],
        "producer_status_sha256": approval["producer_status_sha256"],
        "producer_job_name": approval["producer_job"],
        "script_sha256": metadata["script_sha256"], "producer_exit_code": 0,
    }
    if any(audit.get(key) != value for key, value in identities.items()):
        raise ValueError("Producer output audit has mismatched study/source/completion identity")
    if (Path(audit.get("producer_status_path", "")).resolve()
            != Path(approval["producer_status_path"]).resolve()
            or not _producer_output_matches(audit.get("original_output_directory"), output_dir)):
        raise ValueError("Producer output audit does not resolve this output directory")
    checks = audit.get("checks", {})
    required = ("output_destination_verified", "data_attribution_verified", "independent_qc_verified")
    if any(checks.get(key) is not True for key in required):
        raise ValueError("Producer output audit lacks independent destination/data/QC verification")
    if (audit.get("argv_status") not in {"reconstructed_not_recorded", "actual_recorded"}
            or not audit.get("verification_scope") or not audit.get("historical_limitations")):
        raise ValueError("Producer output audit must disclose its historical verification scope")
    evidence = audit.get("evidence_files", [])
    if not evidence:
        raise ValueError("Producer output audit lacks frozen evidence files")
    for item in evidence:
        evidence_path = Path(item["path"])
        if (not evidence_path.is_file()
                or _file_record(evidence_path)["sha256"] != item.get("sha256")
                or not item.get("role") or not item.get("capture_timing")):
            raise ValueError("Producer output audit evidence changed or lacks capture provenance")
    represented = {item["sha256"] for item in evidence}
    if not {metadata["script_sha256"], approval["producer_status_sha256"]} <= represented:
        raise ValueError("Producer output audit evidence omits executed source or original status")
    qc = Path(audit.get("independent_qc_evidence", "")).resolve()
    if not any(Path(item["path"]).resolve() == qc for item in evidence):
        raise ValueError("Producer output audit independent QC is not hash-pinned")
    return {"manifest": record, "audit": audit}


def import_analysis_results(
    output_dir: Path,
    database_url: str = DATABASE_URL,
    accession: str = "GSE127530",
    require_completion: bool = False,
    producer_output_audit: Path | None = None,
    producer_output_audit_sha256: str | None = None,
) -> dict[str, int]:
    if (producer_output_audit is None) != (producer_output_audit_sha256 is None):
        raise ValueError("Producer output audit requires both its path and approved SHA256")
    if producer_output_audit is not None and not require_completion:
        raise ValueError("Supplemental producer audit requires the strict completion gate")
    output_dir = output_dir.expanduser().resolve()
    metadata_path = output_dir / "analysis_metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Analysis metadata missing: {metadata_path}")
    run_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    cache_mib = _import_cache_mib()
    run_metadata["loader_settings"] = {"sqlite_cache_mib": cache_mib,
                                       "cache_spill": "enabled", "atomic_version_import": True}
    if run_metadata.get("accession") != accession:
        raise ValueError("Analysis metadata accession does not match requested series")
    if int(run_metadata.get("independent_people", 0)) < 2:
        raise ValueError("Analysis metadata does not report an auditable person-level sample size")

    result_files = _safe_result_files(run_metadata)
    completion = output_dir / "DONE.json"
    if require_completion and not completion.is_file():
        raise ValueError("New analysis release requires a DONE completion marker")
    if completion.is_file():
        completed = json.loads(completion.read_text(encoding="utf-8"))
        if completed.get("status") != "completed" or completed.get("analysis_version") != run_metadata.get("analysis_version"):
            raise ValueError("Invalid analysis completion marker")
        if require_completion:
            approval_path=output_dir/"RELEASE_APPROVAL.json"
            approval=json.loads(approval_path.read_text(encoding="utf-8")) if approval_path.is_file() else completed
            required=set(result_files)|{"analysis_metadata.json"}
            listed={r["path"] for r in completed.get("files",[])}
            run_id=sha256((accession+":"+str(run_metadata.get("analysis_version"))+":"+str(run_metadata.get("run_utc"))+":"+str(run_metadata.get("script_sha256"))).encode()).hexdigest()
            if (completed.get("accession") != accession or completed.get("run_utc") != run_metadata.get("run_utc")
                or approval.get("run_id") != run_id or not required <= listed
                or approval.get("producer_exit_code") != 0
                or approval.get("scientific_qc_status") not in {"passed","passed_with_declared_limitations"}
                or (approval_path.is_file() and approval.get("completion_sha256") != _file_record(completion)["sha256"])):
                raise ValueError("Analysis completion/source manifest or scientific QC is not approved")
            snapshots = completed.get("executed_source_snapshots", [])
            manifest_hashes = {r["path"]: r["sha256"] for r in completed.get("files", [])}
            if (not snapshots or run_metadata.get("script_sha256") not in {r.get("sha256") for r in snapshots}
                or any(manifest_hashes.get(r.get("path")) != r.get("sha256") for r in snapshots)):
                raise ValueError("Executed source bytes are not frozen in the completion manifest")
            producer_path = Path(approval.get("producer_status_path", ""))
            if not producer_path.is_file() or _file_record(producer_path)["sha256"] != approval.get("producer_status_sha256"):
                raise ValueError("Producer exit-status snapshot is missing or changed")
            producer = json.loads(producer_path.read_text(encoding="utf-8"))
            jobs = [job for job in producer.get("jobs", []) if job.get("name") == approval.get("producer_job")]
            if len(jobs) != 1:
                raise ValueError("Producer receipt does not prove this source/output completed successfully")
            job = jobs[0]
            source_names = {Path(r["path"]).name for r in snapshots
                            if r["sha256"] == run_metadata.get("script_sha256")}
            # Older immutable receipts recorded the executed basename and explicit
            # --output-dir, while the run/DONE froze source bytes. Preserve that
            # narrower provenance instead of fabricating a new historical receipt.
            source_matches = (job.get("script_sha256") == run_metadata.get("script_sha256")
                              or ("script_sha256" not in job and job.get("script") in source_names))
            outputs = list(job.get("outputs", []))
            arguments = job.get("arguments", [])
            outputs += [arguments[i + 1] for i, arg in enumerate(arguments[:-1]) if arg == "--output-dir"]
            if job.get("status") != "completed" or job.get("returncode") != 0 or not source_matches:
                raise ValueError("Producer receipt does not prove this source/output completed successfully")
            if producer_output_audit is not None:
                run_metadata["producer_output_audit"] = _verify_output_audit(
                    producer_output_audit, producer_output_audit_sha256, output_dir,
                    run_metadata, approval, accession,
                )
            # Contradicting recorded destinations cannot be overridden by a later audit.
            output_matches = any(_producer_output_matches(value, output_dir) for value in outputs)
            if not output_matches and (outputs or producer_output_audit is None):
                raise ValueError("Producer receipt does not prove this source/output completed successfully")
            run_metadata["producer_source_verification"] = ("receipt_sha256_and_frozen_source" if "script_sha256" in job
                                                            else "receipt_basename_and_frozen_source_sha256")
        for record in completed.get("files", []):
            path = (output_dir / record["path"]).resolve()
            if not path.is_relative_to(output_dir) or _file_record(path)["sha256"] != record["sha256"]:
                raise ValueError("Analysis completion marker has changed or unsafe files")
    result_records = {filename: _file_record(output_dir / filename) for filename in result_files}
    if any(not (output_dir / filename).is_file() for filename in result_files):
        missing = [name for name in result_files if not (output_dir / name).is_file()]
        raise FileNotFoundError(f"Analysis result files are missing: {', '.join(missing)}")

    auxiliary_files = run_metadata.get("auxiliary_files") or list(DEFAULT_AUXILIARY_FILES)
    if isinstance(auxiliary_files, str):
        auxiliary_files = [auxiliary_files]
    if not isinstance(auxiliary_files, list) or any(
        not isinstance(name, str) or not name or Path(name).name != name for name in auxiliary_files
    ):
        raise ValueError("Analysis auxiliary files must be basenames inside the output directory")
    output_manifest = [result_records[name] for name in result_files]
    output_manifest.extend(
        _file_record(output_dir / filename)
        for filename in auxiliary_files
        if (output_dir / filename).is_file()
    )
    output_manifest.append(_file_record(metadata_path))
    if completion.is_file():
        # Keep marker provenance outside output_manifest[-1], which guards metadata.
        run_metadata["completion_marker"] = _file_record(completion)
        approval_path=output_dir/"RELEASE_APPROVAL.json"
        if approval_path.is_file():
            run_metadata["release_approval"] = {**json.loads(approval_path.read_text(encoding="utf-8")),
                                               "manifest":_file_record(approval_path)}

    default_script = PROJECT_ROOT / "scripts" / f"analyze_{accession.lower()}.R"
    script_path = _provenance_path(run_metadata.get("script_path"), default_script)
    default_lock = PROJECT_ROOT.parent / "toolchain" / "r-environment-linux-64.lock.txt"
    lock_path = _provenance_path(run_metadata.get("environment_lock_path"), default_lock)
    script_record = _file_record(script_path) if script_path.is_file() else None
    lock_record = _file_record(lock_path) if lock_path.is_file() else None

    # Validate once before the transaction; a second streaming pass bounds memory.
    row_count = 0
    for filename in result_files:
        for row in _read_rows(output_dir / filename):
            if not row.get("feature_type") or not row.get("feature_id") or not row.get("contrast"):
                raise ValueError(f"Analysis result row lacks feature_type, feature_id, or contrast: {filename}")
            if not row["feature_type"].replace("_", "").isalnum():
                raise ValueError(f"Invalid feature_type {row['feature_type']!r} in {filename}")
            row_count += 1
    if not row_count:
        raise ValueError("No analysis result rows found")

    create_schema(database_url)
    session: Session = get_session_factory(database_url)()
    counts: dict[str, int] = {"inserted": 0, "updated": 0, "result_rows": 0}
    try:
        series = session.get(GeoSeries, accession)
        if series is None:
            raise ValueError(f"GEO Series {accession} must be imported before analysis results")
        series.assay_data_downloaded = True
        version = str(run_metadata.get("analysis_version", "unknown"))
        analysis_run = session.scalar(
            select(AnalysisRun).where(
                AnalysisRun.series_accession == accession,
                AnalysisRun.analysis_version == version,
            )
        )
        if analysis_run is None:
            analysis_run = AnalysisRun(
                series_accession=accession,
                analysis_version=version,
                status="completed",
            )
            session.add(analysis_run)
        previous_outputs = list(analysis_run.output_manifest_json or [])
        analysis_run.status = "completed"
        analysis_run.started_at = run_metadata.get("run_utc")
        analysis_run.source_script_path = str(script_path) if script_record else None
        # The run metadata is written at analysis time and is authoritative. The
        # path may now point to a subsequently edited script, so retain both
        # hashes instead of attributing the current file to an older run.
        recorded_script_sha256 = run_metadata.get("script_sha256")
        analysis_run.source_script_sha256 = (
            recorded_script_sha256
            if isinstance(recorded_script_sha256, str) and recorded_script_sha256
            else (script_record["sha256"] if script_record else None)
        )
        analysis_run.environment_lock_path = str(lock_path) if lock_record else None
        # Preserve the lock identity recorded at execution even if the live
        # project lock has subsequently changed for a new analysis version.
        recorded_lock_sha256 = run_metadata.get("environment_lock_sha256")
        analysis_run.environment_lock_sha256 = (
            recorded_lock_sha256
            if isinstance(recorded_lock_sha256, str) and recorded_lock_sha256
            else (lock_record["sha256"] if lock_record else None)
        )
        analysis_run.metadata_json = run_metadata
        if script_record is not None:
            analysis_run.metadata_json["script_file_current_sha256"] = script_record["sha256"]
        if lock_record is not None:
            analysis_run.metadata_json["environment_lock_file_current_sha256"] = lock_record["sha256"]
        analysis_run.input_manifest_json = run_metadata.get("input_manifest") or [
            {
                key: value
                for key, value in run_metadata.items()
                if key.startswith("input_") or key.endswith("_sha256")
            }
        ]
        analysis_run.output_manifest_json = output_manifest
        existing_keys = set(session.execute(select(
            StudyEffect.feature_type, StudyEffect.feature_id, StudyEffect.contrast
        ).where(StudyEffect.series_accession == accession, StudyEffect.analysis_version == version)))
        # A release version is immutable once published. Identical retries are safe.
        if analysis_run.id is not None:
            old_outputs = {Path(x["path"]).name: x.get("sha256") for x in previous_outputs}
            for name, record in result_records.items():
                if name in old_outputs and old_outputs[name] != record["sha256"]:
                    raise ValueError("Analysis version content changed; publish a new version")
        batch = []
        seen_keys = set()
        statement = sqlite_insert(StudyEffect.__table__)
        statement = statement.on_conflict_do_update(
            index_elements=["series_accession", "feature_type", "feature_id", "contrast", "analysis_version"],
            set_={column.name: getattr(statement.excluded, column.name)
                  for column in StudyEffect.__table__.columns
                  if column.name not in {"id", "series_accession", "feature_type", "feature_id", "contrast", "analysis_version"}},
        )
        # Flush the run once, then reuse one compiled Core executemany statement.
        # Expanding 500 rows into a fresh SQL expression per batch dominated large
        # CpG imports. Explicit dictionaries still preserve SQL NULL and JSON.
        session.flush()
        connection = session.connection()
        # Connection-local, bounded budget. Large CpG batches need enough cache
        # for multiple B-tree indexes; undersized caches repeatedly rewrite WAL
        # pages on the throttled server disk. No persistent/global setting changes.
        connection.exec_driver_sql(f"PRAGMA cache_size=-{cache_mib * 1024}")

        def flush_batch():
            if not batch:
                return
            connection.execute(statement, batch)
            batch.clear()

        default_scope = run_metadata.get("effect_scope") or run_metadata.get("interpretation_limit")
        if accession == "GSE127530" and not default_scope:
            default_scope = (
                "Cohort-level response to a mixed high-fat meal; not component-specific "
                "or an individual prediction."
            )
        for filename, row in ((name, row) for name in result_files for row in _read_rows(output_dir / name)):
            feature_type = row["feature_type"]
            feature_id = row["feature_id"]
            contrast = row["contrast"]
            key = (feature_type, feature_id, contrast)
            if key in seen_keys:
                raise ValueError(f"Duplicate effect key in analysis output: {key}")
            seen_keys.add(key)
            was_inserted = key not in existing_keys
            effect = {"series_accession":accession, "feature_type":feature_type,
                      "feature_id":feature_id, "contrast":contrast, "analysis_version":version}
            row_metadata = _metadata(row.get("metadata_json"))
            additional_metadata = {
                key: value
                for key, value in row.items()
                if key not in STANDARD_RESULT_COLUMNS and value not in {"", "NA"}
            }
            effect["feature_label"] = row.get("feature_label") or feature_id
            effect["tissue"] = row.get("tissue") or run_metadata.get("tissue") or "whole blood"
            effect["timepoint"] = row.get("timepoint")
            effect["intervention"] = row.get("intervention") or run_metadata.get("intervention")
            if not effect["intervention"] and accession == "GSE127530":
                effect["intervention"] = LEGACY_GSE127530_INTERVENTION
            effect["dose"] = row.get("dose") or run_metadata.get("dose")
            effect["duration"] = row.get("duration") or run_metadata.get("duration")
            if not effect["duration"] and accession == "GSE127530":
                effect["duration"] = "acute; fasting, 3 h, and 6 h"
            effect["effect_estimate"] = _optional_float(row.get("effect_estimate"))
            effect["standard_error"] = _optional_float(row.get("standard_error"))
            effect["p_value"] = _optional_float(row.get("p_value"))
            effect["q_value"] = _optional_float(row.get("q_value"))
            effect["effect_unit"] = row.get("effect_unit")
            effect["status"] = row.get("status") or "measured"
            effect["n_people"] = _optional_int(row.get("n_people")) or _optional_int(
                str(run_metadata.get("independent_people", ""))
            )
            effect["n_samples"] = _optional_int(row.get("n_samples")) or _optional_int(
                str(run_metadata.get("sample_count", ""))
            )
            effect["source_file"] = str(output_dir / filename)
            effect["metadata_json"] = {
                **row_metadata,
                **additional_metadata,
                "analysis_run_version": version,
                "source_filename": filename,
                "source_file_sha256": result_records[filename]["sha256"],
                "inference_scope": row_metadata.get("inference_scope") or default_scope,
            }
            batch.append(effect)
            if len(batch) >= 500:
                flush_batch()
            counts["inserted" if was_inserted else "updated"] += 1
            counts["result_rows"] += 1
            if counts["result_rows"] % 100000 == 0:
                print(json.dumps({"status": "importing_uncommitted", "accession": accession,
                                  "analysis_version": version, "rows": counts["result_rows"],
                                  "total_rows": row_count, "cache_mib": cache_mib}), flush=True)
            count_key = f"{feature_type}_effects"
            counts[count_key] = counts.get(count_key, 0) + 1
        flush_batch()
        # A producer must not rewrite outputs while a release is being loaded.
        for filename, record in result_records.items():
            if _file_record(output_dir / filename)["sha256"] != record["sha256"]:
                raise ValueError("Analysis output changed during import; transaction rolled back")
        if _file_record(metadata_path)["sha256"] != output_manifest[-1]["sha256"]:
            raise ValueError("Analysis metadata changed during import; transaction rolled back")
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load reproducible GEO gene, pathway, and methylation effects"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--accession", required=True)
    parser.add_argument("--require-completion", action="store_true", help="Mandatory for newly promoted analysis releases")
    parser.add_argument("--producer-output-audit", type=Path)
    parser.add_argument("--producer-output-audit-sha256")
    args = parser.parse_args()
    result = import_analysis_results(args.output_dir, args.database_url, args.accession, args.require_completion,
                                     args.producer_output_audit, args.producer_output_audit_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
