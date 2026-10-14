from __future__ import annotations

import argparse
import hashlib
import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import GeoSeries
from nutrigenomics.settings import DATA_DIR, DATABASE_URL


RAW_ARCHIVE_KINDS = {"raw_array_archive", "raw_methylation_archive"}


GEO_FILES = [
    {
        "accession": "GSE127530",
        "name": "GSE127530_family.soft.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE127nnn/GSE127530/soft/GSE127530_family.soft.gz",
        "kind": "geo_family_soft",
    },
    {
        "accession": "GSE56960",
        "name": "GSE56960_family.soft.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE56nnn/GSE56960/soft/GSE56960_family.soft.gz",
        "kind": "geo_family_soft",
    },
    {
        "accession": "GSE54325",
        "name": "GSE54325_family.soft.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE54nnn/GSE54325/soft/GSE54325_family.soft.gz",
        "kind": "geo_family_soft",
    },
    {
        "accession": "GSE54643",
        "name": "GSE54643_family.soft.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE54nnn/GSE54643/soft/GSE54643_family.soft.gz",
        "kind": "geo_family_soft",
    },
    {
        "accession": "GSE54690",
        "name": "GSE54690_family.soft.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE54nnn/GSE54690/soft/GSE54690_family.soft.gz",
        "kind": "geo_family_soft",
    },
    {
        "accession": "GSE107205",
        "name": "GSE107205_family.soft.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE107nnn/GSE107205/soft/GSE107205_family.soft.gz",
        "kind": "geo_family_soft",
    },
    {
        "accession": "GSE219217",
        "name": "GSE219217_family.soft.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE219nnn/GSE219217/soft/GSE219217_family.soft.gz",
        "kind": "geo_family_soft",
    },
    {
        "accession": "GSE54325",
        "name": "GSE54325_RAW.tar",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE54nnn/GSE54325/suppl/GSE54325_RAW.tar",
        "kind": "raw_array_archive",
    },
    {
        "accession": "GSE54643",
        "name": "GSE54643_RAW.tar",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE54nnn/GSE54643/suppl/GSE54643_RAW.tar",
        "kind": "raw_methylation_archive",
    },
    {
        "accession": "GSE54643",
        "name": "GSE54643_Matrix_signal_intensities.txt.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE54nnn/GSE54643/suppl/GSE54643_Matrix_signal_intensities.txt.gz",
        "kind": "processed_methylation_matrix",
    },
    {
        "accession": "GSE107205",
        "name": "GSE107205_RAW.tar",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE107nnn/GSE107205/suppl/GSE107205_RAW.tar",
        "kind": "raw_methylation_archive",
    },
    {
        "accession": "GSE107205",
        "name": "GSE107205_Raw_matrix_ordered.txt.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE107nnn/GSE107205/suppl/GSE107205_Raw_matrix_ordered.txt.gz",
        "kind": "processed_methylation_matrix",
    },
    {
        "accession": "GSE219217",
        "name": "GSE219217_RAW.tar",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE219nnn/GSE219217/suppl/GSE219217_RAW.tar",
        "kind": "raw_methylation_archive",
    },
    {
        "accession": "GSE127530",
        "name": "GSE127530_combinedCounts.txt.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE127nnn/GSE127530/suppl/GSE127530_combinedCounts.txt.gz",
        "kind": "processed_rnaseq_counts",
    },
    {
        "accession": "GSE127530",
        "name": "GSE127530_fixed_combinedCounts.txt.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE127nnn/GSE127530/suppl/GSE127530_fixed_combinedCounts.txt.gz",
        "kind": "processed_rnaseq_counts",
    },
    {
        "accession": "GSE56960",
        "name": "GSE56960_series_matrix.txt.gz",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE56nnn/GSE56960/matrix/GSE56960_series_matrix.txt.gz",
        "kind": "processed_microarray_matrix",
    },
    {
        "accession": "GSE56960",
        "name": "GSE56960_RAW.tar",
        "url": "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE56nnn/GSE56960/suppl/GSE56960_RAW.tar",
        "kind": "raw_array_archive",
    },
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "nutrigenomics-research-db/0.1 (public GEO data retrieval)"})
    return session


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "1", "files": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("files", [])
    return data


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _validate_payload(path: Path, name: str) -> None:
    with path.open("rb") as handle:
        magic = handle.read(8)
    if name.endswith(".gz") and magic[:2] != b"\x1f\x8b":
        raise ValueError(f"Downloaded file is not gzip data: {name}")
    if name.endswith(".tar"):
        if not tarfile.is_tarfile(path):
            raise ValueError(f"Downloaded file is not a valid TAR archive: {name}")


