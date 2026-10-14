from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.kfind import (
    NUTRIENT_CROSSWALK,
    food_identity,
    parse_100g_basis,
    parse_source_value,
    validate_selection,
)
from nutrigenomics.models import Component, CompositionMeasurement, Food, Resource
from nutrigenomics.settings import DATABASE_URL, PROJECT_ROOT

RESOURCE_ID = "R02"
RESOURCE_NAME = "K-FIND 식품영양성분 데이터베이스"
RESOURCE_INFO_URL = "https://various.foodsafetykorea.go.kr/nutrient/industry/openApi/info.do"
SELECTION_PATH = PROJECT_ROOT / "config" / "kfind_food_selection.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_dataset(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load one immutable raw release and verify its downloaded file hashes/counts."""
    directory = directory.expanduser().resolve()
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"K-FIND release manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dataset_id = str(manifest.get("dataset_id", ""))
    if dataset_id not in {"15100065", "15100070"}:
        raise ValueError(f"Unsupported K-FIND dataset ID in {manifest_path}: {dataset_id!r}")
    expected_hashes = manifest.get("sha256")
    if not isinstance(expected_hashes, dict) or "columns.json" not in expected_hashes:
        raise ValueError(f"K-FIND release has no complete SHA-256 manifest: {manifest_path}")

    metadata_path = directory / "columns.json"
    metadata_bytes = metadata_path.read_bytes()
    if _sha256(metadata_bytes) != expected_hashes["columns.json"]:
        raise ValueError(f"K-FIND column metadata hash mismatch: {metadata_path}")
    metadata = json.loads(metadata_bytes)
    columns = metadata.get("tableVO", {}).get("colNmList")
    required = {"FOOD_CD", "FOOD_NM", "NUT_CON_SRTR_QUA", *NUTRIENT_CROSSWALK}
    if not isinstance(columns, list) or not required.issubset(columns):
        missing = sorted(required - set(columns or []))
        raise ValueError(f"K-FIND metadata is missing required columns: {missing}")

    page_paths = sorted(directory.glob("page-*.json"))
    expected_pages = int(manifest.get("page_count", 0))
    page_counts = manifest.get("page_record_counts")
    if (
        expected_pages < 1
        or len(page_paths) != expected_pages
        or not isinstance(page_counts, list)
        or len(page_counts) != expected_pages
    ):
        raise ValueError(f"K-FIND page count mismatch in {directory}")
    records: list[dict[str, Any]] = []
    observed_page_counts: list[int] = []
    for page_path in page_paths:
        filename = page_path.name
        expected_hash = expected_hashes.get(filename)
        if not expected_hash:
            raise ValueError(f"K-FIND manifest has no checksum for {page_path}")
        body = page_path.read_bytes()
        if _sha256(body) != expected_hash:
            raise ValueError(f"K-FIND data page hash mismatch: {page_path}")
        page_records = json.loads(body)
        if not isinstance(page_records, list) or any(not isinstance(row, dict) for row in page_records):
            raise ValueError(f"K-FIND data page must be a JSON object array: {page_path}")
        observed_page_counts.append(len(page_records))
        records.extend(page_records)

    expected_total = int(manifest.get("total_count", -1))
    if len(records) != expected_total or observed_page_counts != page_counts:
        raise ValueError(
            f"K-FIND record count mismatch in {directory}: "
            f"total={len(records)}/{expected_total}, pages={observed_page_counts}/{page_counts}"
        )
    for row in records:
        if not row.get("FOOD_CD") or not row.get("FOOD_NM"):
            raise ValueError(f"K-FIND record in {directory} has no food code/name")
    return manifest, records


