from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.ingest import parse_soft_samples
from nutrigenomics.models import GeoSample, GeoSeries
from nutrigenomics.settings import DATA_DIR, DATABASE_URL

ACCESSIONS = ("GSE127530", "GSE56960", "GSE54325", "GSE54643", "GSE54690", "GSE107205", "GSE219217")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def import_family_soft(
    data_dir: Path = DATA_DIR, database_url: str = DATABASE_URL
) -> dict[str, int]:
    data_dir = data_dir.expanduser().resolve()
    geo_dir = data_dir / "raw" / "geo"
    manifest_path = geo_dir / "download_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"GEO download manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = {row.get("name"): row for row in manifest.get("files", []) if row.get("name")}
    create_schema(database_url)
    session: Session = get_session_factory(database_url)()
    counts = {"series": 0, "samples": 0, "empty_series_files": 0}
    try:
        for accession in ACCESSIONS:
            filename = f"{accession}_family.soft.gz"
            path = geo_dir / accession / filename
            record = records.get(filename)
            if not path.is_file() or not record:
                raise FileNotFoundError(f"GEO family SOFT or manifest record missing: {path}")
            digest = _sha256(path)
            if digest != record.get("sha256"):
                raise ValueError(f"GEO family SOFT checksum mismatch for {accession}")
            series = session.get(GeoSeries, accession)
            if series is None:
                raise ValueError(f"Import GEO series metadata before family SOFT: {accession}")
            samples = parse_soft_samples(path)
            if not samples:
                counts["empty_series_files"] += 1
            for sample_accession, payload in samples.items():
                sample = session.scalar(
                    select(GeoSample).where(
                        GeoSample.series_accession == accession,
                        GeoSample.sample_accession == sample_accession,
                    )
                )
                if sample is None:
                    sample = GeoSample(
                        series_accession=accession, sample_accession=sample_accession
                    )
                    session.add(sample)
                sample.title = payload.get("title")
                sample.characteristics = payload.get("characteristics", [])
                sample.metadata_json = {
                    **(payload.get("metadata") or {}),
                    "channel_characteristics": payload.get("channel_characteristics", {}),
                    "source_url": record.get("url"),
                    "source_sha256": digest,
                    "retrieved_utc": record.get("retrieved_utc"),
                }
                counts["samples"] += 1
            if samples:
                series.sample_count = len(samples)
            series.metadata_json = {
                **(series.metadata_json or {}),
                "family_soft_sha256": digest,
                "family_soft_source_url": record.get("url"),
                "family_soft_sample_count": len(samples),
            }
            counts["series"] += 1
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import sample-level GEO metadata from family SOFT files"
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--database-url", default=DATABASE_URL)
    args = parser.parse_args()
    print(
        json.dumps(
            import_family_soft(args.data_dir, args.database_url), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
