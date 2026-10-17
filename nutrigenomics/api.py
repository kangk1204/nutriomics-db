from __future__ import annotations

import csv
import io
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Generator, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .analysis_downloads import analysis_auxiliary_response
from .annotations import search_annotations
from .bridge import (
    get_provenance,
    list_interventions,
    meal_evidence,
    search_assertions,
    study_bridge_detail,
)
from .composition import analyze_meal
from .explorer import attach_explorer
from .personal_impact import personal_impact_status
from .db import create_schema, get_session_factory
from .models import (
    AnalysisRun,
    Component,
    Food,
    GeoSeries,
    KnowledgeLink,
    MealItem,
    MealRecord,
    StudyEffect,
)
from .schemas import MealAnalysisInput, exportable_payload
from .settings import DATA_DIR, DATABASE_URL

LOCAL_TZ = ZoneInfo("Asia/Seoul")
app = FastAPI(
    title="Nutrigenomics Research DB",
    version="0.1.0",
    description=(
        "연구용 음식 성분량과 사람 연구 근거를 조회합니다. 개인별 발현·methylation 또는 질병 위험을 예측하지 않습니다."
    ),
)

# The explorer reads an independent release; an unavailable release leaves the
# established meal/study API usable.
attach_explorer(app)


@app.on_event("startup")
def _startup() -> None:
    storage = os.environ.get('NUTRIGENOMICS_MEAL_STORAGE_DATABASE_URL')
    if storage:
        create_schema(storage)
    else:
        create_schema()


def get_session() -> Generator[Session, None, None]:
    session = get_session_factory(DATABASE_URL)()
    try:
        yield session
    finally:
        session.close()


def get_meal_storage_session(research: Session = Depends(get_session)) -> Generator[Session, None, None]:
    """Optional mutable meal history beside an unchanged research snapshot."""
    url = os.environ.get('NUTRIGENOMICS_MEAL_STORAGE_DATABASE_URL')
    if not url:
        yield research
        return
    with get_session_factory(url)() as session:
        yield session


@app.get("/api/v1/personal-impact/status")
def personal_impact_readiness() -> dict:
    """Task/data readiness only; no personal records or model predictions."""
    return personal_impact_status()


def _food_dict(food: Food) -> dict:
    return {
        "id": food.id,
        "source_id": food.source_id,
        "source_code": food.source_code,
        "name": food.name,
        "name_en": food.name_en,
        "aliases": food.aliases or [],
        "food_group": food.food_group,
        "edible_state": food.edible_state,
        "preparation_state": food.preparation_state,
    }


def _effect_dict(effect: StudyEffect) -> dict:
    return {
        "series_accession": effect.series_accession,
        "feature_type": effect.feature_type,
        "feature_id": effect.feature_id,
        "feature_label": effect.feature_label,
        "contrast": effect.contrast,
        "tissue": effect.tissue,
        "timepoint": effect.timepoint,
        "intervention": effect.intervention,
        "dose": effect.dose,
        "duration": effect.duration,
        "effect_estimate": effect.effect_estimate,
        "standard_error": effect.standard_error,
        "p_value": effect.p_value,
        "q_value": effect.q_value,
        "effect_unit": effect.effect_unit,
        "status": effect.status,
        "n_people": effect.n_people,
        "n_samples": effect.n_samples,
        "analysis_version": effect.analysis_version,
        "metadata": effect.metadata_json or {},
        "export_allowed": (effect.metadata_json or {}).get("export_allowed", True),
    }


