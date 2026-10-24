"""Versioned, resumable public-data acquisition for the intervention bridge.

Files are downloaded into a dedicated expansion directory; production data are
never replaced. Source manifests include successful and failed requests. Small
metadata and raw assay files have separate, explicit budgets.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gzip
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

try:
    from scripts.direct_plus_source import (
        API,
        STUDY,
        SUCCESS,
        load_sdrf,
        mapping_identity,
        resolve_files,
        validate_raw_mapping,
    )
except ModuleNotFoundError:  # Direct script execution.
    from direct_plus_source import (
        API,
        STUDY,
        SUCCESS,
        load_sdrf,
        mapping_identity,
        resolve_files,
        validate_raw_mapping,
    )

SOURCES = {
    "GSE13466": {"series_matrix": "GSE13466_series_matrix.txt.gz", "raw": "GSE13466_RAW.tar"},
    "GSE98645": {"counts": "GSE98645_combined.counts.txt.gz"},
    "GSE27385": {"series_matrix": "GSE27385_series_matrix.txt.gz", "raw": "GSE27385_RAW.tar"},
}
BASE = "https://ftp.ncbi.nlm.nih.gov/geo/series"
EBI = "https://www.ebi.ac.uk/biostudies"
record_lock = threading.Lock()


class IncompleteDirectPlusAcquisition(RuntimeError):
    """Carries the complete failed-attempt accounting across the CLI boundary."""

    def __init__(self, result: dict):
        self.result = result
        super().__init__(f"DIRECT PLUS acquisition incomplete: {result['failed_count']} failed, "
                         f"{result['success_count']} verified of {result['expected_raw_files']}")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def fetch(url: str, destination: Path, limit: int, retries: int = 4, *,
          expected_size: int | None = None, source_identity: str | None = None,
          refresh: bool = False, expected_sha256: str | None = None) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    partial_receipt = partial.with_suffix(partial.suffix + ".json")
    identity = {"url": url, "expected_size": expected_size, "source_identity": source_identity}
    if expected_size is not None and (expected_size <= 0 or expected_size > limit):
        raise ValueError("Invalid expected file size/budget")
    if partial.exists() and source_identity is not None:
        prior = json.loads(partial_receipt.read_text()) if partial_receipt.exists() else {}
        if any(prior.get(k) != v for k, v in identity.items()):
            # Preserve unbound partial bytes instead of appending a different source.
            orphan = partial.with_name(partial.name + f".unbound.{time.time_ns()}")
            partial.replace(orphan)
    if destination.is_file() and not refresh:
        try:
            if expected_size is not None and destination.stat().st_size != expected_size:
                raise ValueError("Cached file size differs from official listing")
            if destination.stat().st_size > limit:
                raise ValueError("Cached file exceeds source budget")
            if destination.suffix == ".gz":
                with gzip.open(destination, "rb") as check:
                    while check.read(4 * 1024 * 1024):
                        pass
        except (OSError, ValueError, EOFError) as error:
            return {"url": url, "path": str(destination), "status": "failed",
                    "reason": "Cached source validation failed: " + str(error)}
        cached_sha = digest(destination)
        if expected_sha256 is not None and cached_sha != expected_sha256:
            return {"url": url, "path": str(destination), "status": "failed",
                    "reason": "Cached SHA256 differs from prior successful source-bound receipt"}
        return {"url": url, "path": str(destination), "bytes": destination.stat().st_size,
                "sha256": cached_sha, "status": "cached_verified",
                "expected_bytes": expected_size, "source_identity": source_identity,
                "integrity_scope": "Official byte size and local SHA256; no remote checksum asserted"}
    for attempt in range(retries):
        offset = partial.stat().st_size if partial.exists() else 0
        prior = json.loads(partial_receipt.read_text()) if partial_receipt.exists() else {}
        headers = {"User-Agent": "NutrigenomicsResearchDB/0.1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if prior.get("etag"):
                headers["If-Range"] = prior["etag"]
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                resumed = offset > 0 and response.status == 206
                if resumed:
                    content_range = response.headers.get("Content-Range", "")
                    if (not content_range.startswith(f"bytes {offset}-")
                            or (expected_size is not None and not content_range.endswith(f"/{expected_size}"))
                            or (prior.get("etag") and response.headers.get("ETag") != prior["etag"])):
                        raise ValueError("Server returned a mismatched range")
                else:
                    offset = 0
                length = int(response.headers.get("Content-Length", "0"))
                if expected_size is not None and length and offset + length != expected_size:
                    raise ValueError(f"Response byte count {offset + length} differs from official file listing {expected_size}")
                atomic_json(partial_receipt, {**identity, "etag": response.headers.get("ETag")})
                if length and offset + length > limit:
                    raise ValueError(f"Declared size {offset + length} exceeds source budget {limit}")
                if shutil.disk_usage(destination.parent).free < max(length, 1024 ** 3) + 2 * 1024 ** 3:
                    raise ValueError("Insufficient free disk for download and reserve")
                with partial.open("ab" if resumed else "wb") as output:
                    size = offset
                    for block in iter(lambda: response.read(1024 * 1024), b""):
                        size += len(block)
                        if size > limit:
                            raise ValueError("Stream exceeded source budget")
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if length and size != offset + length:
                    raise OSError("Truncated download")
                if expected_size is not None and size != expected_size:
                    raise OSError("Completed stream differs from expected size")
                if destination.suffix == ".gz":
                    with gzip.open(partial, "rb") as check:
                        while check.read(4 * 1024 * 1024):
                            pass
                if destination.exists() and refresh:
                    previous_sha = digest(destination)
                    if previous_sha != digest(partial):
                        archive = destination.with_name(destination.name + f".source-{previous_sha}")
                        if not archive.exists():
                            shutil.copy2(destination, archive)
                partial.replace(destination)
                return {"url": url, "path": str(destination), "bytes": size,
                        "sha256": digest(destination), "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"), "status": "downloaded",
                        "expected_bytes": expected_size, "source_identity": source_identity,
                        "integrity_scope": "Official byte size and local SHA256; no remote checksum asserted"}
        except (OSError, ValueError, EOFError, urllib.error.URLError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code in (401, 403, 404):
                return {"url": url, "status": "unavailable", "reason": str(error)}
            if attempt == retries - 1:
                return {"url": url, "status": "failed", "reason": str(error)}
            time.sleep(min(2 ** attempt, 8))
    raise AssertionError("unreachable")


def soft_samples(path: Path) -> list[dict]:
    samples, record = [], None
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if line.startswith("^SAMPLE = "):
                record = {"sample_accession": line.split(" = ", 1)[1]}
                samples.append(record)
            elif line.startswith("^"):
                record = None
            elif record is not None and line.startswith("!Sample_") and " = " in line:
                key, value = line.split(" = ", 1)
                record.setdefault(key.removeprefix("!Sample_"), []).append(value)
    return samples


def write_sample_audit(series: str, directory: Path) -> None:
    archive = directory / f"{series}_family.soft.gz"
    records = soft_samples(archive)
    atomic_json(directory / "sample_metadata.json", records)
    atomic_json(directory / "sample_metadata_provenance.json", {
        "source_url": f"{BASE}/{series[:-3]}nnn/{series}/soft/{series}_family.soft.gz",
        "source_file": str(archive), "source_sha256": digest(archive),
        "source_sample_count": len(records),
        "parsed_output": str(directory / "sample_metadata.json"),
        "parsed_sha256": digest(directory / "sample_metadata.json"),
        "method": "Sample-level key/value parsing of explicit ^SAMPLE sections in frozen original GEO family SOFT",
        "matched_GSMs": [row["sample_accession"] for row in records],
    })
    with (directory / "sample_audit.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["sample_accession", "title", "source", "characteristics_json", "supplementary_json", "inclusion_status", "reason"])
        for row in records:
            writer.writerow([row["sample_accession"], "|".join(row.get("title", [])), "|".join(row.get("source_name_ch1", [])),
                             json.dumps(row.get("characteristics_ch1", [])), json.dumps(row.get("supplementary_file", [])),
                             "pending_design_audit", "Do not infer person/arm/time without validated metadata"])


def all_files(value: object) -> list[dict]:
    output = []
    if isinstance(value, dict):
        if "path" in value and isinstance(value["path"], str):
            output.append(value)
        for child in value.values():
            output.extend(all_files(child))
    elif isinstance(value, list):
        for child in value:
            output.extend(all_files(child))
    return output


def direct_plus_audit(directory: Path) -> dict:
    rows, result = load_sdrf(directory / f"{STUDY}.sdrf.txt")
    atomic_json(directory / "sample_audit.json", result)
    with (directory / "sample_design.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["sample_id", "subject_id", "arm", "time_months", "raw_file", "inclusion_status", "reason"])
        for row in rows:
            writer.writerow([row["Source Name"], row["Characteristics[individual]"],
                             row["Factor Value[diet]"], row["Factor Value[time]"],
                             row["Array Data File"], "included_metadata",
                             "Explicit SDRF pair/channel mapping; raw QC pending"])
    return result


def acquire_direct_plus(directory: Path, record, *, raw: bool, raw_budget: int,
                        workers: int = 8) -> dict:
    """A metadata-only run is explicit; --raw succeeds only with every source file."""
    def required(url, path, budget, **kwargs):
        result = record(url, path, budget, **kwargs)
        if result["status"] not in SUCCESS:
            raise RuntimeError(f"Required DIRECT PLUS source unavailable: {url}; {result.get('reason')}")
        return result

    required(f"https://www.ebi.ac.uk/biostudies/api/v1/studies/{STUDY}",
             directory / "study_metadata.json", 16 * 1024 ** 2, refresh=True)
    required(API + "/info", directory / "study_info.json", 1024 ** 2, refresh=True)
    info = json.loads((directory / "study_info.json").read_text(encoding="utf-8"))

    def get_page(offset, size):
        path = directory / "file_pages" / f"files_{offset:05d}.json"
        required(API + f"/files?offset={offset}&limit={size}", path, 8 * 1024 ** 2, refresh=True)
        return json.loads(path.read_text(encoding="utf-8"))

    files = resolve_files(info, get_page)
    required(API + "/info", directory / "study_info_after_listing.json", 1024 ** 2,
             refresh=True)
    after = json.loads((directory / "study_info_after_listing.json").read_text(encoding="utf-8"))
    if any(info.get(k) != after.get(k) for k in ("httpLink", "modified", "files", "sectionFileCounts")):
        raise RuntimeError("Study changed while resolving file pages; restart metadata acquisition")
    identity = mapping_identity(info, files)
    for entry in files:
        if entry["section"] != "raw-data":
            required(entry["url"], directory / entry["name"], entry["size"],
                     expected_size=entry["size"], source_identity=identity, refresh=True)
    audit = direct_plus_audit(directory)
    mapping = validate_raw_mapping(files, audit)
    resolved = {"accession": STUDY, "source_identity": identity,
                "source_info_sha256": digest(directory / "study_info.json"),
                "sdrf_sha256": digest(directory / f"{STUDY}.sdrf.txt"),
                "files": files, "counts": mapping,
                "terms_status": "Original study metadata and pre-existing rights retained; public access is not redistribution permission"}
    prior_path = directory / "resolved_files.json"
    if prior_path.exists():
        old_sha = digest(prior_path)
        archive = directory / f"resolved_files.{old_sha}.json"
        if not archive.exists():
            shutil.copy2(prior_path, archive)
    atomic_json(prior_path, resolved)
    if mapping["raw_bytes"] > raw_budget:
        raise ValueError("DIRECT PLUS aggregate IDAT bytes exceed raw budget")
    if not raw:
        result = {**mapping, "status": "metadata_only_raw_not_requested", "source_identity": identity}
    else:
        if shutil.disk_usage(directory).free < mapping["raw_bytes"] + 2 * 1024 ** 3:
            # Existing verified files do not need additional disk space.
            remaining = sum(f["size"] for f in files if f["section"] == "raw-data"
                            and not (directory / "idat" / f["name"]).is_file())
            if shutil.disk_usage(directory).free < remaining + 2 * 1024 ** 3:
                raise ValueError("Insufficient disk for planned raw files and reserve")
        def download(entry):
            path = directory / "idat" / entry["name"]
            try:
                result = record(entry["url"], path, entry["size"],
                                expected_size=entry["size"], source_identity=identity)
            except Exception as error:  # noqa: BLE001 -- Isolate and record each failed source; collect every queued file.
                result = {"url": entry["url"], "path": str(path), "status": "failed",
                          "reason": f"record_exception:{type(error).__name__}: {error}"}
            result = {**result, "name": entry["name"], "path": str(path),
                      "expected_bytes": entry["size"], "source_identity": identity}
            if result["status"] in SUCCESS and result.get("bytes") != entry["size"]:
                result = {**result, "status": "failed", "reason": "Receipt byte count differs from official listing"}
            return result
        raw_files = [f for f in files if f["section"] == "raw-data"]
        attempt_id = str(time.time_ns())
        attempt_dir = directory / "raw_attempts"
        attempt_dir.mkdir(exist_ok=True)
        checkpoint_path = attempt_dir / f"in_progress.{attempt_id}.json"
        receipts = [{"name": f["name"], "url": f["url"], "expected_bytes": f["size"],
                     "status": "not_attempted"} for f in raw_files]

        def attempt_snapshot():
            success = sum(r["status"] in SUCCESS for r in receipts)
            pending = sum(r["status"] == "not_attempted" for r in receipts)
            return {"attempt_id": attempt_id, "source_identity": identity,
                    "expected_raw_files": len(raw_files), "attempted_count": len(raw_files)-pending,
                    "success_count": success, "failed_count": len(raw_files)-pending-success,
                    "not_attempted_count": pending,
                    "failed_names": [r["name"] for r in receipts if r["status"] not in SUCCESS | {"not_attempted"}],
                    "complete": success == len(raw_files), "records": receipts}

        atomic_json(checkpoint_path, attempt_snapshot())
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
            # Every worker returns its own failure. A bad source object must not
            # close map's iterator and cancel the remaining queued source files.
            for index, receipt in enumerate(executor.map(download, raw_files)):
                receipts[index] = receipt
                if (index+1) % 16 == 0:
                    atomic_json(checkpoint_path, attempt_snapshot())
        attempt = attempt_snapshot()
        attempt_hash = hashlib.sha256(json.dumps(attempt, sort_keys=True).encode()).hexdigest()
        attempt_path = attempt_dir / f"attempt.{attempt_id}.{attempt_hash}.json"
        atomic_json(attempt_path, attempt)
        atomic_json(directory / "raw_attempt_receipts.json", attempt)
        atomic_json(checkpoint_path, {**attempt, "final_receipt_path": str(attempt_path)})
        accounting = {k: attempt[k] for k in ("attempt_id", "expected_raw_files", "attempted_count",
            "success_count", "failed_count", "not_attempted_count", "failed_names", "complete")}
        accounting.update({"attempt_receipt_path": str(attempt_path),
                           "attempt_receipt_sha256": digest(attempt_path)})
        if not attempt["complete"]:
            result = {**mapping, **accounting, "status": "failed_incomplete", "source_identity": identity,
                      "reason": "All raw files attempted; at least one source failed strict validation"}
            atomic_json(directory / "acquisition_status.json", result)
            raise IncompleteDirectPlusAcquisition(result)
        # Completion is a hash-bound receipt, never inferred from an absent legacy file list.
        atomic_json(directory / "raw_receipts.json", receipts)
        result = {**mapping, **accounting, "status": "raw_complete_local_sha256_verified",
                  "source_identity": identity, "raw_receipts_sha256": digest(directory / "raw_receipts.json")}
    atomic_json(directory / "acquisition_status.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--studies", nargs="+", default=list(SOURCES) + ["E-MTAB-12527"])
    parser.add_argument("--raw", action="store_true", help="Include CEL/IDAT; metadata and processed files are always fetched")
    parser.add_argument("--raw-budget-gib", type=int, default=120)
    parser.add_argument("--download-workers", type=int, default=8)
    options = parser.parse_args()
    root = options.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"files": []}
    manifest.setdefault("usage", "Noncommercial research; archive-specific conditions retained; no article full-text redistribution")
    manifest.update({"updated_utc": datetime.now(UTC).isoformat(), "raw_budget_bytes": options.raw_budget_gib * 1024 ** 3})

    def record(url: str, path: Path, budget: int, **kwargs) -> dict:
        records_dir = root / "download_records"
        records_dir.mkdir(exist_ok=True)
        record_path = records_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        old_receipt = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
        if kwargs.get("source_identity") is not None and path.exists() and not kwargs.get("refresh"):
            known = [old_receipt] + list(reversed(old_receipt.get("attempt_history", [])))
            prior = next((r for r in known if r.get("source_identity") == kwargs["source_identity"]
                          and r.get("status") in SUCCESS and r.get("sha256")), None)
            if prior is None:
                # A same-size file from another or unknown release cannot be rebound by name.
                kwargs["refresh"] = True
            else:
                kwargs["expected_sha256"] = prior["sha256"]
        result = fetch(url, path, budget, **kwargs)
        # Per-URL durable records avoid losing provenance when independent source
        # downloads run concurrently. The aggregate is a derived index.
        with record_lock:
            old = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
            history = old.get("attempt_history", [])
            if old:
                history = history + [{k: v for k, v in old.items() if k != "attempt_history"}]
            merged = {**old, **result, "attempt_history": history}
            atomic_json(record_path, merged)
            existing = {r["url"]: r for r in manifest.get("files", []) if "url" in r}
            for p in records_dir.glob("*.json"):
                row = json.loads(p.read_text(encoding="utf-8"))
                existing[row["url"]] = {**existing.get(row["url"], {}), **row}
            manifest["files"] = list(existing.values())
            atomic_json(manifest_path, manifest)
        print(json.dumps({k: v for k, v in result.items() if k in {"url", "status", "bytes", "reason"}}), flush=True)
        return result

    for study in options.studies:
        directory = root / "raw" / study
        if study in SOURCES:
            prefix = study[:-3] + "nnn"
            base = f"{BASE}/{prefix}/{study}"
            soft = record(f"{base}/soft/{study}_family.soft.gz", directory / f"{study}_family.soft.gz", 256 * 1024 ** 2)
            if soft["status"] in {"downloaded", "cached_verified"}:
                write_sample_audit(study, directory)
            for kind, filename in SOURCES[study].items():
                if kind == "raw" and not options.raw:
                    continue
                segment = "matrix" if kind == "series_matrix" else "suppl"
                record(f"{base}/{segment}/{filename}", directory / filename,
                       options.raw_budget_gib * 1024 ** 3 if kind == "raw" else 2 * 1024 ** 3)
        elif study == STUDY:
            try:
                result = acquire_direct_plus(directory, record, raw=options.raw,
                                             raw_budget=options.raw_budget_gib * 1024 ** 3,
                                             workers=options.download_workers)
            except Exception as error:
                directory.mkdir(parents=True, exist_ok=True)
                details = error.result if isinstance(error, IncompleteDirectPlusAcquisition) else {}
                atomic_json(directory / "acquisition_status.json", {**details,
                    "status": "failed_incomplete", "reason": str(error), "complete": False,
                    "raw_requested": options.raw, "updated_utc": datetime.now(UTC).isoformat()})
                raise
            print(json.dumps(result), flush=True)
        else:
            raise ValueError(f"Unregistered source {study}")


if __name__ == "__main__":
    main()