def select_records(
    selection: dict[str, Any],
    datasets: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Match the locked food list to exact provider dataset, code, and name triples."""
    selected = validate_selection(selection)
    result: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    invalid: list[str] = []
    for item in selected:
        dataset_id = str(item["dataset_id"])
        if dataset_id not in datasets:
            invalid.append(f"{dataset_id}/{item['food_code']}/{item['food_name']} (release missing)")
            continue
        manifest, records = datasets[dataset_id]
        matches = [
            row
            for row in records
            if str(row.get("FOOD_CD")) == str(item["food_code"])
            and str(row.get("FOOD_NM")) == str(item["food_name"])
        ]
        if len(matches) != 1:
            invalid.append(
                f"{dataset_id}/{item['food_code']}/{item['food_name']} (matches={len(matches)})"
            )
            continue
        row = matches[0]
        if parse_100g_basis(row.get("NUT_CON_SRTR_QUA")) != "source_100g":
            invalid.append(
                f"{dataset_id}/{item['food_code']}/{item['food_name']} "
                f"(basis={row.get('NUT_CON_SRTR_QUA')!r}, expected 100g)"
            )
            continue
        if str(row.get("DATA_PROD_NM", "")).strip() != "분석":
            invalid.append(
                f"{dataset_id}/{item['food_code']}/{item['food_name']} "
                f"(data production={row.get('DATA_PROD_NM')!r}, expected analyzed data)"
            )
            continue
        result.append((item, row, manifest))
    if invalid:
        raise ValueError("K-FIND curation validation failed:\n- " + "\n- ".join(invalid))
    if len(result) != 50:
        raise ValueError(f"The frozen K-FIND curation list must contain 50 foods; found {len(result)}")
    return result


def _source_release(manifest: dict[str, Any]) -> str:
    dataset_id = str(manifest["dataset_id"])
    reference_date = manifest.get("provider_reference_date_max")
    if not reference_date:
        reference_date = str(manifest.get("downloaded_utc", ""))[:10]
    try:
        date.fromisoformat(str(reference_date))
    except ValueError as error:
        raise ValueError(f"K-FIND release has no ISO reference/download date: {reference_date!r}") from error
    return f"K-FIND-{dataset_id}-{reference_date}"


def _record_checksum(row: dict[str, Any]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(payload)


def _release_checksum(manifests: dict[str, dict[str, Any]]) -> str:
    payload = {
        dataset_id: sorted(manifest["sha256"].items())
        for dataset_id, manifest in sorted(manifests.items())
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _upsert_resource(
    session: Session,
    manifests: dict[str, dict[str, Any]],
    directories: dict[str, Path],
    selection_version: str,
    selected_counts: Counter[str],
) -> None:
    resource = session.get(Resource, RESOURCE_ID)
    if resource is None:
        resource = Resource(id=RESOURCE_ID)
        session.add(resource)
    release_rows = {
        dataset_id: {
            "release": _source_release(manifest),
            "downloaded_utc": manifest.get("downloaded_utc"),
            "provider_reference_date_min": manifest.get("provider_reference_date_min"),
            "provider_reference_date_max": manifest.get("provider_reference_date_max"),
            "total_count": manifest.get("total_count"),
            "selected_count": selected_counts[dataset_id],
            "source_page": manifest.get("source_page"),
            "local_path": str(directories[dataset_id]),
            "sha256": manifest.get("sha256", {}),
            "duplicate_food_code_excess_count": manifest.get("duplicate_food_code_excess_count", 0),
        }
        for dataset_id, manifest in manifests.items()
    }
    existing_metadata = dict(resource.metadata_json or {})
    dataset_releases = dict(existing_metadata.get("kfind_dataset_releases", {}))
    dataset_releases.update(release_rows)
    resource.name = RESOURCE_NAME
    resource.layer = "food_composition"
    resource.priority = "P1"
    resource.source_url = RESOURCE_INFO_URL
    resource.verification = "DATA.GO.KR 공개 표준데이터 원본 JSON 페이지의 해시와 레코드 수를 확인했습니다."
    resource.use_conditions = (
        "비상업적 연구를 위한 로컬 사용. 표준데이터 상세 이용허락 표기를 별도 확인하기 전까지 "
        "원본 대량자료의 재배포를 금지합니다."
    )
    resource.role_and_limits = (
        "K-FIND의 음식·원재료 성분값을 제공합니다. 100g으로 공표된 함량을 보존하며, "
        "사진 인식·개인별 건강효과·혈중 농도 환산 자료는 제공하지 않습니다."
    )
    resource.release = ";".join(_source_release(manifests[key]) for key in sorted(manifests))
    resource.sha256 = _release_checksum(manifests)
    resource.local_path = os.path.commonpath([str(path) for path in directories.values()])
    resource.metadata_json = {
        **existing_metadata,
        "provider": "식품의약품안전처 K-FIND / DATA.GO.KR",
        "kfind_dataset_releases": dataset_releases,
        "selection_version": selection_version,
        "nutrient_crosswalk": {
            field: f"FDC-NUTRIENT:{definition['fdc_id']}"
            for field, definition in NUTRIENT_CROSSWALK.items()
        },
        "license_review_status": (
            "공개 표준데이터 상세 이용조건 미확인; 비상업 로컬 연구만, 원본 재배포 제한"
        ),
    }


def import_kfind_foods(
    dataset_directories: list[Path],
    selection_path: Path = SELECTION_PATH,
    database_url: str = DATABASE_URL,
) -> dict[str, Any]:
    selection = json.loads(selection_path.expanduser().resolve().read_text(encoding="utf-8"))
    selected = validate_selection(selection)
    required_dataset_ids = {str(item["dataset_id"]) for item in selected}
    if len(dataset_directories) != len(required_dataset_ids):
        raise ValueError(
            f"Provide exactly one raw release directory for each selected dataset: "
            f"{sorted(required_dataset_ids)}"
        )
    directories: dict[str, Path] = {}
    manifests: dict[str, dict[str, Any]] = {}
    data_rows: dict[str, list[dict[str, Any]]] = {}
    for directory in dataset_directories:
        manifest, rows = load_dataset(directory)
        dataset_id = str(manifest["dataset_id"])
        if dataset_id in directories:
            raise ValueError(f"More than one K-FIND release supplied for dataset {dataset_id}")
        directories[dataset_id] = directory.expanduser().resolve()
        manifests[dataset_id] = manifest
        data_rows[dataset_id] = rows
    if set(directories) != required_dataset_ids:
        raise ValueError(
            f"K-FIND releases do not match selection. Required {sorted(required_dataset_ids)}, "
            f"received {sorted(directories)}"
        )
    matched = select_records(
        selection,
        {dataset_id: (manifests[dataset_id], data_rows[dataset_id]) for dataset_id in directories},
    )
    selected_counts: Counter[str] = Counter(
        str(item["dataset_id"]) for item, _, _ in matched
    )
    release_ids = {dataset_id: _source_release(manifest) for dataset_id, manifest in manifests.items()}

    # No database changes happen until both complete immutable releases and all 50 exact
    # source identities have passed validation.
    create_schema(database_url)
    session = get_session_factory(database_url)()
    counts: Counter[str] = Counter()
    try:
        with session.begin():
            _upsert_resource(
                session,
                manifests,
                directories,
                str(selection.get("version", "unspecified")),
                selected_counts,
            )
            session.flush()
            for selected_food, row, manifest in matched:
                dataset_id = str(manifest["dataset_id"])
                source_code, source_ref = food_identity(
                    dataset_id, str(row["FOOD_CD"]), str(row["FOOD_NM"])
                )
                food = session.scalar(
                    select(Food).where(
                        Food.source_id == RESOURCE_ID,
                        Food.source_code == source_code,
                    )
                )
                if food is None:
                    food = Food(source_id=RESOURCE_ID, source_code=source_code)
                    session.add(food)
                release = release_ids[dataset_id]
                raw_name = str(row["FOOD_NM"])
                aliases = list(dict.fromkeys([*(selected_food.get("aliases") or []), raw_name]))
                source_record_hash = _record_checksum(row)
                food.name = str(selected_food["name"])
                food.name_en = str(selected_food["name_en"])
                food.aliases = aliases
                food.food_group = str(selected_food["food_group"])
                food.edible_state = (
                    "원천 레코드 기준 중량 100g; 가식부 환산 여부는 별도 확인 필요"
                )
                food.preparation_state = str(selected_food["preparation_state"])
                food.active_release = release
                food.composition_mode = "direct"
                food.metadata_json = {
                    "selection_version": selection.get("version"),
                    "kfind_dataset_id": dataset_id,
                    "provider_food_code": row["FOOD_CD"],
                    "provider_food_name": raw_name,
                    "provider_source_name": row.get("SRC_NM"),
                    "data_production_method": row.get("DATA_PROD_NM"),
                    "provider_basis": row.get("NUT_CON_SRTR_QUA"),
                    "provider_reference_date": row.get("CRTR_YMD"),
                    "provider_creation_date": row.get("CRT_YMD"),
                    "refuse_percent": row.get("REFUSE"),
                    "food_size": row.get("FOOD_SIZE"),
                    "serving_size": row.get("SERV_SIZE"),
                    "country_of_origin": row.get("COO_NM"),
                    "selection_record_sha256": source_record_hash,
                    "raw_release_path": str(directories[dataset_id]),
                    "raw_release_sha256": manifests[dataset_id]["sha256"],
                    "source_record_basis_note": (
                        "함량은 K-FIND의 100g 공표 기준으로 저장. REFUSE 값은 별도 보존하며 "
                        "가식부 환산을 자동 적용하지 않음."
                    ),
                }
                session.flush()
                counts["foods"] += 1
                for field, definition in NUTRIENT_CROSSWALK.items():
                    component_key = f"FDC-NUTRIENT:{definition['fdc_id']}"
                    component = session.scalar(
                        select(Component).where(Component.component_key == component_key)
                    )
                    if component is None:
                        component = Component(
                            component_key=component_key,
                            name=definition["name"],
                            name_en=definition["name_en"],
                            category="nutrient",
                            canonical_unit=definition["unit"],
                            standard_identifier=component_key,
                            aliases=[f"K-FIND:{field}"],
                            metadata_json={"kfind_field_codes": [field]},
                        )
                        session.add(component)
                        session.flush()
                    else:
                        if component.canonical_unit is None:
                            component.canonical_unit = definition["unit"]
                        component.metadata_json = {
                            **(component.metadata_json or {}),
                            "kfind_field_codes": list(
                                dict.fromkeys(
                                    [
                                        *(component.metadata_json or {}).get("kfind_field_codes", []),
                                        field,
                                    ]
                                )
                            ),
                        }
                        component.aliases = list(
                            dict.fromkeys([*(component.aliases or []), f"K-FIND:{field}"])
                        )
                    parsed = parse_source_value(row.get(field))
                    measurement_ref = f"{source_ref}:nutrient:{field}"
                    measurement = session.scalar(
                        select(CompositionMeasurement).where(
                            CompositionMeasurement.food_id == food.id,
                            CompositionMeasurement.component_id == component.id,
                            CompositionMeasurement.source_id == RESOURCE_ID,
                            CompositionMeasurement.source_ref == measurement_ref,
                            CompositionMeasurement.release == release,
                        )
                    )
                    if measurement is None:
                        measurement = CompositionMeasurement(
                            food_id=food.id,
                            component_id=component.id,
                            source_id=RESOURCE_ID,
                            source_ref=measurement_ref,
                            release=release,
                        )
                        session.add(measurement)
                    measurement.amount_per_100g = (
                        float(parsed.amount) if parsed.amount is not None else None
                    )
                    measurement.unit = definition["unit"]
                    measurement.basis = "source_100g"
                    measurement.status = parsed.status
                    measurement.n_observations = None
                    measurement.standard_error = None
                    measurement.minimum = None
                    measurement.maximum = None
                    measurement.metadata_json = {
                        "provider_dataset_id": dataset_id,
                        "provider_food_code": row["FOOD_CD"],
                        "provider_food_name": raw_name,
                        "provider_nutrient_field": field,
                        "provider_value_raw": parsed.raw_value,
                        "provider_basis": row.get("NUT_CON_SRTR_QUA"),
                        "provider_source_name": row.get("SRC_NM"),
                        "data_production_method": row.get("DATA_PROD_NM"),
                        "provider_reference_date": row.get("CRTR_YMD"),
                        "component_standard_identifier": component_key,
                        "source_record_sha256": source_record_hash,
                        "amount_interpretation": (
                            "source value per 100g; no edible-yield conversion applied"
                        ),
                    }
                    counts["composition_records"] += 1
                    counts[f"status_{parsed.status}"] += 1
        return {
            "status": "completed",
            "selection_version": selection.get("version"),
            "selected_food_count": len(matched),
            "foods_upserted": counts["foods"],
            "composition_records_upserted": counts["composition_records"],
            "composition_status_counts": {
                key.removeprefix("status_"): value
                for key, value in sorted(counts.items())
                if key.startswith("status_")
            },
            "dataset_releases": release_ids,
            "source_id": RESOURCE_ID,
            "database_url": database_url,
        }
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import the versioned 50-food K-FIND curation into the research database"
    )
    parser.add_argument(
        "--dataset-dir",
        action="append",
        type=Path,
        required=True,
        help="One immutable K-FIND raw release directory; supply once per selected dataset",
    )
    parser.add_argument("--selection-file", type=Path, default=SELECTION_PATH)
    parser.add_argument("--database-url", default=DATABASE_URL)
    args = parser.parse_args()
    print(
        json.dumps(
            import_kfind_foods(args.dataset_dir, args.selection_file, args.database_url),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
