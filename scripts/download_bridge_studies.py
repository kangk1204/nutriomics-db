"""Versioned, resumable public-data acquisition for the intervention bridge.

Files are downloaded into a dedicated expansion directory; production data are
never replaced. Source manifests include successful and failed requests. Small
metadata and raw assay files have separate, explicit budgets.
"""
from __future__ import annotations

import argparse
import csv
import concurrent.futures
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import time
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCES = {
    "GSE13466": {"series_matrix": "GSE13466_series_matrix.txt.gz", "raw": "GSE13466_RAW.tar"},
    "GSE98645": {"counts": "GSE98645_combined.counts.txt.gz"},
    "GSE27385": {"series_matrix": "GSE27385_series_matrix.txt.gz", "raw": "GSE27385_RAW.tar"},
}
BASE = "https://ftp.ncbi.nlm.nih.gov/geo/series"
EBI = "https://www.ebi.ac.uk/biostudies"
record_lock = threading.Lock()


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


def fetch(url: str, destination: Path, limit: int, retries: int = 4) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    if destination.is_file():
        try:
            if destination.stat().st_size > limit:
                raise ValueError("Cached file exceeds source budget")
            if destination.suffix == ".gz":
                with gzip.open(destination, "rb") as check:
                    while check.read(4 * 1024 * 1024):
                        pass
        except (OSError, ValueError, EOFError) as error:
            return {"url": url, "path": str(destination), "status": "failed",
                    "reason": "Cached source validation failed: " + str(error)}
        return {"url": url, "path": str(destination), "bytes": destination.stat().st_size,
                "sha256": digest(destination), "status": "cached_verified"}
    for attempt in range(retries):
        offset = partial.stat().st_size if partial.exists() else 0
        request = urllib.request.Request(url, headers={"User-Agent": "NutrigenomicsResearchDB/0.1", **({"Range": f"bytes={offset}-"} if offset else {})})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                resumed = offset > 0 and response.status == 206
                if resumed:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-"):
                        raise ValueError("Server returned a mismatched range")
                else:
                    offset = 0
                length = int(response.headers.get("Content-Length", "0"))
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
                    raise IOError("Truncated download")
                if destination.suffix == ".gz":
                    with gzip.open(partial, "rb") as check:
                        while check.read(4 * 1024 * 1024):
                            pass
                partial.replace(destination)
                return {"url": url, "path": str(destination), "bytes": size,
                        "sha256": digest(destination), "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"), "status": "downloaded"}
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
    with (directory / "E-MTAB-12527.sdrf.txt").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    columns = list(rows[0])
    individual = next(c for c in columns if c.lower() == "characteristics[individual]")
    time_col = next(c for c in columns if "time" in c.lower() and c.startswith("Factor Value"))
    arm_col = next(c for c in columns if "diet" in c.lower() and c.startswith("Factor Value"))
    groups, seen, people = {}, set(), {}
    raw_records = []
    raw_col = next(c for c in columns if c.lower() == "array data file")
    with (directory / "sample_design.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(["sample_id", "subject_id", "arm", "time_months", "raw_file", "inclusion_status", "reason"])
        for row in rows:
            person, arm, t = row[individual], row[arm_col], row[time_col]
            time_match = re.search(r"\d+", t)
            if not time_match:
                raise ValueError(f"Unrecognized timepoint {t!r}")
            month = int(time_match.group())
            sample = row["Source Name"]
            key = (person, month)
            people.setdefault(person, {})[month] = arm
            writer.writerow([sample, person, arm, month, row[raw_col], "included_metadata", "SDRF explicit individual, diet, time; raw QC pending"])
            raw_records.append(row[raw_col])
            if key not in seen:
                groups[arm] = groups.get(arm, 0) + 1
                seen.add(key)
    incomplete = {p: v for p, v in people.items() if set(v) != {0, 18} or len(set(v.values())) != 1}
    result = {"accession": "E-MTAB-12527", "independent_people": len(people), "biological_samples": len(seen),
              "sdrf_rows": len(rows), "raw_channel_files": len(set(raw_records)), "arm_samples": groups,
              "incomplete_pairs": incomplete, "status": "metadata_audited_raw_qc_pending",
              "interpretation": "IDAT red/green channels are not independent biological samples; RNA availability not established"}
    atomic_json(directory / "sample_audit.json", result)
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
    manifest.update({"updated_utc": datetime.now(timezone.utc).isoformat(), "raw_budget_bytes": options.raw_budget_gib * 1024 ** 3,
                     "usage": "Noncommercial research; archive-specific conditions retained; no article full-text redistribution"})

    def record(url: str, path: Path, budget: int) -> dict:
        result = fetch(url, path, budget)
        # Per-URL durable records avoid losing provenance when independent source
        # downloads run concurrently. The aggregate is a derived index.
        records_dir = root / "download_records"
        records_dir.mkdir(exist_ok=True)
        atomic_json(records_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json"), result)
        with record_lock:
            manifest["files"] = [json.loads(p.read_text()) for p in records_dir.glob("*.json")]
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
        elif study == "E-MTAB-12527":
            record(f"{EBI}/api/v1/studies/{study}", directory / "study_metadata.json", 16 * 1024 ** 2)
            record(f"{EBI}/files/{study}/{study}.sdrf.txt", directory / f"{study}.sdrf.txt", 16 * 1024 ** 2)
            record(f"{EBI}/files/{study}/{study}.idf.txt", directory / f"{study}.idf.txt", 16 * 1024 ** 2)
            if (directory / f"{study}.sdrf.txt").exists():
                print(json.dumps(direct_plus_audit(directory)), flush=True)
            metadata = json.loads((directory / "study_metadata.json").read_text())
            filelist_path = next((f["path"] for f in all_files(metadata) if f["path"].endswith("raw-data_filelist.json")), None)
            if not filelist_path and '"value": "raw-data_filelist.json"' in json.dumps(metadata):
                filelist_path = "raw-data_filelist.json"
            if filelist_path:
                result = record(f"{EBI}/files/{study}/{filelist_path}", directory / "raw-data_filelist.json", 16 * 1024 ** 2)
                if options.raw and result["status"] in {"downloaded", "cached_verified"}:
                    listing = json.loads((directory / "raw-data_filelist.json").read_text())
                    files = {f["path"]: f for f in all_files(listing) if f["path"].lower().endswith(".idat")}
                    planned_size = sum(int(f.get("size", 0)) for f in files.values())
                    if planned_size > options.raw_budget_gib * 1024 ** 3:
                        raise ValueError("DIRECT PLUS total IDAT size exceeds aggregate source budget")
                    def download_idat(item: tuple[str, dict]) -> None:
                        path, info = item
                        safe_name = Path(path).name
                        result = record(f"{EBI}/files/{study}/{path}", directory / "idat" / safe_name,
                                        max(int(info.get("size", 0)) * 2, 256 * 1024 ** 2))
                        if result["status"] not in {"downloaded", "cached_verified"}:
                            raise RuntimeError("IDAT request failed; resumable manifest retained")
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(options.download_workers, 8))) as executor:
                        list(executor.map(download_idat, files.items()))
        else:
            raise ValueError(f"Unregistered source {study}")


if __name__ == "__main__":
    main()