def _mark_downloaded(accession: str, database_url: str) -> None:
    create_schema(database_url)
    session = get_session_factory(database_url)()
    try:
        row = session.get(GeoSeries, accession)
        if row is not None:
            row.assay_data_downloaded = True
            session.commit()
    finally:
        session.close()


def download_files(
    data_dir: Path = DATA_DIR,
    include_raw: bool = True,
    max_raw_gb: float = 8.0,
    database_url: str = DATABASE_URL,
) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    manifest_path = data_dir / "raw" / "geo" / "download_manifest.json"
    manifest = _read_manifest(manifest_path)
    existing = {row.get("name"): row for row in manifest["files"] if row.get("name")}
    statuses: list[dict[str, Any]] = []
    session = _session()
    try:
        for item in GEO_FILES:
            if item["kind"] in RAW_ARCHIVE_KINDS and not include_raw:
                statuses.append({"name": item["name"], "status": "skipped_by_option"})
                continue
            destination_dir = data_dir / "raw" / "geo" / item["accession"]
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / item["name"]
            relative = destination.relative_to(data_dir).as_posix()
            current_record = existing.get(item["name"])
            if destination.exists():
                if current_record and current_record.get("sha256") == _sha256(destination):
                    statuses.append({"name": item["name"], "status": "already_verified", "sha256": current_record["sha256"]})
                    continue
                raise FileExistsError(
                    f"Existing file is not verified by this manifest; preserving it: {destination}"
                )

            head = session.head(item["url"], allow_redirects=True, timeout=(15, 45))
            head.raise_for_status()
            size_header = head.headers.get("Content-Length")
            expected_size = int(size_header) if size_header and size_header.isdigit() else None
            if item["kind"] in RAW_ARCHIVE_KINDS and expected_size is not None:
                if expected_size > max_raw_gb * 1024**3:
                    statuses.append(
                        {
                            "name": item["name"],
                            "status": "skipped_size_limit",
                            "bytes": expected_size,
                            "max_raw_gb": max_raw_gb,
                        }
                    )
                    continue

            temporary = destination.with_name(destination.name + ".part")
            digest = hashlib.sha256()
            total = 0
            try:
                with session.get(item["url"], stream=True, allow_redirects=True, timeout=(15, 120)) as response:
                    response.raise_for_status()
                    with temporary.open("xb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            digest.update(chunk)
                            total += len(chunk)
                if expected_size is not None and total != expected_size:
                    raise IOError(f"Content length mismatch for {item['name']}: expected {expected_size}, received {total}")
                _validate_payload(temporary, item["name"])
                checksum = digest.hexdigest()
                os.replace(temporary, destination)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

            record = {
                **item,
                "status": "downloaded",
                "retrieved_utc": datetime.now(timezone.utc).isoformat(),
                "relative_path": relative,
                "bytes": total,
                "sha256": checksum,
                "content_type": head.headers.get("Content-Type"),
            }
            manifest["files"] = [row for row in manifest["files"] if row.get("name") != item["name"]]
            manifest["files"].append(record)
            _atomic_json(manifest_path, manifest)
            if item["kind"] != "geo_family_soft":
                _mark_downloaded(item["accession"], database_url)
            statuses.append({"name": item["name"], "status": "downloaded", "bytes": total, "sha256": checksum})
    finally:
        session.close()
    return {"manifest": str(manifest_path), "files": statuses}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download public GEO family metadata and selected processed/raw assay data")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--no-raw", action="store_true", help="skip raw microarray and methylation archives")
    parser.add_argument("--max-raw-gb", type=float, default=8.0)
    parser.add_argument("--database-url", default=DATABASE_URL)
    args = parser.parse_args()
    print(
        json.dumps(
            download_files(args.data_dir, not args.no_raw, args.max_raw_gb, args.database_url),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

