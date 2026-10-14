from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import Component, CompositionMeasurement, Food, Resource
from nutrigenomics.settings import DATA_DIR, DATABASE_URL

RESOURCE_ID = "R03"
RESOURCE_NAME = "USDA FoodData Central"
DATASETS = [
    {
        "name": "FoodData_Central_foundation_food_json_2026-04-30.zip",
        "release": "Foundation Foods 2026-04-30",
        "root_keys": ("FoundationFoods", "FoundationFood"),
    },
    {
        "name": "FoodData_Central_survey_food_json_2024-10-31.zip",
        "release": "FNDDS 2021-2023 (2024-10-31)",
        "root_keys": ("SurveyFoods", "FNDDSFoods", "SurveyFood"),
    },
]


def record_archive_provenance(resource: Resource, fdc_dir: Path, manifest: dict) -> None:
    """Persist verified source history using a fresh JSON object for ORM tracking."""
    by_name = {row.get("name"): row for row in manifest.get("files", [])}
    archives = {}
    for dataset in DATASETS:
        record = by_name.get(dataset["name"])
        archive = fdc_dir / dataset["name"]
        if not record or not archive.is_file():
            raise FileNotFoundError(f"Verified archive missing: {archive}")
        if record.get("release") != dataset["release"]:
            raise ValueError("FDC manifest release does not match the archive identity")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != record.get("sha256"):
            raise ValueError(f"FDC archive checksum mismatch: {archive}")
        if record.get("bytes") != archive.stat().st_size:
            raise ValueError("FDC archive size differs from the download manifest")
        expected_url = "https://fdc.nal.usda.gov/fdc-datasets/" + dataset["name"]
        if record.get("url") != expected_url:
            raise ValueError("FDC archive source URL does not match its official identity")
        archives[dataset["release"]] = {
            "sha256": digest, "bytes": archive.stat().st_size,
            "local_path": archive.as_posix(), "url": record["url"],
            "retrieved_utc": record.get("retrieved_utc"),
            "license_note": record.get("license_note"),
        }
    metadata = copy.deepcopy(resource.metadata_json or {})
    metadata.setdefault("previous_source_summary", {
        "source_url": resource.source_url, "verification": resource.verification,
        "release": resource.release, "sha256": resource.sha256,
        "local_path": resource.local_path,
    })
    metadata.setdefault("local_archives", {}).update(archives)
    manifest_path = fdc_dir / "download_manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    if json.loads(manifest_bytes) != manifest:
        raise ValueError("FDC download manifest changed during provenance capture")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    metadata["fdc_download_manifest"] = {
        "path": manifest_path.as_posix(), "sha256": manifest_digest,
        "hash_scope": "download_manifest; each archive has its own hash",
    }
    resource.metadata_json = metadata
    resource.release = "; ".join(sorted(metadata["local_archives"]))
    resource.sha256 = manifest_digest
    resource.local_path = manifest_path.as_posix()
    resource.source_url = "https://fdc.nal.usda.gov/download-datasets/"
    resource.verification = "Official release archives downloaded; source hashes and release identities verified"


def repair_fdc_provenance(data_dir: Path, database_url: str) -> dict:
    """Repair source metadata only; never reimport or rewrite food measurements."""
    fdc_dir = data_dir.expanduser().resolve() / "raw/fdc"
    manifest = json.loads((fdc_dir / "download_manifest.json").read_text(encoding="utf-8"))
    with get_session_factory(database_url)() as session:
        resource = session.get(Resource, RESOURCE_ID)
        if resource is None:
            raise ValueError("Provenance repair requires the existing FDC resource")
        releases = set(session.scalars(select(CompositionMeasurement.release).where(
            CompositionMeasurement.source_id == RESOURCE_ID).distinct()))
        if not releases or not releases.issubset({r["release"] for r in DATASETS}):
            raise ValueError("Imported FDC releases are absent or not covered by the verified archives")
        before = {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                  for model in (Food, CompositionMeasurement)}
        record_archive_provenance(resource, fdc_dir, manifest)
        session.commit()
        after = {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                 for model in (Food, CompositionMeasurement)}
        return {"status": "provenance_repaired", "before": before, "after": after,
                "manifest_sha256": resource.sha256, "imported_releases": sorted(releases)}


