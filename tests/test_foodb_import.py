from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from nutrigenomics.composition import analyze_meal
from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.foodb import build_manifest, import_foodb, parse_compound_content
from nutrigenomics.models import (
    Component,
    Food,
    FoodCompoundMeasurement,
    Resource,
)


def test_parse_foodb_status_and_explicit_mass_per_100g_basis() -> None:
    quantified = parse_compound_content(0, "60", "40", "80", "mg/100g")
    assert quantified["status"] == "quantified"
    assert quantified["amount_per_100g"] == 60
    assert quantified["unit"] == "mg"
    assert quantified["basis"] == "source_100g"
    assert quantified["minimum"] == 40 and quantified["maximum"] == 80
    assert "not statistical confidence intervals" in quantified["metadata"]["content_interpretation"]

    detected = parse_compound_content(1, "60", "40", "80", "mg/100g")
    assert detected["status"] == "detected"
    assert detected["amount_per_100g"] is None

    unknown_basis = parse_compound_content(0, "2", "", "", "mg/g")
    assert unknown_basis["status"] == "reported_unscaled"
    assert unknown_basis["amount_per_100g"] is None
    assert unknown_basis["basis"] == "source_reported"

    range_only = parse_compound_content(0, "", "40", "80", "μg/100g")
    assert range_only["status"] == "range_only"
    assert range_only["amount_per_100g"] is None
    assert range_only["unit"] == "ug"


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_foodb_import_uses_reviewed_crosswalk_and_calculates_only_quantified_rows(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "foodb"
    data_dir.mkdir()
    _write_csv(
        data_dir / "foods.csv",
        [
            {
                "id": "41",
                "public_id": "FDB000041",
                "name": "Test berry",
                "name_scientific": "Vaccinium example",
                "food_group": "Berries",
            }
        ],
    )
    _write_csv(
        data_dir / "compounds.csv",
        [
            {
                "id": "701",
                "public_id": "FDB000701",
                "name": "Example flavonoid",
                "status": "0",
                "export": "1",
                "inchi_key": "ABCDEFGHIJKLMN-OPQRSTUVWX-Y",
                "pubchem_id": "12345",
            },
            {
                "id": "702",
                "public_id": "FDB000702",
                "name": "Detected compound",
                "status": "1",
                "export": "1",
                "inchi_key": "",
                "pubchem_id": "",
            },
        ],
    )
    _write_csv(
        data_dir / "contents.csv",
        [
            {
                "id": "9001",
                "source_id": "701",
                "source_type": "Compound",
                "food_id": "41",
                "orig_content": "60",
                "orig_min": "40",
                "orig_max": "80",
                "orig_unit": "mg/100g",
                "orig_food_common_name": "Test berry, raw",
                "orig_food_part": "fruit",
                "orig_source_name": "Journal source",
                "orig_citation": "doi:10.0000/example",
                "citation": "doi:10.0000/example",
                "citation_type": "LITERATURE",
                "orig_method": "HPLC",
                "orig_unit_expression": "mg per 100 g",
                "preparation_type": "raw",
                "export": "1",
            },
            {
                "id": "9002",
                "source_id": "702",
                "source_type": "Compound",
                "food_id": "41",
                "orig_content": "5",
                "orig_min": "",
                "orig_max": "",
                "orig_unit": "mg/100g",
                "orig_food_common_name": "Test berry, raw",
                "orig_food_part": "fruit",
                "orig_source_name": "Journal source",
                "orig_citation": "doi:10.0000/example",
                "citation": "doi:10.0000/example",
                "citation_type": "LITERATURE",
                "orig_method": "HPLC",
                "orig_unit_expression": "mg per 100 g",
                "preparation_type": "raw",
                "export": "1",
            },
            {
                "id": "9003",
                "source_id": "701",
                "source_type": "Nutrient",
                "food_id": "41",
                "orig_content": "100",
                "orig_min": "",
                "orig_max": "",
                "orig_unit": "mg/100g",
                "orig_food_common_name": "Test berry, raw",
                "orig_food_part": "fruit",
                "orig_source_name": "Journal source",
                "orig_citation": "",
                "citation": "",
                "citation_type": "",
                "orig_method": "",
                "orig_unit_expression": "",
                "preparation_type": "raw",
                "export": "1",
            },
        ],
    )
    manifest = build_manifest(
        data_dir,
        {"foods": "foods.csv", "compounds": "compounds.csv", "contents": "contents.csv"},
    )
    (data_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    crosswalk_path = tmp_path / "crosswalk.json"
    crosswalk_path.write_text(
        json.dumps(
            {
                "matches": [
                    {
                        "local_food_source_id": "R02",
                        "local_food_source_code": "K-FIND:TEST:1",
                        "foodb_food_public_id": "FDB000041",
                        "match_method": "manual_review",
                        "review_note": "Synthetic fixture mapping",
                        "review_source_url": "https://example.org/mapping-evidence",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    database_url = f"sqlite:///{(tmp_path / 'foodb.sqlite3').as_posix()}"
    create_schema(database_url)
    session = get_session_factory(database_url)()
    session.add(Resource(id="R05", name="FooDB", layer="bioactive"))
    session.add(Resource(id="R02", name="Local foods", layer="composition"))
    session.flush()
    session.add(Food(source_id="R02", source_code="K-FIND:TEST:1", name="테스트 베리"))
    session.commit()
    food = session.scalar(select(Food).where(Food.source_code == "K-FIND:TEST:1"))

    counts = import_foodb(data_dir, crosswalk_path, database_url)
    assert counts == {
        "foods_mapped": 1,
        "compounds_seen": 2,
        "content_rows_seen": 2,
        "quantified_rows": 1,
        "non_quantified_rows": 1,
        "inserted": 2,
        "updated": 0,
    }
    session.expire_all()
    measurements = list(
        session.scalars(
            select(FoodCompoundMeasurement).where(FoodCompoundMeasurement.food_id == food.id)
        )
    )
    assert {row.status for row in measurements} == {"quantified", "detected"}
    compound = session.scalar(
        select(Component).where(Component.component_key == "FOODB:FDB000701")
    )
    assert compound is not None
    assert compound.metadata_json["external_identifiers"]["pubchem_id"] == "12345"
    assert compound.inchi_key == "ABCDEFGHIJKLMN-OPQRSTUVWX-Y"

    result = analyze_meal(session, [{"food_id": food.id, "amount_g": 50.0}])
    assert result["status"] == "partial"
    assert "원래 단위·범위·인용 정보" in result["interpretation"]
    assert len(result["components"]) == 1
    assert result["components"][0]["amount"] == pytest.approx(30.0)
    source_record = result["components"][0]["source_records"][0]
    assert source_record["release"] == "FooDB-2020-04-07"
    assert source_record["basis"] == "source_100g"
    assert source_record["source_minimum_per_100g"] == 40
    assert "not statistical confidence intervals" in source_record["source_metadata"][
        "content_interpretation"
    ]
    assert any(row["status"] == "detected" for row in result["unquantified"])

    repeated = import_foodb(data_dir, crosswalk_path, database_url)
    assert repeated["inserted"] == 0 and repeated["updated"] == 2
    session.close()


def test_foodb_import_rejects_changed_source_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "foodb"
    data_dir.mkdir()
    for table_name, filename, header in (
        ("foods", "foods.csv", "id,public_id,name\n1,FDB000001,Apple\n"),
        ("compounds", "compounds.csv", "id,public_id,name,status\n1,FDB000002,Test,0\n"),
        (
            "contents",
            "contents.csv",
            "id,source_id,source_type,food_id,orig_content,orig_unit\n1,1,Compound,1,1,mg/100g\n",
        ),
    ):
        (data_dir / filename).write_text(header, encoding="utf-8")
    manifest = build_manifest(
        data_dir,
        {"foods": "foods.csv", "compounds": "compounds.csv", "contents": "contents.csv"},
    )
    (data_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (data_dir / "compounds.csv").write_text("changed", encoding="utf-8")
    crosswalk = tmp_path / "crosswalk.json"
    crosswalk.write_text('{"matches":[{}]}', encoding="utf-8")

    with pytest.raises(ValueError, match="size mismatch|SHA-256 mismatch"):
        import_foodb(data_dir, crosswalk, f"sqlite:///{(tmp_path / 'unused.sqlite3').as_posix()}")
