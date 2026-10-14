from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import create_schema, get_session_factory
from .models import Component, Food, FoodCompoundMeasurement, Resource
from .settings import DATABASE_URL

RESOURCE_ID = "R05"
RELEASE = "FooDB-2020-04-07"
DOWNLOADS_URL = "https://foodb.ca/downloads"
COMPLIANCE_URL = "https://foodb.ca/compliance"
REQUIRED_TABLES = {
    "foods": {"id", "public_id", "name"},
    "compounds": {"id", "public_id", "name", "status"},
    "contents": {"id", "source_id", "source_type", "food_id", "orig_content", "orig_unit"},
}

_STATUS_LABELS = {
    "0": "quantified",
    "1": "detected",
    "2": "expected",
    "3": "predicted",
}
_MASS_PER_100G = re.compile(r"^(g|mg|ug|mcg|μg|µg)/100g$", re.IGNORECASE)


def _decimal_or_none(value: Any) -> float | None:
    if value is None:
        return None
    token = str(value).strip().replace(",", "")
    if not token:
        return None
    try:
        amount = Decimal(token)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    converted = float(amount)
    return converted if math.isfinite(converted) else None


def _per_100g_mass_unit(value: Any) -> tuple[str, str] | None:
    token = re.sub(r"\s+", "", str(value or "")).replace("μ", "u").replace("µ", "u")
    match = _MASS_PER_100G.fullmatch(token)
    if match is None:
        return None
    unit = match.group(1).casefold().replace("μ", "u").replace("µ", "u")
    if unit == "mcg":
        unit = "ug"
    return unit, "source_100g"