def _evidence_for_components(session: Session, component_ids: set[int]) -> list[dict]:
    if not component_ids:
        return []
    components = list(session.scalars(select(Component).where(Component.id.in_(component_ids))))
    keys = {component.component_key for component in components}
    keys.update(component.standard_identifier for component in components if component.standard_identifier)
    if not keys:
        return []
    links = list(
        session.scalars(
            select(KnowledgeLink)
            .where(KnowledgeLink.subject_type == "component", KnowledgeLink.subject_id.in_(keys))
            .order_by(KnowledgeLink.id)
            .limit(500)
        )
    )
    effects_by_feature: dict[tuple[str, str, str, str, str], list[StudyEffect]] = {}
    output = []
    for link in links:
        effects: list[StudyEffect] = []
        attachment_status = "not_an_effect_reference"
        if link.target_type in {"gene", "pathway", "cpg", "dmr", "clinical"}:
            metadata = link.metadata_json or {}
            attachment_status = "ambiguous_effect_reference"
            if all(metadata.get(key) for key in ("series_accession", "contrast", "analysis_version")):
                accession = str(metadata["series_accession"])
                feature_type = str(metadata.get("feature_type") or link.target_type)
                cache_key = (
                    accession, feature_type, link.target_id,
                    str(metadata["contrast"]), str(metadata["analysis_version"]),
                )
                if cache_key not in effects_by_feature:
                    effects_by_feature[cache_key] = list(
                        session.scalars(
                            select(StudyEffect).where(
                                StudyEffect.series_accession == accession,
                                StudyEffect.feature_type == feature_type,
                                StudyEffect.feature_id == link.target_id,
                                StudyEffect.contrast == cache_key[3],
                                StudyEffect.analysis_version == cache_key[4],
                            )
                        )
                    )
                effects = effects_by_feature[cache_key]
                attachment_status = "exact_effect_reference" if effects else "effect_not_found"
        output.append(
            {
                "source": link.source,
                "source_ref": link.source_ref,
                "subject_type": link.subject_type,
                "subject_id": link.subject_id,
                "relation": link.relation,
                "target_type": link.target_type,
                "target_id": link.target_id,
                "evidence_kind": link.evidence_kind,
                "organism": link.organism,
                "tissue": link.tissue,
                "dose": link.dose,
                "duration": link.duration,
                "pubmed_id": link.pubmed_id,
                "metadata": link.metadata_json or {},
                "effects": [_effect_dict(effect) for effect in effects],
                "effect_attachment_status": attachment_status,
                "export_allowed": (link.metadata_json or {}).get("export_allowed", True),
            }
        )
    return output


