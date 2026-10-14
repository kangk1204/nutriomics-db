from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import create_schema, get_session_factory
from .models import AnalysisRun, GeoSample, GeoSeries, ResearchStudy, Resource
from .settings import DATABASE_URL, PROJECT_ROOT, RESEARCH_DIR

SAMPLE_START = re.compile(r"^\^SAMPLE\s*=\s*(GSM\d+)", re.IGNORECASE)
SAMPLE_FIELD = re.compile(r"^!Sample_([^=]+?)\s*=\s*(.*)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_soft_samples(path: Path) -> dict[str, dict[str, Any]]:
    samples: dict[str, dict[str, Any]] = {}
    current_id: str | None = None
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            start = SAMPLE_START.match(raw_line)
            if start:
                current_id = start.group(1).upper()
                samples.setdefault(current_id, {"characteristics": [], "metadata": {}})
                continue
            if current_id is None:
                continue
            field = SAMPLE_FIELD.match(raw_line)
            if not field:
                continue
            key = field.group(1).strip()
            value = field.group(2).strip()
            if key.casefold() == "title":
                samples[current_id]["title"] = value
            elif key.casefold().startswith("characteristics"):
                samples[current_id]["characteristics"].append(value)
                channel = re.search(r"_ch([12])$", key, re.IGNORECASE)
                if channel:
                    channel_key = f"ch{channel.group(1)}"
                    samples[current_id].setdefault("channel_characteristics", {}).setdefault(
                        channel_key, []
                    ).append(value)
            elif key.casefold() in {"platform_id", "channel_count"} or re.search(
                r"_ch[12]$", key, re.IGNORECASE
            ):
                samples[current_id]["metadata"].setdefault(key, []).append(value)
    return samples


def _series_fields(manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in manifest:
        if (
            isinstance(item, dict)
            and item.get("accession")
            and isinstance(item.get("fields"), dict)
        ):
            result.append(item)
    return result


def _field(fields: dict[str, Any], name: str, default: Any = None) -> Any:
    value = fields.get(name, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def _series_group_map(config_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    groups = json.loads(config_path.read_text(encoding="utf-8"))
    group_by_id = {row["id"]: row for row in groups}
    series_to_group = {
        accession: group["id"] for group in groups for accession in group.get("series", [])
    }
    return group_by_id, series_to_group


def _update_catalog_resource(
    resource: Resource, item: dict[str, Any], catalog: dict[str, Any], catalog_path: Path,
    catalog_hash: str,
) -> None:
    """Version planning metadata independently of downloaded-source provenance.

    Importers populate the authoritative source fields and add their manifests to
    metadata_json. A later planning catalog import must not demote those records
    back to a 'not downloaded / terms not checked' planning snapshot.
    """
    resource.name = item.get("name", item["id"])
    resource.layer = item.get("layer", "unknown")
    resource.priority = item.get("priority")
    resource.role_and_limits = item.get("role_and_limits")
    for field in ("source_url", "verification", "use_conditions"):
        if getattr(resource, field) is None:
            setattr(resource, field, item.get(field))
    metadata = dict(resource.metadata_json or {})
    snapshot = {
        "schema_version": catalog.get("schema_version"),
        "as_of": catalog.get("as_of"),
        "catalog_sha256": catalog_hash,
        "catalog_source_path": str(catalog_path),
        "record": dict(item),
    }
    history = list(metadata.get("catalog_history", []))
    previous = metadata.get("catalog_snapshot")
    if previous and previous.get("catalog_sha256") != catalog_hash and previous not in history:
        history.append(previous)
    metadata["catalog_snapshot"] = snapshot
    metadata["catalog_history"] = history
    # Retain old consumers' access to catalog-only extension fields, without
    # overwriting runtime importer keys even if a future catalog uses the same name.
    source_fields = {
        "id", "name", "layer", "priority", "source_url", "verification",
        "use_conditions", "role_and_limits", "release", "sha256", "local_path",
        "catalog_snapshot", "catalog_history",
    }
    for key, value in item.items():
        if key not in source_fields:
            metadata.setdefault(key, value)
    resource.metadata_json = metadata


def ingest_research_metadata(
    research_dir: Path = RESEARCH_DIR,
    config_path: Path | None = None,
    database_url: str = DATABASE_URL,
) -> dict[str, int]:
    research_dir = research_dir.expanduser().resolve()
    catalog_path = research_dir / "resource_catalog.json"
    manifest_path = research_dir / "geo_metadata_manifest.json"
    if config_path is None:
        config_path = PROJECT_ROOT / "config" / "study_groups.json"
    config_path = config_path.expanduser().resolve()
    for required in (catalog_path, manifest_path, config_path):
        if not required.is_file():
            raise FileNotFoundError(f"Required research input is missing: {required}")

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog_hash = sha256_file(catalog_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(manifest, dict):
        manifest = manifest.get("studies", manifest.get("series", []))
    if not isinstance(manifest, list):
        raise TypeError("GEO manifest must be a list of Series records")
    records = _series_fields(manifest)
    group_by_id, series_to_group = _series_group_map(config_path)

    create_schema(database_url)
    session: Session = get_session_factory(database_url)()
    counts = {"resources": 0, "research_studies": 0, "series": 0, "samples": 0}
    try:
        for item in catalog.get("resources", []):
            resource = session.get(Resource, item["id"])
            if resource is None:
                resource = Resource(id=item["id"])
                session.add(resource)
            _update_catalog_resource(resource, item, catalog, catalog_path, catalog_hash)
            counts["resources"] += 1

        for group_id, group in group_by_id.items():
            study = session.get(ResearchStudy, group_id)
            if study is None:
                study = ResearchStudy(id=group_id)
                session.add(study)
            study.title = group["title"]
            study.description = group["description"]
            study.pubmed_id = group.get("pubmed_id")
            study.independent_study = True
            study.metadata_json = {
                "series": group.get("series", []),
                "study_design": group.get("study_design"),
                "limitations": group.get("limitations"),
                "primary_sources": group.get("primary_sources", []),
                "series_design": group.get("series_design", {}),
            }
            counts["research_studies"] += 1

        session.flush()
        for record in records:
            accession = str(record["accession"]).upper()
            group_id = series_to_group.get(accession)
            if group_id is None:
                raise ValueError(f"No independent-study grouping configured for {accession}")
            fields = record["fields"]
            metadata_path = research_dir / "geo_metadata" / f"{accession}.soft.txt"
            if not metadata_path.is_file():
                raise FileNotFoundError(f"GEO SOFT source is missing: {metadata_path}")
            actual_hash = sha256_file(metadata_path)
            expected_hash = record.get("sha256")
            if expected_hash and actual_hash != expected_hash:
                raise ValueError(
                    f"GEO metadata hash mismatch for {accession}: "
                    f"expected {expected_hash}, got {actual_hash}"
                )

            series = session.get(GeoSeries, accession)
            if series is None:
                series = GeoSeries(accession=accession, study_id=group_id, title="")
                session.add(series)
            series.study_id = group_id
            series.parent_accession = "GSE54690" if accession in {"GSE54325", "GSE54643"} else None
            series.title = str(_field(fields, "title", accession))
            series.assay_type = str(_field(fields, "type", "")) or None
            platforms = fields.get("platform_id", [])
            series.platform_ids = platforms if isinstance(platforms, list) else [platforms]
            series.organism = (
                str(_field(fields, "sample_organism", _field(fields, "platform_organism", "")))
                or None
            )
            series.pubmed_id = str(_field(fields, "pubmed_id", "")) or None
            series.sample_count = record.get("geo_sample_record_count")
            series.metadata_verified = record.get("status") == "metadata_verified"
            recorded_assay_data = bool(record.get("assay_data_downloaded", False))
            existing_analysis = session.scalar(
                select(AnalysisRun.id)
                .where(AnalysisRun.series_accession == accession)
                .limit(1)
            )
            series.assay_data_downloaded = bool(
                recorded_assay_data or series.assay_data_downloaded or existing_analysis
            )
            series.metadata_json = {
                "summary": fields.get("summary", []),
                "overall_design": fields.get("overall_design", []),
                "submission_date": fields.get("submission_date", []),
                "last_update_date": fields.get("last_update_date", []),
                "supplementary_file": fields.get("supplementary_file", []),
                "relation": fields.get("relation", []),
                "metadata_sha256": actual_hash,
                "metadata_source_url": record.get("metadata_url"),
                "retrieved_utc": record.get("retrieved_utc"),
            }

            sample_records = parse_soft_samples(metadata_path)
            listed_ids = fields.get("sample_id", [])
            if not isinstance(listed_ids, list):
                listed_ids = [listed_ids]
            for sample_accession in listed_ids:
                sample_accession = str(sample_accession).upper()
                meta = sample_records.get(sample_accession, {})
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
                sample.title = meta.get("title")
                sample.characteristics = meta.get("characteristics", [])
                sample.metadata_json = meta.get("metadata", {})
                counts["samples"] += 1
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
        description="Import the collected research catalog and GEO metadata"
    )
    parser.add_argument("--research-dir", type=Path, default=RESEARCH_DIR)
    parser.add_argument(
        "--config", type=Path, default=PROJECT_ROOT / "config" / "study_groups.json"
    )
    parser.add_argument("--database-url", default=DATABASE_URL)
    args = parser.parse_args()
    result = ingest_research_metadata(args.research_dir, args.config, args.database_url)
    print(json.dumps({"status": "imported", **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