def _iter_foods(payload: Any) -> Iterator[dict[str, Any]]:
    if isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, dict) and item.get("fdcId"))
        return
    if not isinstance(payload, dict):
        return
    for key in ("FoundationFoods", "SurveyFoods", "FNDDSFoods", "BrandedFoods", "SRLegacyFoods"):
        rows = payload.get(key)
        if isinstance(rows, list):
            for item in rows:
                if isinstance(item, dict) and item.get("fdcId"):
                    yield item
            return
    # Some release archives contain one JSON object per food rather than a top-level collection.
    if payload.get("fdcId"):
        yield payload


def _category(food: dict[str, Any]) -> str | None:
    for key in ("foodCategory", "wweiaFoodCategory"):
        value = food.get(key)
        if isinstance(value, dict):
            value = value.get("description") or value.get("wweiaFoodCategoryDescription")
        if value:
            return str(value)
    return None


def _nutrient_fields(food_nutrient: dict[str, Any]) -> tuple[str, str, str | None, float | None, dict[str, Any]] | None:
    nutrient = food_nutrient.get("nutrient") or {}
    nutrient_id = nutrient.get("id") or food_nutrient.get("nutrientId")
    name = nutrient.get("name") or food_nutrient.get("nutrientName")
    unit = nutrient.get("unitName") or food_nutrient.get("unitName")
    amount = food_nutrient.get("amount")
    if nutrient_id is None or not name:
        return None
    try:
        value = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        value = None
    extra = {
        key: food_nutrient.get(key)
        for key in ("dataPoints", "standardError", "min", "max", "median", "derivationCode", "footnote")
        if food_nutrient.get(key) is not None
    }
    return str(nutrient_id), str(name), str(unit) if unit else None, value, extra


def _upsert_food(session: Session, release: str, item: dict[str, Any]) -> Food:
    fdc_id = str(item["fdcId"])
    food = session.scalar(
        select(Food).where(Food.source_id == RESOURCE_ID, Food.source_code == f"FDC:{fdc_id}")
    )
    if food is None:
        food = Food(source_id=RESOURCE_ID, source_code=f"FDC:{fdc_id}", name="")
        session.add(food)
        session.flush()
    description = str(item.get("description") or f"FDC {fdc_id}")
    food.name = description
    food.name_en = description
    food.food_group = _category(item)
    food.edible_state = "edible_portion"
    food.preparation_state = None
    food.active_release = release
    food.composition_mode = "direct"
    food.aliases = [str(item[key]) for key in ("foodClass", "scientificName") if item.get(key)]
    food.metadata_json = {
        "fdc_id": item.get("fdcId"),
        "data_type": item.get("dataType"),
        "publication_date": item.get("publicationDate"),
        "modified_date": item.get("modifiedDate"),
        "food_category": _category(item),
        "release": release,
    }
    return food