def _meal_result(session: Session, payload: MealAnalysisInput) -> dict:
    try:
        result = analyze_meal(
            session,
            [item.model_dump() for item in payload.items],
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    result["evidence"] = _evidence_for_components(
        session,
        {int(row["component_id"]) for row in result["components"]},
    )
    result["evidence_status"] = (
        "matched" if result["evidence"] else "no_linked_evidence_in_database"
    )
    context = payload.reference_context.model_dump() if payload.reference_context else None
    result.update(meal_evidence(session, result, context))
    result["reference_context"] = context
    return result


def _as_utc_naive(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _date_bounds_utc(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    lower = None
    upper = None
    if start is not None:
        lower = datetime.combine(start, time.min, LOCAL_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    if end is not None:
        next_day = datetime.combine(end, time.min, LOCAL_TZ).astimezone(timezone.utc).replace(tzinfo=None)
        upper = next_day + timedelta(days=1)
    return lower, upper


def _meal_dict(meal: MealRecord) -> dict:
    timestamp = meal.consumed_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return {
        "id": meal.id,
        "consumed_at": timestamp.isoformat(),
        "created_at": (
            meal.created_at.replace(tzinfo=timezone.utc).isoformat()
            if meal.created_at and meal.created_at.tzinfo is None
            else meal.created_at.isoformat() if meal.created_at else None
        ),
        "note": meal.note,
        "items": [
            {
                "food_id": item.food_id,
                "food_name": item.name_snapshot,
                "preparation_state": item.preparation_snapshot,
                "amount_g": item.amount_g,
            }
            for item in meal.items
        ],
        "analysis": meal.calculation_json,
    }


def _csv_response(rows: list[dict], filename: str, fieldnames: list[str] | None = None) -> Response:
    if fieldnames is None:
        fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else "" if value is None else value
                for key, value in row.items()
            }
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _json_response(payload: dict | list, filename: str) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "nutrigenomics-research-db", "version": "0.1.0"}


@app.get("/api/v1/foods")
def search_foods(
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    stmt = select(Food).options(selectinload(Food.compositions)).order_by(Food.name).limit(limit)
    if query.strip():
        pattern = f"%{query.strip()}%"
        aliases = func.json_each(Food.aliases).table_valued("key", "value").alias("food_alias")
        alias_match = (
            select(1)
            .select_from(aliases)
            .where(aliases.c.value.ilike(pattern))
            .exists()
        )
        stmt = (
            select(Food)
            .where(
                or_(
                    Food.name.ilike(pattern),
                    Food.name_en.ilike(pattern),
                    alias_match,
                )
            )
            .options(selectinload(Food.compositions))
            .order_by(Food.name)
            .limit(limit)
        )
    foods = list(session.scalars(stmt).unique())
    return {"items": [_food_dict(food) for food in foods], "count": len(foods)}


@app.post("/api/v1/meals/analyze")
def analyze_meal_endpoint(
    payload: MealAnalysisInput, session: Session = Depends(get_session)
) -> dict:
    return _meal_result(session, payload)


@app.post("/api/v1/meals", status_code=201)
def save_meal(payload: MealAnalysisInput, session: Session = Depends(get_meal_storage_session),
              research: Session = Depends(get_session)) -> dict:
    result = _meal_result(research, payload)
    foods = {
        food.id: food
        for food in research.scalars(
            select(Food).where(Food.id.in_([item.food_id for item in payload.items]))
        )
    }
    if session is not research:
        for food in foods.values():
            stored = session.get(Food, food.id)
            if stored is None or (stored.source_id, stored.source_code) != (food.source_id, food.source_code):
                raise HTTPException(409, '식사 기록의 음식 식별자가 현재 분석자료와 일치하지 않습니다.')
    meal = MealRecord(
        consumed_at=_as_utc_naive(payload.consumed_at),
        note=payload.note,
        calculation_json=result,
        items=[
            MealItem(
                food_id=item.food_id,
                amount_g=item.amount_g,
                name_snapshot=foods[item.food_id].name,
                preparation_snapshot=foods[item.food_id].preparation_state,
            )
            for item in payload.items
        ],
    )
    session.add(meal)
    session.commit()
    session.refresh(meal)
    return _meal_dict(meal)


@app.get("/api/v1/meals")
def list_meals(
    start: date | None = None,
    end: date | None = None,
    session: Session = Depends(get_meal_storage_session),
) -> dict:
    if start is not None and end is not None and end < start:
        raise HTTPException(status_code=422, detail="end must be the same as or later than start")
    lower, upper = _date_bounds_utc(start, end)
    stmt = select(MealRecord).options(selectinload(MealRecord.items)).order_by(MealRecord.consumed_at.desc())
    if lower is not None:
        stmt = stmt.where(MealRecord.consumed_at >= lower)
    if upper is not None:
        stmt = stmt.where(MealRecord.consumed_at < upper)
    meals = list(session.scalars(stmt.limit(500)).unique())
    return {"items": [_meal_dict(meal) for meal in meals], "count": len(meals)}


@app.get("/api/v1/meals/totals")
def meal_totals(
    start: date,
    end: date,
    session: Session = Depends(get_meal_storage_session),
) -> dict:
    if end < start:
        raise HTTPException(status_code=422, detail="end must be the same as or later than start")
    lower, upper = _date_bounds_utc(start, end)
    meals = list(
        session.scalars(
            select(MealRecord)
            .where(MealRecord.consumed_at >= lower, MealRecord.consumed_at < upper)
            .order_by(MealRecord.consumed_at)
        )
    )
    daily: dict[str, dict[tuple[int, str], dict]] = {}
    for meal in meals:
        timestamp = meal.consumed_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        day = timestamp.astimezone(LOCAL_TZ).date().isoformat()
        bucket = daily.setdefault(day, {})
        for component in meal.calculation_json.get("components", []):
            key = (int(component["component_id"]), str(component["unit"]))
            record = bucket.setdefault(
                key,
                {
                    "component_id": key[0],
                    "component_key": component["component_key"],
                    "name": component["name"],
                    "category": component["category"],
                    "unit": key[1],
                    "amount": 0.0,
                },
            )
            record["amount"] += float(component["amount"])
    total_meals = len(meals)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "meal_count": total_meals,
        "days": [
            {
                "date": day,
                "components": [
                    {**component, "amount": round(component["amount"], 9)}
                    for component in sorted(rows.values(), key=lambda row: (row["name"].casefold(), row["unit"]))
                ],
            }
            for day, rows in sorted(daily.items())
        ],
        "note": "누적량은 입력한 식사 중 정량 가능한 성분만 합산합니다.",
    }


@app.get("/api/v1/export/meals")
def export_meals(
    start: date | None = None,
    end: date | None = None,
    format: Literal["json", "csv"] = "json",
    session: Session = Depends(get_meal_storage_session),
) -> Response:
    if start is not None and end is not None and end < start:
        raise HTTPException(status_code=422, detail="end must be the same as or later than start")
    lower, upper = _date_bounds_utc(start, end)
    stmt = (
        select(MealRecord)
        .options(selectinload(MealRecord.items))
        .order_by(MealRecord.consumed_at)
        .limit(10_000)
    )
    if lower is not None:
        stmt = stmt.where(MealRecord.consumed_at >= lower)
    if upper is not None:
        stmt = stmt.where(MealRecord.consumed_at < upper)
    meals = [_meal_dict(meal) for meal in session.scalars(stmt).unique()]
    safe_result = exportable_payload({"items": meals, "count": len(meals)})
    meals = safe_result["items"]
    if format == "json":
        return _json_response(safe_result, "nutrigenomics-meals.json")
    rows = []
    for meal in meals:
        item_summary = meal["items"]
        components = meal["analysis"].get("components", [])
        if not components:
            rows.append(
                {
                    "meal_id": meal["id"],
                    "consumed_at": meal["consumed_at"],
                    "note": meal["note"],
                    "food_items": item_summary,
                    "analysis_status": meal["analysis"].get("status"),
                    "export_withheld_records": safe_result["export_withheld_records"],
                    "observed_evidence": meal["analysis"].get("observed_evidence", []),
                    "mechanistic_hypotheses": meal["analysis"].get("mechanistic_hypotheses", []),
                    "evidence_gaps": meal["analysis"].get("evidence_gaps", []),
                    "provenance": meal["analysis"].get("provenance", []),
                    "applicability": meal["analysis"].get("applicability", []),
                    "reference_context": meal["analysis"].get("reference_context"),
                }
            )
        for component in components:
            rows.append(
                {
                    "meal_id": meal["id"],
                    "consumed_at": meal["consumed_at"],
                    "note": meal["note"],
                    "food_items": item_summary,
                    "analysis_status": meal["analysis"].get("status"),
                    "component_id": component.get("component_id"),
                    "component_key": component.get("component_key"),
                    "component_name": component.get("name"),
                    "amount": component.get("amount"),
                    "unit": component.get("unit"),
                    "source_records": component.get("source_records", []),
                    "export_withheld_records": safe_result["export_withheld_records"],
                    "observed_evidence": meal["analysis"].get("observed_evidence", []),
                    "mechanistic_hypotheses": meal["analysis"].get("mechanistic_hypotheses", []),
                    "evidence_gaps": meal["analysis"].get("evidence_gaps", []),
                    "provenance": meal["analysis"].get("provenance", []),
                    "applicability": meal["analysis"].get("applicability", []),
                    "reference_context": meal["analysis"].get("reference_context"),
                }
            )
    return _csv_response(rows, "nutrigenomics-meals.csv")


@app.get("/api/v1/evidence")
def search_evidence(
    query: str = Query(default="", max_length=200),
    feature_type: str | None = Query(default=None, max_length=32),
    accession: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=100, ge=1, le=100_000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    layer: str | None = None,
    organism: str | None = None,
    tissue: str | None = None,
    study_design: str | None = None,
    contrast: str | None = None,
    analysis_version: str | None = None,
) -> dict:
    stmt = select(StudyEffect).options(selectinload(StudyEffect.series))
    count_stmt = select(func.count()).select_from(StudyEffect)
    if feature_type:
        stmt = stmt.where(StudyEffect.feature_type == feature_type)
        count_stmt = count_stmt.where(StudyEffect.feature_type == feature_type)
    if accession:
        stmt = stmt.where(StudyEffect.series_accession == accession.upper())
        count_stmt = count_stmt.where(StudyEffect.series_accession == accession.upper())
    for column, value in (
        (StudyEffect.tissue, tissue), (StudyEffect.contrast, contrast),
        (StudyEffect.analysis_version, analysis_version),
    ):
        if value:
            stmt = stmt.where(column == value)
            count_stmt = count_stmt.where(column == value)
    if organism:
        organism_condition = StudyEffect.series.has(GeoSeries.organism == organism)
        stmt = stmt.where(organism_condition)
        count_stmt = count_stmt.where(organism_condition)
    if layer and layer not in {"observed", "molecular_measurement"}:
        stmt = stmt.where(False)
        count_stmt = count_stmt.where(False)
    if study_design:
        # The bridge has reviewed design assertions. Legacy free-text summaries
        # cannot satisfy a categorical design filter reliably.
        stmt = stmt.where(False)
        count_stmt = count_stmt.where(False)
    if query.strip():
        pattern = f"%{query.strip()}%"
        condition = or_(
            StudyEffect.feature_id.ilike(pattern),
            StudyEffect.feature_label.ilike(pattern),
            StudyEffect.contrast.ilike(pattern),
            StudyEffect.intervention.ilike(pattern),
        )
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total_effects = session.scalar(count_stmt) or 0
    stmt = stmt.order_by(
        case((StudyEffect.q_value.is_(None), 1), else_=0), StudyEffect.q_value.asc(), StudyEffect.id.asc()
    )
    effects = list(session.scalars(stmt.offset(offset).limit(limit)).unique())
    links = []
    total_links = 0
    if query.strip():
        pattern = f"%{query.strip()}%"
        link_stmt = select(KnowledgeLink).where(
            or_(
                KnowledgeLink.subject_id.ilike(pattern),
                KnowledgeLink.target_id.ilike(pattern),
                KnowledgeLink.relation.ilike(pattern),
                KnowledgeLink.source_ref.ilike(pattern),
            )
        )
        for column, value in ((KnowledgeLink.organism, organism), (KnowledgeLink.tissue, tissue)):
            if value:
                link_stmt = link_stmt.where(column == value)
        if layer or study_design or contrast or accession or feature_type or analysis_version:
            # Unstructured legacy links do not establish the requested context.
            link_stmt = link_stmt.where(False)
        total_links = session.scalar(select(func.count()).select_from(link_stmt.subquery())) or 0
        links = [
            {
                "source": row.source,
                "source_ref": row.source_ref,
                "subject_type": row.subject_type,
                "subject_id": row.subject_id,
                "relation": row.relation,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "evidence_kind": row.evidence_kind,
                "organism": row.organism,
                "tissue": row.tissue,
                "dose": row.dose,
                "duration": row.duration,
                "pubmed_id": row.pubmed_id,
                "metadata": row.metadata_json or {},
                "export_allowed": (row.metadata_json or {}).get("export_allowed", True),
            }
            for row in session.scalars(link_stmt.order_by(KnowledgeLink.id).offset(offset).limit(limit))
        ]
    assertions = search_assertions(
        session, query=query, layer=layer, organism=organism, tissue=tissue,
        study_design=study_design, contrast=contrast, limit=limit, offset=offset,
        accession=accession, feature_type=feature_type,
        analysis_version=analysis_version,
    )
    annotation_limit = min(limit, 200)
    annotations = {"total": 0, "records": [], "limit": annotation_limit, "offset": offset}
    annotation_layers = {"molecular_identity", "molecular_target", "pathway", "molecular_measurement"}
    if (layer is None or layer in annotation_layers) and not any(
        (accession, feature_type, study_design, contrast, analysis_version)
    ):
        annotations = search_annotations(
            session, query=query or None, layer=layer, organism=organism, tissue=tissue,
            limit=annotation_limit, offset=offset,
        )
    return {
        "effects": [_effect_dict(effect) for effect in effects],
        "links": links,
        "count": len(effects) + len(links),
        "total_effects": total_effects,
        "total_links": total_links,
        "assertions": assertions.get("items", []),
        "total_assertions": assertions.get("total", 0),
        "assertion_count": assertions.get("count", len(assertions.get("items", []))),
        "mechanistic_annotations": annotations["records"],
        "mechanistic_annotation_count": len(annotations["records"]),
        "total_mechanistic_annotations": annotations["total"],
        "annotation_offset": offset,
        "annotation_limit": annotation_limit,
        "annotation_filter_note": "분자 주석은 query·layer·organism·tissue로 검색합니다. 연구·대비·분석 버전 필터가 있으면 표시하지 않습니다.",
        "offset": offset,
        "limit": limit,
        "note": "조회 결과는 해당 조건의 연구 근거이며 개인별 반응 예측값이 아닙니다.",
    }


@app.get("/api/v1/export/evidence")
def export_evidence(
    query: str = Query(default="", max_length=200),
    feature_type: str | None = Query(default=None, max_length=32),
    accession: str | None = Query(default=None, max_length=24),
    format: Literal["json", "csv"] = "json",
    limit: int = Query(default=100_000, ge=1, le=100_000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    layer: str | None = None,
    organism: str | None = None,
    tissue: str | None = None,
    study_design: str | None = None,
    contrast: str | None = None,
    analysis_version: str | None = None,
) -> Response:
    result = search_evidence(
        query=query,
        feature_type=feature_type,
        accession=accession,
        limit=limit,
        offset=offset,
        session=session,
        layer=layer, organism=organism, tissue=tissue,
        study_design=study_design, contrast=contrast,
        analysis_version=analysis_version,
    )
    result = exportable_payload(result)
    result["count"] = len(result["effects"]) + len(result["links"])
    result["assertion_count"] = len(result["assertions"])
    result["mechanistic_annotation_count"] = len(result["mechanistic_annotations"])
    if format == "json":
        return _json_response(result, "nutrigenomics-evidence.json")
    rows = [
        {"record_type": "study_effect", "interpretation_note": result["note"], **effect}
        for effect in result["effects"]
    ] + [
        {"record_type": "knowledge_link", "interpretation_note": result["note"], **link}
        for link in result["links"]
    ] + [
        {"record_type": "evidence_assertion", "interpretation_note": result["note"], **assertion}
        for assertion in result["assertions"]
    ] + [
        {"record_type": "mechanistic_annotation", "interpretation_note": result["note"], **annotation}
        for annotation in result["mechanistic_annotations"]
    ]
    if not rows and result["export_withheld_records"]:
        rows.append({"record_type": "export_notice", "interpretation_note": result["note"]})
    fieldnames = [
        "record_type",
        "interpretation_note",
        "series_accession",
        "feature_type",
        "feature_id",
        "feature_label",
        "contrast",
        "tissue",
        "timepoint",
        "intervention",
        "dose",
        "duration",
        "effect_estimate",
        "standard_error",
        "p_value",
        "q_value",
        "effect_unit",
        "status",
        "n_people",
        "n_samples",
        "analysis_version",
        "subject_type",
        "subject_id",
        "relation",
        "target_type",
        "target_id",
        "evidence_kind",
        "organism",
        "tissue",
        "pubmed_id",
        "source",
        "source_ref",
        "metadata",
        "effects",
        "evidence_id", "id", "layer", "study_id", "exposure", "effect_selector",
        "applicability", "limitations", "provenance", "export_allowed",
    ]
    for row in rows:
        row["export_withheld_records"] = result["export_withheld_records"]
    fieldnames.extend(key for row in rows for key in row if key not in fieldnames)
    return _csv_response(rows, "nutrigenomics-evidence.csv", list(dict.fromkeys(fieldnames)))


@app.get("/api/v1/interventions")
def interventions_endpoint(
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> dict:
    return list_interventions(session, query=query, limit=limit, offset=offset)


@app.get("/api/v1/provenance/{evidence_id}")
def provenance_endpoint(evidence_id: str, session: Session = Depends(get_session)) -> dict:
    result = get_provenance(session, evidence_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Evidence provenance not found")
    return result


@app.get("/api/v1/studies")
def list_studies(
    accession: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    stmt = (
        select(GeoSeries)
        .options(selectinload(GeoSeries.study))
        .order_by(GeoSeries.accession)
    )
    if accession:
        stmt = stmt.where(GeoSeries.accession == accession.upper())
    rows = list(session.scalars(stmt))
    return {
        "items": [
            {
                "accession": row.accession,
                "study_id": row.study_id,
                "study_title": row.study.title,
                "study_design": (row.study.metadata_json or {}).get("study_design"),
                "parent_accession": row.parent_accession,
                "title": row.title,
                "assay_type": row.assay_type,
                "organism": row.organism,
                "platform_ids": row.platform_ids or [],
                "pubmed_id": row.pubmed_id,
                "sample_count": row.sample_count,
                "metadata_verified": row.metadata_verified,
                "assay_data_downloaded": row.assay_data_downloaded,
            }
            for row in rows
        ],
        "count": len(rows),
    }


@app.get("/api/v1/studies/{accession}")
def get_study(accession: str, session: Session = Depends(get_session)) -> dict:
    row = session.scalar(
        select(GeoSeries)
        .where(GeoSeries.accession == accession.upper())
        .options(selectinload(GeoSeries.study), selectinload(GeoSeries.samples))
    )
    if row is None:
        raise HTTPException(status_code=404, detail="GEO series not found")
    samples = sorted(row.samples, key=lambda sample: sample.sample_accession)
    effect_count_total = session.scalar(
        select(func.count()).select_from(StudyEffect).where(StudyEffect.series_accession == row.accession)
    ) or 0
    effects = list(
        session.scalars(
            select(StudyEffect)
            .where(StudyEffect.series_accession == row.accession)
            .order_by(case((StudyEffect.q_value.is_(None), 1), else_=0), StudyEffect.q_value.asc())
            .limit(1000)
        )
    )
    analysis_runs = list(
        session.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.series_accession == row.accession)
            .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id.desc())
        )
    )
    study_metadata = row.study.metadata_json or {}
    series_design = (study_metadata.get("series_design") or {}).get(row.accession, {})
    analysis_sample_counts = [
        {
            "analysis_version": run.analysis_version,
            "status": run.status,
            "people": (run.metadata_json or {}).get("independent_people"),
            "assay_samples": (run.metadata_json or {}).get("sample_count"),
            "source": "analysis run metadata",
        }
        for run in analysis_runs
        if (run.metadata_json or {}).get("independent_people") is not None
        or (run.metadata_json or {}).get("sample_count") is not None
    ]
    return {
        "accession": row.accession,
        "study_id": row.study_id,
        "study_title": row.study.title,
        "parent_accession": row.parent_accession,
        "title": row.title,
        "assay_type": row.assay_type,
        "organism": row.organism,
        "platform_ids": row.platform_ids or [],
        "pubmed_id": row.pubmed_id,
        "sample_count": row.sample_count,
        "counts": {
            "reported_people": series_design.get("reported_people"),
            "reported_people_source": series_design.get("sample_count_basis"),
            "analysis_runs": analysis_sample_counts,
            "geo_sample_records": len(samples),
            "geo_reported_sample_count": row.sample_count,
            "biological_samples_reported": series_design.get("biological_samples"),
            "sample_count_note": (
                "GEO sample record 수와 assay 수는 독립 참가자 수 또는 생물학적 시료 수와 다를 수 있습니다. "
                "특히 two-color array는 한 assay record에 두 생물학적 시료가 포함될 수 있습니다."
            ),
        },
        "metadata_verified": row.metadata_verified,
        "assay_data_downloaded": row.assay_data_downloaded,
        "metadata": row.metadata_json or {},
        "study_design": study_metadata.get("study_design"),
        "study_limitations": study_metadata.get("limitations"),
        "primary_sources": study_metadata.get("primary_sources", []),
        "series_design": series_design,
        "samples": [
            {
                "accession": sample.sample_accession,
                "title": sample.title,
                "characteristics": sample.characteristics or [],
            }
            for sample in samples
        ],
        "effects": [_effect_dict(effect) for effect in effects],
        "effect_count_total": effect_count_total,
        "effects_truncated": effect_count_total > len(effects),
        "analysis_runs": [
            {
                "analysis_version": run.analysis_version,
                "status": run.status,
                "started_at": run.started_at,
                "source_script_path": run.source_script_path,
                "source_script_sha256": run.source_script_sha256,
                "environment_lock_path": run.environment_lock_path,
                "environment_lock_sha256": run.environment_lock_sha256,
                "metadata": run.metadata_json or {},
                "inputs": run.input_manifest_json or [],
                "outputs": run.output_manifest_json or [],
            }
            for run in analysis_runs
        ],
        "metadata_only_notice": (
            "이 Series의 GEO metadata가 수록되어 있습니다. assay 파일은 다운로드·분석으로 확인되기 전까지 별도 표시합니다."
            if not row.assay_data_downloaded
            else None
        ),
        "intervention_evidence": study_bridge_detail(session, row.accession),
    }


@app.get("/api/v1/studies/{accession}/analysis-runs/{analysis_version}/files/{filename}")
def download_analysis_auxiliary_file(
    accession: str,
    analysis_version: str,
    filename: str,
    session: Session = Depends(get_session),
) -> FileResponse:
    return analysis_auxiliary_response(accession, analysis_version, filename, session, DATA_DIR)


@app.delete("/api/v1/meals/{meal_id}", status_code=204)
def delete_meal(meal_id: int, session: Session = Depends(get_session)) -> None:
    meal = session.get(MealRecord, meal_id)
    if meal is None:
        raise HTTPException(status_code=404, detail="Meal record not found")
    session.delete(meal)
    session.commit()
