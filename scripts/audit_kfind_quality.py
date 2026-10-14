from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nutrigenomics.composition import normalize_unit
from nutrigenomics.kfind import NUTRIENT_CROSSWALK, food_identity, validate_selection


def _read_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _food_key_counts(foods: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    tokens: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for food in foods:
        for token in {food["name"], *(food.get("aliases") or [])}:
            if token:
                tokens[str(token).strip().casefold()].append(food)
    return {
        token: rows
        for token, rows in tokens.items()
        if len({row["id"] for row in rows}) > 1
    }


def audit(database: Path, selection_path: Path) -> dict[str, Any]:
    database = database.expanduser().resolve()
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    selected = validate_selection(selection)
    expected_foods = {
        food_identity(str(item["dataset_id"]), str(item["food_code"]), str(item["food_name"]))[0]: item
        for item in selected
    }
    uri = f"file:{database.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        resource_rows = connection.execute(
            "SELECT * FROM resource WHERE id = 'R02'"
        ).fetchall()
        food_rows = connection.execute(
            "SELECT * FROM food WHERE source_id = 'R02' ORDER BY source_code"
        ).fetchall()
        composition_rows = connection.execute(
            """SELECT cm.*, f.source_code, f.name AS food_name,
                      f.active_release, f.metadata_json AS food_metadata_json,
                      c.component_key, c.name AS component_name,
                      c.canonical_unit, c.metadata_json AS component_metadata_json
               FROM composition_measurement cm
               JOIN food f ON f.id = cm.food_id
               JOIN component c ON c.id = cm.component_id
               WHERE cm.source_id = 'R02'
               ORDER BY f.source_code, c.component_key, cm.release"""
        ).fetchall()

    resource = dict(resource_rows[0]) if resource_rows else {}
    resource_metadata = _read_json(resource.get("metadata_json"))
    foods = []
    for row in food_rows:
        item = dict(row)
        item["metadata_json"] = _read_json(item.get("metadata_json"))
        item["aliases"] = json.loads(item["aliases"] or "[]")
        foods.append(item)

    rows = []
    for row in composition_rows:
        item = dict(row)
        item["metadata_json"] = _read_json(item.get("metadata_json"))
        item["food_metadata_json"] = _read_json(item.get("food_metadata_json"))
        rows.append(item)

    food_by_source_code = {food["source_code"]: food for food in foods}
    food_mismatches = []
    for source_code, expected in expected_foods.items():
        actual = food_by_source_code.get(source_code)
        if actual is None:
            food_mismatches.append({"source_code": source_code, "problem": "missing_from_db"})
            continue
        metadata = actual["metadata_json"]
        checks = {
            "display_name": actual["name"] == expected["name"],
            "dataset_id": str(metadata.get("kfind_dataset_id")) == str(expected["dataset_id"]),
            "provider_food_code": str(metadata.get("provider_food_code")) == str(expected["food_code"]),
            "provider_food_name": metadata.get("provider_food_name") == expected["food_name"],
            "active_release_present": bool(actual["active_release"]),
        }
        failed = [key for key, passed in checks.items() if not passed]
        if failed:
            food_mismatches.append(
                {"source_code": source_code, "problem": "selection_identity_mismatch", "failed": failed}
            )
    unexpected_foods = sorted(set(food_by_source_code) - set(expected_foods))

    expected_components = {
        f"FDC-NUTRIENT:{definition['fdc_id']}": {"field": field, **definition}
        for field, definition in NUTRIENT_CROSSWALK.items()
    }
    status_counts = Counter(row["status"] for row in rows)
    basis_counts = Counter(row["basis"] for row in rows)
    unit_counts = Counter(row["unit"] for row in rows)
    release_counts = Counter(row["release"] for row in rows)
    group_counts = Counter(
        (row["food_metadata_json"].get("kfind_dataset_id"), row["status"])
        for row in rows
    )

    issues: dict[str, list[dict[str, Any]]] = defaultdict(list)
    observed_grain: Counter[tuple[Any, ...]] = Counter()
    per_food: dict[int, dict[str, Any]] = {}
    for food in foods:
        per_food[food["id"]] = {
            "food_id": food["id"],
            "name": food["name"],
            "source_code": food["source_code"],
            "dataset_id": food["metadata_json"].get("kfind_dataset_id"),
            "active_release": food["active_release"],
            "measurement_count": 0,
            "status_counts": Counter(),
            "missing_components": [],
        }

    expected_fields = set(NUTRIENT_CROSSWALK)
    for row in rows:
        food = per_food.get(row["food_id"])
        metadata = row["metadata_json"]
        field = metadata.get("provider_nutrient_field")
        expected_component_key = (
            f"FDC-NUTRIENT:{NUTRIENT_CROSSWALK[field]['fdc_id']}" if field in NUTRIENT_CROSSWALK else None
        )
        observed_grain[(row["food_id"], row["component_id"], row["source_id"], row["source_ref"], row["release"])] += 1
        if food:
            food["measurement_count"] += 1
            food["status_counts"][row["status"]] += 1
        record_ref = row["source_ref"]
        if row["basis"] != "source_100g":
            issues["unexpected_basis"].append({"food": row["food_name"], "component": row["component_key"], "basis": row["basis"]})
        if row["active_release"] != row["release"]:
            issues["inactive_or_mismatched_release_rows"].append({"food": row["food_name"], "row_release": row["release"], "active_release": row["active_release"]})
        if normalize_unit(row["unit"]) != normalize_unit(row["canonical_unit"]):
            issues["unit_vs_canonical_unit_mismatch"].append({"food": row["food_name"], "component": row["component_key"], "unit": row["unit"], "canonical_unit": row["canonical_unit"]})
        if row["status"] not in {"measured", "measured_zero", "not_measured", "below_detection", "unparsed_source_value", "source_conflict"}:
            issues["unknown_status"].append({"food": row["food_name"], "component": row["component_key"], "status": row["status"]})
        if row["status"] in {"measured", "measured_zero"} and row["amount_per_100g"] is None:
            issues["quantified_without_amount"].append({"food": row["food_name"], "component": row["component_key"], "status": row["status"]})
        if row["status"] == "measured_zero" and row["amount_per_100g"] != 0:
            issues["measured_zero_nonzero_amount"].append({"food": row["food_name"], "component": row["component_key"], "amount": row["amount_per_100g"]})
        if row["status"] not in {"measured", "measured_zero"} and row["amount_per_100g"] is not None:
            issues["nonquantified_with_amount"].append({"food": row["food_name"], "component": row["component_key"], "status": row["status"], "amount": row["amount_per_100g"]})
        if expected_component_key is None or row["component_key"] != expected_component_key:
            issues["field_component_crosswalk_mismatch"].append({"food": row["food_name"], "component": row["component_key"], "source_field": field, "expected_component": expected_component_key})
        if record_ref != f"{row['source_code']}:nutrient:{field}":
            issues["source_ref_mismatch"].append({"food": row["food_name"], "source_ref": record_ref, "source_field": field})
        required_metadata = (
            "provider_dataset_id", "provider_food_code", "provider_food_name",
            "provider_nutrient_field", "provider_value_raw", "provider_basis",
            "data_production_method", "component_standard_identifier", "source_record_sha256",
        )
        missing_metadata = [key for key in required_metadata if key not in metadata]
        if missing_metadata:
            issues["measurement_provenance_missing"].append({"food": row["food_name"], "component": row["component_key"], "fields": missing_metadata})
        source_hash = metadata.get("source_record_sha256", "")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            issues["invalid_source_record_hash"].append({"food": row["food_name"], "component": row["component_key"]})
        if food and metadata.get("provider_food_code") != row["food_metadata_json"].get("provider_food_code"):
            issues["food_measurement_identity_mismatch"].append({"food": row["food_name"], "component": row["component_key"]})

    grain_duplicates = [list(key) for key, count in observed_grain.items() if count != 1]
    for record in per_food.values():
        found_fields = {
            row["metadata_json"].get("provider_nutrient_field")
            for row in rows if row["food_id"] == record["food_id"]
        }
        record["missing_components"] = sorted(expected_fields - found_fields)
        record["status_counts"] = dict(sorted(record["status_counts"].items()))

    component_presence = Counter(row["component_key"] for row in rows)
    component_crosswalk_gaps = sorted(
        key for key in expected_components if component_presence.get(key, 0) != len(foods)
    )
    name_collisions = _food_key_counts(foods)
    collisions = {
        token: sorted({food["name"] for food in collision_rows})
        for token, collision_rows in sorted(name_collisions.items())
    }

    release_manifest = dict(resource_metadata.get("kfind_dataset_releases", {}))
    for food in foods:
        metadata = food["metadata_json"]
        dataset_id = str(metadata.get("kfind_dataset_id", ""))
        if dataset_id and dataset_id not in release_manifest:
            release_manifest[dataset_id] = {
                "release": food["active_release"],
                "local_path": metadata.get("raw_release_path"),
                "sha256": metadata.get("raw_release_sha256", {}),
            }
    raw_release_checks = []
    for dataset_id, manifest in sorted(release_manifest.items()):
        local_path = Path(manifest.get("local_path", ""))
        file_results = []
        expected_hashes = manifest.get("sha256", {})
        if local_path.is_dir():
            for filename, expected_hash in sorted(expected_hashes.items()):
                path = local_path / filename
                exists = path.is_file()
                actual_hash = _sha256_file(path) if exists else None
                file_results.append({"file": filename, "exists": exists, "hash_matches": actual_hash == expected_hash if exists else None})
        raw_release_checks.append({
            "dataset_id": dataset_id,
            "release": manifest.get("release"),
            "path": str(local_path),
            "directory_exists": local_path.is_dir(),
            "expected_file_count": len(expected_hashes),
            "checked_file_count": sum(1 for item in file_results if item["exists"]),
            "files": file_results,
        })

    refuse_values = Counter()
    for food in foods:
        raw_refuse = food["metadata_json"].get("refuse_percent")
        if raw_refuse is None or str(raw_refuse).strip() == "":
            refuse_values["missing"] += 1
        else:
            refuse_values["present"] += 1

    expected_measurements = len(selected) * len(NUTRIENT_CROSSWALK)
    quantitative = status_counts.get("measured", 0) + status_counts.get("measured_zero", 0)
    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "database": str(database),
        "selection_version": selection.get("version"),
        "resource_found": bool(resource),
        "resource_release": resource.get("release"),
        "selected_food_count": len(selected),
        "database_food_count": len(foods),
        "expected_nutrients_per_food": len(NUTRIENT_CROSSWALK),
        "expected_measurement_count": expected_measurements,
        "actual_measurement_count": len(rows),
        "quantified_measurement_count": quantitative,
        "quantified_fraction": round(quantitative / len(rows), 4) if rows else None,
        "status_counts": dict(sorted(status_counts.items())),
        "dataset_status_counts": {
            str(dataset): dict(sorted((status, count) for (group_dataset, status), count in group_counts.items() if group_dataset == dataset))
            for dataset in sorted({key[0] for key in group_counts})
        },
        "basis_counts": dict(sorted((str(key), value) for key, value in basis_counts.items())),
        "unit_counts": dict(sorted((str(key), value) for key, value in unit_counts.items())),
        "release_counts": dict(sorted((str(key), value) for key, value in release_counts.items())),
        "food_selection_mismatches": food_mismatches,
        "unexpected_food_source_codes": unexpected_foods,
        "duplicate_grain_keys": grain_duplicates,
        "component_crosswalk_gaps": component_crosswalk_gaps,
        "component_presence_counts": dict(sorted(component_presence.items())),
        "ambiguous_search_tokens": collisions,
        "refuse_percent_present_missing": dict(refuse_values),
        "raw_release_checks": raw_release_checks,
        "issues": {key: values for key, values in sorted(issues.items())},
        "per_food": sorted(per_food.values(), key=lambda item: item["name"]),
    }
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# K-FIND 50종 데이터 품질 감사",
        "",
        f"- 생성 시각(UTC): {summary['generated_utc']}",
        f"- DB: `{summary['database']}` (읽기 전용 연결)",
        f"- 선정 목록 버전: `{summary['selection_version']}`",
        "",
        "## 커버리지",
        "",
        "| 검사 | 결과 |",
        "|---|---:|",
        f"| 선정 음식 / DB 음식 | {summary['selected_food_count']} / {summary['database_food_count']} |",
        f"| 음식당 기대 영양소 | {summary['expected_nutrients_per_food']} |",
        f"| 기대 / 실제 측정 레코드 | {summary['expected_measurement_count']} / {summary['actual_measurement_count']} |",
        f"| 정량 가능 (`measured` + `measured_zero`) | {summary['quantified_measurement_count']} ({summary['quantified_fraction']:.1%}) |",
        f"| 단위별 레코드 | `{json.dumps(summary['unit_counts'], ensure_ascii=False)}` |",
        f"| 기준별 레코드 | `{json.dumps(summary['basis_counts'], ensure_ascii=False)}` |",
        f"| 상태별 레코드 | `{json.dumps(summary['status_counts'], ensure_ascii=False)}` |",
        f"| 버전별 레코드 | `{json.dumps(summary['release_counts'], ensure_ascii=False)}` |",
        "",
        "## 추적성 및 정합성",
        "",
        f"- 선정 목록과 DB 식별 불일치: {len(summary['food_selection_mismatches'])}",
        f"- 선정 외 음식 코드: {len(summary['unexpected_food_source_codes'])}",
        f"- 중복 grain key: {len(summary['duplicate_grain_keys'])}",
        f"- 영양소 crosswalk 누락/불완전: {len(summary['component_crosswalk_gaps'])}",
        f"- 모호한 음식 검색 토큰: {len(summary['ambiguous_search_tokens'])}",
        f"- 원본 REFUSE 값 존재/결측 음식: `{json.dumps(summary['refuse_percent_present_missing'], ensure_ascii=False)}`",
        "",
        "원본 조성값은 DB에 기록된 `source_100g` 기준을 그대로 사용했다. `REFUSE` 값은 추적용으로 보존되며 현재 섭취량 계산에서 가식부 환산을 적용하지 않는다. 화면 입력량이 가식부 g라면 원자료의 100g 기준과 같은 중량 의미인지 확인되기 전까지는 해석상 주의가 필요하다.",
        "",
        "원천 컬럼과 업데이트 주기는 [공공데이터포털의 전국통합식품영양성분정보 표준](https://www.data.go.kr/data/15100064/standard.do)에서 확인했다. 이 표준은 영양성분 기준량과 폐기율을 별도 필드로 제공하고 K-FIND·농촌진흥청 등 관계기관 자료를 포함한다.",
        "",
        "### 원본 파일 재확인",
        "",
    ]
    for release in summary["raw_release_checks"]:
        lines.append(
            f"- `{release['dataset_id']}` / `{release['release']}`: 경로 존재 `{release['directory_exists']}`, "
            f"파일 해시 대조 {sum(1 for item in release['files'] if item['hash_matches'] is True)}/"
            f"{release['expected_file_count']}"
        )
    if not summary["raw_release_checks"]:
        lines.append("- DB 자원 메타데이터에서 원본 release manifest를 찾지 못함")
    lines.extend(["", "## 음식별 커버리지", "", "| 음식 | 상태별 레코드 | 누락 성분 수 |", "|---|---|---:|"])
    for food in summary["per_food"]:
        lines.append(
            f"| {food['name']} | `{json.dumps(food['status_counts'], ensure_ascii=False)}` | "
            f"{len(food['missing_components'])} |"
        )
    lines.extend(["", "## 발견된 구조적 오류", ""])
    if summary["issues"]:
        for key, values in summary["issues"].items():
            lines.append(f"- `{key}`: {len(values)}건")
    else:
        lines.append("- 검사한 레코드 수준의 상태·단위·출처·교차표 불일치가 발견되지 않음")
    lines.extend([
        "",
        "## 해석과 다음 조치",
        "",
        "- 정량률은 값이 `measured` 또는 명시적 0인 행의 비율이며, 미측정·검출한계 미만을 0으로 처리하지 않았다.",
        "- 가식부 중량과 K-FIND 100g 공표 기준의 관계, 음식별 조리/수율 적용 가능성은 식품별로 검증한 뒤 계산 기능에 반영해야 한다.",
        "- 아래 JSON에는 음식별 성분 누락 목록, 정합성 오류의 상세 행, 검색 별칭 충돌, 원본 파일 해시 결과가 포함된다.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only quality audit of the frozen K-FIND 50-food import")
    parser.add_argument("--database", type=Path, required=True, help="SQLite database file")
    parser.add_argument("--selection-file", type=Path, default=Path("config/kfind_food_selection.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    summary = audit(args.database, args.selection_file)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.output_dir / f"kfind-50-food-quality-{stamp}.json"
    markdown_path = args.output_dir / f"kfind-50-food-quality-{stamp}.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path), "summary": {
        key: summary[key] for key in (
            "selected_food_count", "database_food_count", "expected_measurement_count",
            "actual_measurement_count", "quantified_measurement_count", "quantified_fraction",
            "status_counts", "basis_counts", "unit_counts", "release_counts",
            "food_selection_mismatches", "unexpected_food_source_codes",
            "duplicate_grain_keys", "component_crosswalk_gaps", "ambiguous_search_tokens",
            "refuse_percent_present_missing", "issues",
        )
    }}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