def import_fdc(data_dir: Path = DATA_DIR, database_url: str = DATABASE_URL) -> dict[str, int]:
    data_dir = data_dir.expanduser().resolve()
    fdc_dir = data_dir / "raw" / "fdc"
    manifest_path = fdc_dir / "download_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"FDC download manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_name = {row.get("name"): row for row in manifest.get("files", [])}
    create_schema(database_url)
    session: Session = get_session_factory(database_url)()
    counts = {"food_records": 0, "composition_records": 0, "missing_amount_records": 0}
    try:
        resource = session.get(Resource, RESOURCE_ID)
        if resource is None:
            resource = Resource(
                id=RESOURCE_ID,
                name=RESOURCE_NAME,
                layer="composition",
                priority="P0",
                source_url="https://fdc.nal.usda.gov/download-datasets/",
                verification="Official release archive downloaded and hashed locally",
                use_conditions="Preserve FDC source identity and release in derived records.",
                role_and_limits="US food composition reference; not a Korean food name crosswalk.",
            )
            session.add(resource)
            session.flush()

        for dataset in DATASETS:
            archive = fdc_dir / dataset["name"]
            record = by_name.get(dataset["name"])
            if not archive.is_file() or not record:
                raise FileNotFoundError(f"Verified archive missing: {archive}")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest != record.get("sha256"):
                raise ValueError(f"FDC archive checksum mismatch: {archive}")
            source_metadata = copy.deepcopy(resource.metadata_json or {})
            releases = source_metadata.setdefault("local_archives", {})
            releases[dataset["release"]] = {
                "sha256": digest,
                "local_path": archive.as_posix(),
                "url": record.get("url"),
            }
            resource.metadata_json = source_metadata

            with zipfile.ZipFile(archive) as zf:
                json_files = [name for name in zf.namelist() if name.lower().endswith(".json")]
                if not json_files:
                    raise ValueError(f"No JSON data file found in {archive.name}")
                candidate = None
                for member in json_files:
                    basename = Path(member).name.casefold()
                    if "food" in basename and "nutrient" not in basename:
                        candidate = member
                        break
                candidate = candidate or json_files[0]
                with zf.open(candidate) as handle:
                    payload = json.load(handle)

            matched_root = None
            if isinstance(payload, dict):
                for root_key in dataset["root_keys"]:
                    if isinstance(payload.get(root_key), list):
                        matched_root = root_key
                        break
            if isinstance(payload, dict) and matched_root:
                food_rows = payload[matched_root]
            else:
                food_rows = list(_iter_foods(payload))
            if not isinstance(food_rows, list):
                raise ValueError(f"Unsupported FDC JSON structure in {archive.name}")

            for item in food_rows:
                if not isinstance(item, dict) or not item.get("fdcId"):
                    continue
                food = _upsert_food(session, dataset["release"], item)
                counts["food_records"] += 1
                for raw_nutrient in item.get("foodNutrients", []):
                    parsed = _nutrient_fields(raw_nutrient)
                    if parsed is None:
                        continue
                    nutrient_id, name, unit, amount, extra = parsed
                    component_key = f"FDC-NUTRIENT:{nutrient_id}"
                    component = session.scalar(
                        select(Component).where(Component.component_key == component_key)
                    )
                    if component is None:
                        component = Component(
                            component_key=component_key,
                            name=name,
                            name_en=name,
                            category="nutrient",
                            canonical_unit=unit,
                            standard_identifier=component_key,
                        )
                        session.add(component)
                        session.flush()
                    else:
                        if component.canonical_unit is None and unit:
                            component.canonical_unit = unit
                    source_ref = f"FDC:{item['fdcId']}:nutrient:{nutrient_id}"
                    measurement = session.scalar(
                        select(CompositionMeasurement).where(
                            CompositionMeasurement.food_id == food.id,
                            CompositionMeasurement.component_id == component.id,
                            CompositionMeasurement.source_id == RESOURCE_ID,
                            CompositionMeasurement.source_ref == source_ref,
                            CompositionMeasurement.release == dataset["release"],
                        )
                    )
                    if measurement is None:
                        measurement = CompositionMeasurement(
                            food_id=food.id,
                            component_id=component.id,
                            source_id=RESOURCE_ID,
                            source_ref=source_ref,
                            release=dataset["release"],
                            basis="per_100g_edible_portion",
                        )
                        session.add(measurement)
                    if amount is None or not math.isfinite(amount):
                        measurement.amount_per_100g = None
                        measurement.unit = unit
                        measurement.status = "not_measured"
                        counts["missing_amount_records"] += 1
                    elif amount < 0:
                        measurement.amount_per_100g = None
                        measurement.unit = unit
                        measurement.status = "source_conflict"
                        counts["missing_amount_records"] += 1
                    elif unit is None:
                        measurement.amount_per_100g = None
                        measurement.unit = None
                        measurement.status = "unit_missing"
                        counts["missing_amount_records"] += 1
                    else:
                        measurement.amount_per_100g = amount
                        measurement.unit = unit
                        measurement.status = "measured_zero" if amount == 0 else "measured"
                        measurement.n_observations = extra.get("dataPoints")
                        measurement.standard_error = extra.get("standardError")
                        measurement.minimum = extra.get("min")
                        measurement.maximum = extra.get("max")
                    measurement.metadata_json = {
                        **extra,
                        "source_amount": amount,
                        "source_unit": unit,
                    }
                    counts["composition_records"] += 1
            session.flush()
        record_archive_provenance(resource, fdc_dir, manifest)
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Import official USDA FDC JSON releases into SQLite")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--provenance-only", action="store_true",
                        help="Repair verified archive metadata without reimporting foods")
    args = parser.parse_args()
    result = (repair_fdc_provenance(args.data_dir, args.database_url) if args.provenance_only
              else import_fdc(args.data_dir, args.database_url))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