def parse_compound_content(
    compound_status: Any,
    content: Any,
    minimum: Any,
    maximum: Any,
    unit: Any,
) -> dict[str, Any]:
    """Parse FooDB content without guessing a basis or turning detection into zero."""
    raw_status = str(compound_status if compound_status is not None else "").strip()
    status = _STATUS_LABELS.get(raw_status, "unknown_source_status")
    raw_content = None if content is None else str(content)
    raw_unit = None if unit is None else str(unit)
    amount = _decimal_or_none(content)
    low = _decimal_or_none(minimum)
    high = _decimal_or_none(maximum)
    normalized_unit = _per_100g_mass_unit(unit)

    basis: str | None = normalized_unit[1] if normalized_unit else None
    stored_unit: str | None = normalized_unit[0] if normalized_unit else raw_unit
    amount_per_100g = None
    measurement_status = status
    if status == "quantified":
        if amount is None:
            if raw_content and low is None and high is None:
                measurement_status = "content_unparsed"
            else:
                measurement_status = "range_only" if low is not None or high is not None else "content_missing"
        elif normalized_unit is None:
            measurement_status = "reported_unscaled"
            basis = "source_reported"
        else:
            amount_per_100g = amount
    elif status == "unknown_source_status":
        measurement_status = "content_unclassified"

    metadata = {
        "source_status_code": raw_status,
        "orig_content": raw_content,
        "orig_min": None if minimum is None else str(minimum),
        "orig_max": None if maximum is None else str(maximum),
        "orig_unit": raw_unit,
    }
    if status == "quantified":
        metadata["content_interpretation"] = (
            "FooDB documents orig_content as either an interval average or a literature value; "
            "the row does not identify which. Source min/max are retained as reported ranges, "
            "not statistical confidence intervals."
        )

    return {
        "status": measurement_status,
        "amount_per_100g": amount_per_100g,
        "unit": stored_unit,
        "basis": basis,
        "minimum": low,
        "maximum": high,
        "metadata": metadata,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(data_dir: Path, table_files: dict[str, str]) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    if set(table_files) != set(REQUIRED_TABLES):
        raise ValueError(f"FooDB table files must be exactly: {sorted(REQUIRED_TABLES)}")
    tables: dict[str, dict[str, Any]] = {}
    for table_name, filename in table_files.items():
        if Path(filename).name != filename:
            raise ValueError(f"FooDB table filenames must be basenames: {filename!r}")
        path = data_dir / filename
        if not path.is_file():
            raise FileNotFoundError(path)
        tables[table_name] = {
            "file": filename,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
    return {
        "provider": "FooDB",
        "release": RELEASE,
        "source_url": DOWNLOADS_URL,
        "terms_url": COMPLIANCE_URL,
        "use_conditions": "CC BY-NC 4.0; do not redistribute source tables",
        "tables": tables,
    }


def _verified_table_paths(data_dir: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    manifest_path = data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("provider") != "FooDB" or manifest.get("release") != RELEASE:
        raise ValueError(f"Expected the official {RELEASE} FooDB release manifest")
    if manifest.get("source_url") != DOWNLOADS_URL or manifest.get("terms_url") != COMPLIANCE_URL:
        raise ValueError("FooDB source and license URLs do not match the official records")
    table_records = manifest.get("tables")
    if not isinstance(table_records, dict) or set(table_records) != set(REQUIRED_TABLES):
        raise ValueError(f"FooDB manifest must contain exactly: {sorted(REQUIRED_TABLES)}")

    paths: dict[str, Path] = {}
    for table_name, required_columns in REQUIRED_TABLES.items():
        record = table_records[table_name]
        filename = str(record.get("file", ""))
        if Path(filename).name != filename:
            raise ValueError(f"Unsafe FooDB table path: {filename!r}")
        path = data_dir / filename
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size != int(record.get("bytes", -1)):
            raise ValueError(f"FooDB file size mismatch: {path}")
        if sha256_file(path) != record.get("sha256"):
            raise ValueError(f"FooDB SHA-256 mismatch: {path}")
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            headers = set(next(csv.reader(handle), []))
        if not required_columns.issubset(headers):
            raise ValueError(
                f"FooDB {table_name} table is missing columns: "
                f"{sorted(required_columns - headers)}"
            )
        paths[table_name] = path
    return paths, manifest


def _read_crosswalk(path: Path) -> list[dict[str, str]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    matches = document.get("matches") if isinstance(document, dict) else None
    if not isinstance(matches, list) or not matches:
        raise ValueError("FooDB crosswalk must contain reviewed food matches")
    required = {
        "local_food_source_id",
        "local_food_source_code",
        "foodb_food_public_id",
        "match_method",
        "review_note",
    }
    seen_local: set[tuple[str, str]] = set()
    seen_foodb: set[str] = set()
    result = []
    for index, row in enumerate(matches):
        if not isinstance(row, dict):
            raise TypeError(f"FooDB crosswalk row {index} must be an object")
        missing = [key for key in required if not str(row.get(key, "")).strip()]
        if missing:
            raise ValueError(f"FooDB crosswalk row {index} missing: {', '.join(sorted(missing))}")
        local_id = (str(row["local_food_source_id"]), str(row["local_food_source_code"]))
        foodb_id = str(row["foodb_food_public_id"])
        if local_id in seen_local or foodb_id in seen_foodb:
            raise ValueError(f"FooDB crosswalk must be one-to-one; duplicate row: {local_id}/{foodb_id}")
        if row["match_method"] not in {"exact_common_name", "scientific_name", "manual_review"}:
            raise ValueError(f"Unknown FooDB crosswalk method: {row['match_method']!r}")
        seen_local.add(local_id)
        seen_foodb.add(foodb_id)
        result.append({key: str(value).strip() for key, value in row.items() if value is not None})
    return result


def _csv_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _external_identifiers(row: dict[str, str]) -> dict[str, str]:
    prefixes = ("pubchem", "chebi", "chembl", "hmdb", "inchi", "cas")
    return {
        key: value.strip()
        for key, value in row.items()
        if value and any(prefix in key.casefold() for prefix in prefixes)
    }


def import_foodb(
    data_dir: Path,
    crosswalk_path: Path,
    database_url: str = DATABASE_URL,
) -> dict[str, int]:
    data_dir = data_dir.expanduser().resolve()
    crosswalk_path = crosswalk_path.expanduser().resolve()
    paths, manifest = _verified_table_paths(data_dir)
    crosswalk = _read_crosswalk(crosswalk_path)

    target_public_ids = {row["foodb_food_public_id"] for row in crosswalk}
    food_records: dict[str, dict[str, str]] = {}
    for row in _csv_rows(paths["foods"]):
        public_id = str(row.get("public_id", "")).strip()
        if public_id in target_public_ids:
            if public_id in food_records:
                raise ValueError(f"Duplicate FooDB food public ID: {public_id}")
            food_records[public_id] = row
    missing_foods = target_public_ids - food_records.keys()
    if missing_foods:
        raise ValueError(f"FooDB crosswalk references unknown foods: {sorted(missing_foods)}")

    food_id_to_crosswalk: dict[str, dict[str, str]] = {}
    for match in crosswalk:
        food_record = food_records[match["foodb_food_public_id"]]
        internal_id = str(food_record["id"])
        if internal_id in food_id_to_crosswalk:
            raise ValueError(f"FooDB internal food ID is not unique: {internal_id}")
        food_id_to_crosswalk[internal_id] = match

    selected_contents: list[dict[str, str]] = []
    compound_internal_ids: set[str] = set()
    for row in _csv_rows(paths["contents"]):
        if str(row.get("food_id", "")).strip() not in food_id_to_crosswalk:
            continue
        if str(row.get("source_type", "")).strip().casefold() != "compound":
            continue
        if row.get("export") and str(row["export"]).strip() not in {"1", "true", "True"}:
            continue
        compound_id = str(row.get("source_id", "")).strip()
        if not compound_id:
            continue
        selected_contents.append(row)
        compound_internal_ids.add(compound_id)

    compound_records: dict[str, dict[str, str]] = {}
    for row in _csv_rows(paths["compounds"]):
        internal_id = str(row.get("id", "")).strip()
        if internal_id not in compound_internal_ids:
            continue
        if row.get("export") and str(row["export"]).strip() not in {"1", "true", "True"}:
            continue
        if internal_id in compound_records:
            raise ValueError(f"Duplicate FooDB compound internal ID: {internal_id}")
        compound_records[internal_id] = row
    missing_compounds = compound_internal_ids - compound_records.keys()
    if missing_compounds:
        raise ValueError(f"FooDB contents reference unknown/exported compounds: {sorted(missing_compounds)[:20]}")
    public_ids = [str(row.get("public_id", "")).strip() for row in compound_records.values()]
    if any(not public_id for public_id in public_ids) or len(public_ids) != len(set(public_ids)):
        raise ValueError("FooDB compounds used by selected foods must have unique, non-empty public IDs")

    create_schema(database_url)
    session: Session = get_session_factory(database_url)()
    counts = {
        "foods_mapped": 0,
        "compounds_seen": len(compound_records),
        "content_rows_seen": len(selected_contents),
        "quantified_rows": 0,
        "non_quantified_rows": 0,
        "inserted": 0,
        "updated": 0,
    }
    try:
        resource = session.get(Resource, RESOURCE_ID)
        if resource is None:
            raise ValueError(f"Research resource {RESOURCE_ID} must be ingested before FooDB")
        local_foods: dict[tuple[str, str], Food] = {}
        foodb_internal_to_local: dict[str, tuple[Food, dict[str, str], dict[str, str]]] = {}
        for match in crosswalk:
            local_key = (match["local_food_source_id"], match["local_food_source_code"])
            local_food = session.scalar(
                select(Food).where(Food.source_id == local_key[0], Food.source_code == local_key[1])
            )
            if local_food is None:
                raise ValueError(f"FooDB crosswalk local food does not exist: {local_key}")
            food_record = food_records[match["foodb_food_public_id"]]
            foodb_internal_to_local[str(food_record["id"])] = (local_food, match, food_record)
            local_foods[local_key] = local_food
        counts["foods_mapped"] = len(local_foods)

        compound_public_ids = {str(row["public_id"]).strip() for row in compound_records.values()}
        existing_components: dict[str, Component] = {}
        sorted_public_ids = sorted(compound_public_ids)
        for start in range(0, len(sorted_public_ids), 500):
            batch = sorted_public_ids[start : start + 500]
            existing_components.update(
                {
                    component.component_key.removeprefix("FOODB:"): component
                    for component in session.scalars(
                        select(Component).where(
                            Component.component_key.in_([f"FOODB:{public_id}" for public_id in batch])
                        )
                    )
                }
            )
        components_by_internal_id: dict[str, Component] = {}
        for internal_id, row in compound_records.items():
            public_id = str(row["public_id"]).strip()
            component = existing_components.get(public_id)
            if component is None:
                component = Component(component_key=f"FOODB:{public_id}", name=str(row["name"]).strip())
                session.add(component)
            component.name = str(row["name"]).strip()
            component.name_en = str(row.get("name", "")).strip() or None
            component.category = "bioactive"
            component.canonical_unit = None
            component.standard_identifier = None
            component.inchi_key = str(row.get("inchi_key", "")).strip() or None
            component.aliases = []
            component.metadata_json = {
                "source": "FooDB",
                "foodb_internal_id": internal_id,
                "foodb_public_id": public_id,
                "status_code": str(row.get("status", "")).strip(),
                "external_identifiers": _external_identifiers(row),
            }
            components_by_internal_id[internal_id] = component
        session.flush()

        local_food_ids = [food.id for food in local_foods.values()]
        existing_measurements = {
            (row.food_id, row.component_id, row.source_ref): row
            for row in session.scalars(
                select(FoodCompoundMeasurement).where(
                    FoodCompoundMeasurement.food_id.in_(local_food_ids),
                    FoodCompoundMeasurement.source_id == RESOURCE_ID,
                    FoodCompoundMeasurement.release == RELEASE,
                )
            )
        }
        content_ids: set[str] = set()
        for row in selected_contents:
            source_ref = str(row.get("id", "")).strip()
            if not source_ref or source_ref in content_ids:
                raise ValueError(f"FooDB contents row ID is missing or duplicated: {source_ref!r}")
            content_ids.add(source_ref)
            food, match, food_record = foodb_internal_to_local[str(row["food_id"]).strip()]
            compound_internal_id = str(row["source_id"]).strip()
            compound = components_by_internal_id[compound_internal_id]
            source_status = compound_records[compound_internal_id].get("status")
            parsed = parse_compound_content(
                source_status,
                row.get("orig_content"),
                row.get("orig_min"),
                row.get("orig_max"),
                row.get("orig_unit"),
            )
            existing = existing_measurements.get((food.id, compound.id, source_ref))
            if existing is None:
                existing = FoodCompoundMeasurement(
                    food_id=food.id,
                    component_id=compound.id,
                    source_id=RESOURCE_ID,
                    source_ref=source_ref,
                    release=RELEASE,
                )
                session.add(existing)
                existing_measurements[(food.id, compound.id, source_ref)] = existing
                counts["inserted"] += 1
            else:
                counts["updated"] += 1
            existing.amount_per_100g = parsed["amount_per_100g"]
            existing.unit = parsed["unit"]
            existing.basis = parsed["basis"]
            existing.status = parsed["status"]
            existing.minimum = parsed["minimum"]
            existing.maximum = parsed["maximum"]
            existing.metadata_json = {
                **parsed["metadata"],
                "foodb_internal_food_id": str(food_record["id"]),
                "foodb_food_public_id": match["foodb_food_public_id"],
                "foodb_food_name": str(food_record.get("name", "")),
                "foodb_original_food_name": row.get("orig_food_common_name"),
                "foodb_original_food_part": row.get("orig_food_part"),
                "foodb_orig_source_name": row.get("orig_source_name"),
                "foodb_orig_citation": row.get("orig_citation"),
                "foodb_citation": row.get("citation"),
                "foodb_citation_type": row.get("citation_type"),
                "foodb_orig_method": row.get("orig_method"),
                "foodb_orig_unit_expression": row.get("orig_unit_expression"),
                "foodb_preparation_type": row.get("preparation_type"),
                "local_crosswalk_method": match["match_method"],
                "local_crosswalk_note": match["review_note"],
                "local_crosswalk_source_url": match.get("review_source_url"),
            }
            if parsed["status"] == "quantified":
                counts["quantified_rows"] += 1
            else:
                counts["non_quantified_rows"] += 1
            if counts["inserted"] % 5_000 == 0:
                session.flush()

        resource.release = RELEASE
        resource.source_url = DOWNLOADS_URL
        resource.use_conditions = "CC BY-NC 4.0; do not redistribute source tables"
        resource.local_path = str(data_dir)
        resource.sha256 = sha256_file(data_dir / "manifest.json")
        resource.metadata_json = {
            **(resource.metadata_json or {}),
            "fooDB_import_manifest": manifest,
            "fooDB_crosswalk_sha256": sha256_file(crosswalk_path),
            "fooDB_crosswalk_path": str(crosswalk_path),
        }
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
    return counts
