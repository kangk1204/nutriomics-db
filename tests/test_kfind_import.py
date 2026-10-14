from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from nutrigenomics.db import get_engine, get_session_factory
from nutrigenomics.kfind import NUTRIENT_CROSSWALK
from nutrigenomics.models import Component, CompositionMeasurement, Food, Resource
from scripts.import_kfind_foods import import_kfind_foods


def _write_release(
    directory: Path,
    dataset_id: str,
    selected: list[dict[str, str]],
) -> Path:
    directory.mkdir(parents=True)
    columns = [
        "FOOD_CD",
        "FOOD_NM",
        "NUT_CON_SRTR_QUA",
        "DATA_PROD_NM",
        *NUTRIENT_CROSSWALK,
    ]
    metadata = {"totalCount": str(len(selected)), "tableVO": {"colNmList": columns}}
    metadata_bytes = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    (directory / "columns.json").write_bytes(metadata_bytes)

    records = []
    for index, item in enumerate(selected):
        row = {
            "FOOD_CD": item["food_code"],
            "FOOD_NM": item["food_name"],
            "NUT_CON_SRTR_QUA": "100g",
            "DATA_PROD_NM": "분석",
            **{field: "1.5" for field in NUTRIENT_CROSSWALK},
        }
        if index == 0:
            row.update({"ENERC": "0", "WATER": "", "PROT": "Tr"})
        records.append(row)
    page_bytes = json.dumps(records, ensure_ascii=False).encode("utf-8")
    (directory / "page-0001.json").write_bytes(page_bytes)
    manifest = {
        "dataset_id": dataset_id,
        "dataset_name": f"fixture-{dataset_id}",
        "source_page": f"https://example.invalid/data/{dataset_id}",
        "downloaded_utc": "2026-09-30T00:00:00+00:00",
        "provider_reference_date_min": "2026-01-01",
        "provider_reference_date_max": "2026-01-02",
        "total_count": len(records),
        "page_size": 10_000,
        "page_count": 1,
        "page_record_counts": [len(records)],
        "duplicate_food_code_excess_count": 0,
        "sha256": {
            "columns.json": hashlib.sha256(metadata_bytes).hexdigest(),
            "page-0001.json": hashlib.sha256(page_bytes).hexdigest(),
        },
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return directory


def test_kfind_import_is_provenance_preserving_and_idempotent(tmp_path: Path) -> None:
    foods = []
    for dataset_id, count in (("15100065", 39), ("15100070", 11)):
        for index in range(count):
            foods.append(
                {
                    "dataset_id": dataset_id,
                    "food_code": f"{dataset_id}-F{index:02d}",
                    "food_name": f"food-{dataset_id}-{index:02d}",
                    "name": f"식품 {dataset_id[-2:]}-{index:02d}",
                    "name_en": f"food {dataset_id[-2:]}-{index:02d}",
                    "aliases": [f"별칭 {dataset_id[-2:]}-{index:02d}"],
                    "food_group": "fixture",
                    "preparation_state": "source 100g",
                }
            )
    selection = {
        "version": "fixture-v1",
        "foods": foods,
    }
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(selection, ensure_ascii=False), encoding="utf-8")
    dataset_directories = [
        _write_release(
            tmp_path / dataset_id,
            dataset_id,
            [item for item in selection["foods"] if item["dataset_id"] == dataset_id],
        )
        for dataset_id in ("15100065", "15100070")
    ]
    database_path = tmp_path / "import.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"

    first = import_kfind_foods(dataset_directories, selection_path, database_url)
    second = import_kfind_foods(dataset_directories, selection_path, database_url)
    assert first["selected_food_count"] == 50
    assert first["composition_records_upserted"] == 50 * len(NUTRIENT_CROSSWALK)
    assert first["composition_status_counts"]["measured_zero"] == 2
    assert first["composition_status_counts"]["not_measured"] == 2
    assert first["composition_status_counts"]["below_detection"] == 2
    assert second["foods_upserted"] == 50
    assert second["composition_records_upserted"] == first["composition_records_upserted"]

    session_factory = get_session_factory(database_url)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Food)) == 50
        assert session.scalar(select(func.count()).select_from(Component)) == len(NUTRIENT_CROSSWALK)
        assert session.scalar(select(func.count()).select_from(CompositionMeasurement)) == (
            50 * len(NUTRIENT_CROSSWALK)
        )
        resource = session.get(Resource, "R02")
        assert resource is not None
        assert set(resource.metadata_json["kfind_dataset_releases"]) == {"15100065", "15100070"}
        assert resource.metadata_json["selection_version"] == "fixture-v1"
        assert session.scalar(
            select(CompositionMeasurement.status).where(
                CompositionMeasurement.status == "below_detection"
            ).limit(1)
        ) == "below_detection"
        assert session.scalar(
            select(CompositionMeasurement.basis).limit(1)
        ) == "source_100g"
        assert Counter(food.source_id for food in session.scalars(select(Food))) == {"R02": 50}
    get_engine(database_url).dispose()
