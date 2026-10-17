"""Bounded evidence, regulation and analysis interfaces for the workbench."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from .explorer import _json, _meta, _public, database
from .explorer_context import configured_path
from .explorer_performance import (
    QueryUnavailable,
    canonical,
    fingerprint,
    indexed_source,
    read_connection,
    selector_key,
)
from .settings import DATA_DIR

router = APIRouter(prefix="/api/v1/explorer", tags=["Evidence workbench"])
LAYERS = {
    "all",
    "eqtm",
    "regulatory_tested",
    "regulatory_predicted",
    "clinical",
    "molecular_target",
    "molecular_identity",
    "composition",
    "pathway",
    "molecular_measurement",
}


def _present(db, table):
    return bool(
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    )


def record(row, db):
    item = dict(row)
    data = _json(item.pop("metadata_json"))
    context = data.get("context", data)
    source_row = db.execute(
        "SELECT metadata_json FROM source WHERE id=?", (item["source_id"],)
    ).fetchone()
    source = _public(_json(source_row[0])) if source_row else {}
    original_p = (
        context.get("p_value_original")
        or context.get("original_p_value")
        or context.get("reported_p")
        or context.get("p_bound")
    )
    if data.get("p_value") == 0 and original_p is None:
        original_p = "source-reported zero; precision/underflow unresolved"
    item.update(
        {
            "effect": data.get("effect", data.get("effect_estimate")),
            "effect_unit": data.get("effect_unit"),
            "p_value": None if data.get("p_value") == 0 else data.get("p_value"),
            "p_value_original": original_p,
            "q_value": data.get("q_value"),
            "hypothesis_family": data.get("hypothesis_family"),
            "ci_low": data.get("ci_low"),
            "ci_high": data.get("ci_high"),
            "n_people": data.get("n_people"),
            "n_samples": data.get("n_samples"),
            "value_status": data.get("value_status", "not_reported"),
            "source_ref": data.get("source_ref", source.get("source_location")),
            "source": source,
            "context": _public(context),
            "study": _public(data.get("study")),
            "expert_review_status": "pending",
            "export_allowed": bool(item["export_allowed"]),
            "metadata": _public(data),
        }
    )
    if _present(db, "workbench_review"):
        import hashlib

        digest = hashlib.sha256(row["metadata_json"].encode()).hexdigest()
        reviews = [
            dict(r)
            for r in db.execute(
                "SELECT reviewer,decision,reviewed_at,evidence_sha256,note FROM workbench_review WHERE evidence_id=? AND evidence_sha256=? ORDER BY reviewed_at DESC",
                (item["id"], digest),
            )
        ]
        item["reviews"] = reviews
        # No reviewer identity or decision is synthesized by the application.
        if reviews:
            item["expert_review_status"] = reviews[0]["decision"]
    return item


def _query(
    seed_id, layer, tissue, study_id, review_status, q, page, page_size, regulatory_only=False
):
    if layer not in LAYERS:
        raise HTTPException(422, "지원하지 않는 근거 계층")
    with database() as db:
        release = _meta(db).get("release_id")
        if not _present(db, "workbench_evidence"):
            return {
                "release_id": release,
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "status": "not_indexed",
                "gaps": ["이 release의 조절 근거는 아직 색인되지 않았습니다."],
            }
        clauses, args = [], []
        if regulatory_only:
            clauses.append("layer IN ('eqtm','regulatory_tested','regulatory_predicted')")
        if seed_id:
            # Materialize this entity's indexed IDs before layer/order filters.
            # Otherwise SQLite can scan a million-row layer for one gene.
            clauses.append(
                "id IN (SELECT id FROM workbench_evidence INDEXED BY wb_subject WHERE subject_id=? UNION SELECT id FROM workbench_evidence INDEXED BY wb_object WHERE object_id=?)"
            )
            args += [seed_id, seed_id]
        for key, value in [
            ("layer", layer if layer != "all" else None),
            ("tissue", tissue),
            ("study_id", study_id),
            ("review_status", review_status),
        ]:
            if value:
                clauses.append(key + "=?")
                args.append(value)
        if q:
            clauses.append(
                "(id LIKE ? ESCAPE '\\' OR subject_id LIKE ? ESCAPE '\\' OR object_id LIKE ? ESCAPE '\\')"
            )
            term = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [term] * 3
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        table = "workbench_evidence" + (
            " INDEXED BY sqlite_autoindex_workbench_evidence_1" if seed_id else ""
        )
        total = db.execute("SELECT count(*) FROM " + table + where, args).fetchone()[0]
        rows = db.execute(
            "SELECT * FROM "
            + table
            + where
            + " ORDER BY layer,study_id,tissue,id LIMIT ? OFFSET ?",
            [*args, page_size, (page - 1) * page_size],
        ).fetchall()
        return {
            "release_id": release,
            "items": [record(r, db) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "status": "indexed",
            "gaps": [] if total else ["조회 조건에 해당하는 수록 근거 없음"],
            "note": "근거 수는 독립 대상자 수가 아닙니다. 외부 조절 근거는 식이 반응이나 인과적 건강 효과가 아닙니다.",
        }


@router.get("/evidence")
def evidence(
    seed_id: str | None = Query(None, max_length=240),
    layer: str = "all",
    tissue: str | None = Query(None, max_length=160),
    study_id: str | None = None,
    review_status: str | None = None,
    q: str = Query("", max_length=160),
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=200),
):
    return _query(seed_id, layer, tissue, study_id, review_status, q, page, page_size)


@router.get("/regulation")
def regulation(
    seed_id: str | None = Query(None, max_length=240),
    layer: str = "all",
    tissue: str | None = None,
    q: str = Query("", max_length=160),
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=200),
):
    if layer not in {"all", "eqtm", "regulatory_tested", "regulatory_predicted"}:
        raise HTTPException(422, "지원하지 않는 조절 근거 계층")
    return _query(seed_id, layer, tissue, None, None, q, page, page_size, regulatory_only=True)


@router.get("/evidence/{identifier}")
def evidence_detail(identifier: str):
    with database() as db:
        if not _present(db, "workbench_evidence"):
            raise HTTPException(404, "근거가 아직 색인되지 않았습니다.")
        row = db.execute("SELECT * FROM workbench_evidence WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, "수록 근거 없음")
        return {"release_id": _meta(db).get("release_id"), **record(row, db)}


@router.get("/coverage")
def coverage():
    with database() as db:
        value = _meta(db)
        layers = (
            dict(db.execute("SELECT layer,count(*) FROM workbench_evidence GROUP BY layer"))
            if _present(db, "workbench_evidence")
            else {}
        )
        tissues = (
            [
                dict(r)
                for r in db.execute(
                    "SELECT tissue,count(*) AS records FROM workbench_evidence GROUP BY tissue ORDER BY records DESC"
                )
            ]
            if layers
            else []
        )
        return {
            "release_id": value.get("release_id"),
            "layers": layers,
            "tissues": tissues,
            "workbench": _public(value.get("workbench", {})),
            "count_definition": "Relation records; repeated features, cohorts and assays are not independent people.",
            "expert_review_status": "pending",
        }


@router.get("/loci")
def loci(
    seed_id: str = Query(..., max_length=240),
    genome_build: str = "GRCh38/hg38",
    max_records: int = Query(100, ge=1, le=250),
):
    if genome_build != "GRCh38/hg38":
        raise HTTPException(422, "현재는 출처 기준이 확인된 GRCh38/hg38 위치만 제공합니다.")
    with database() as db:
        release = _meta(db).get("release_id")
        if _present(db, "workbench_evidence"):
            where = "id IN (SELECT id FROM workbench_evidence INDEXED BY wb_subject WHERE subject_id=? UNION SELECT id FROM workbench_evidence INDEXED BY wb_object WHERE object_id=?) AND layer IN ('regulatory_tested','regulatory_predicted')"
            table = "workbench_evidence INDEXED BY sqlite_autoindex_workbench_evidence_1"
            total = db.execute(
                "SELECT count(*) FROM " + table + " WHERE " + where, (seed_id, seed_id)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM " + table + " WHERE " + where + " ORDER BY id LIMIT ?",
                (seed_id, seed_id, max_records),
            ).fetchall()
            data = {"release_id": release, "total": total, "items": [record(r, db) for r in rows]}
        else:
            data = {"release_id": release, "total": 0, "items": []}
    items, unverified = [], 0
    for item in data["items"]:
        context = item["context"]
        interval = context.get("source_interval", "")
        match = re.fullmatch(r"(chr[0-9XYM]+)_(\d+)_(\d+)", str(interval))
        if context.get("genome_build") != genome_build or not match:
            unverified += 1
            continue
        start, end = int(match[2]), int(match[3])
        if end < start:
            unverified += 1
            continue
        items.append(
            {
                "evidence_id": item["id"],
                "subject_id": item["subject_id"],
                "object_id": item["object_id"],
                "chromosome": match[1],
                "start": start,
                "end": end,
                "genome_build": genome_build,
                "coordinate_convention": context.get(
                    "coordinate_convention", "source_native; indexing not independently verified"
                ),
                "layer": item["layer"],
                "tissue": item["tissue"],
                "source": item["source"],
            }
        )
    return {
        "release_id": data["release_id"],
        "items": items,
        "genome_build": genome_build,
        "unverified_or_unlocated_in_page": unverified,
        "total_evidence": data["total"],
        "truncated": data["total"] > max_records,
        "note": "원본 좌표를 표시합니다. 가까운 위치나 중첩으로 발현 조절을 추론하지 않습니다.",
    }


def resolve_feature_type(requested, available):
    """Resolve one declared source-label synonym; never pool feature types."""
    if requested in available:
        return requested
    aliases = {"gene_expression": {"gene", "expression"}, "cpg": {"methylation"}}
    candidates = set(available) & aliases.get(requested, set())
    return next(iter(candidates)) if len(candidates) == 1 else None


@router.get("/omics/results")
def omics_results(
    series: str = Query(..., max_length=80),
    feature_type: str = Query(..., max_length=40),
    contrast: str = Query(..., max_length=240),
    analysis_version: str = Query(..., max_length=120),
    feature_id: str | None = Query(None, max_length=160),
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=200),
    cursor: str | None = Query(None, max_length=3000),
):
    core = configured_path("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", DATA_DIR / "db/nutrigenomics.sqlite3")
    with database() as projection:
        release = _meta(projection).get("release_id")
        # A caller cannot turn an unindexed analysis into a result of this release.
        present = False
        if _present(projection, "science_record"):
            for row in projection.execute(
                "SELECT metadata_json FROM science_record WHERE kind='contrast'"
            ):
                c = _json(row[0])
                if (c.get("series_accession"), c.get("contrast"), c.get("analysis_version")) == (
                    series,
                    contrast,
                    analysis_version,
                ):
                    present = True
                    break
        if not present:
            raise HTTPException(404, "해당 분석 키가 이 release에 없습니다.")
        identity = fingerprint(core)
        cursor_key = hashlib.sha256(canonical([release, identity, series, contrast, analysis_version, feature_type, feature_id]).encode()).hexdigest()
        offset = (page - 1) * page_size
        if cursor:
            try:
                position = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if position["key"] != cursor_key:
                    raise HTTPException(409, "Cursor의 release·원본·조회 조건이 변경되었습니다.")
                offset = position["offset"]
                if type(offset) is not int or not 0 <= offset <= 100000000:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise HTTPException(422, "올바르지 않은 cursor") from exc
        indexed = indexed_source(projection, core) if core.is_file() else False
        selector = selector_key({"series_accession": series, "contrast": contrast, "analysis_version": analysis_version})
        if indexed:
            type_counts = dict(projection.execute("SELECT feature_type,total FROM perf_effect_type WHERE selector=? ORDER BY feature_type", (selector,)))
        else:
            type_counts = None
    if not core.is_file():
        return {"release_id": release, "items": [], "total": None, "status": "source_unavailable"}
    with read_connection(core) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='effect_selector'").fetchone():
            type_counts = dict(db.execute(
                "SELECT feature_type,total FROM effect_selector WHERE series_accession=? AND contrast=? AND analysis_version=? ORDER BY feature_type",
                (series, contrast, analysis_version)))
            if not type_counts:
                return {"release_id": release, "items": [], "total": None,
                        "status": "not_in_public_release",
                        "note": "연구 맥락은 수록되어 있으나 이 대비의 정량 자료는 공개 범위에 없습니다. 확인된 0건 또는 효과 0을 뜻하지 않습니다."}
        available_types = [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT feature_type FROM study_effect WHERE series_accession=? AND contrast=? AND analysis_version=? ORDER BY feature_type",
                (series, contrast, analysis_version),
            )
        ] if type_counts is None else list(type_counts)
        native_type = resolve_feature_type(feature_type, available_types)
        if native_type is None:
            return {
                "release_id": release,
                "items": [],
                "total": None,
                "status": "feature_type_unavailable",
                "available_feature_types": available_types,
                "requested_feature_type": feature_type,
                "note": "요청한 측정 유형의 원본 결과가 없습니다. 부재를 결과 0건이나 효과 0으로 해석하지 않습니다.",
            }
        condition = "series_accession=? AND feature_type=? AND contrast=? AND analysis_version=?"
        args = [series, native_type, contrast, analysis_version]
        if feature_id:
            condition += " AND feature_id=?"
            args.append(feature_id)
        if indexed:
            with database() as projection:
                clause, ref_args = "selector=? AND feature_type=?", [selector, native_type]
                if feature_id:
                    clause += " AND feature_id=?"
                    ref_args.append(feature_id)
                    total = projection.execute("SELECT count(*) FROM perf_effect_ref WHERE " + clause, ref_args).fetchone()[0]
                    refs = list(projection.execute("SELECT effect_id,feature_id FROM perf_effect_ref WHERE " + clause + " ORDER BY ordinal_type LIMIT ? OFFSET ?", [*ref_args, page_size, offset]))
                else:
                    total = type_counts[native_type]
                    refs = list(projection.execute("SELECT effect_id,feature_id FROM perf_effect_ref WHERE " + clause + " AND ordinal_type>? AND ordinal_type<=? ORDER BY ordinal_type", [*ref_args, offset, offset + page_size]))
                ids = [r[0] for r in refs]
                by_id = {r["id"]: dict(r) for r in db.execute("SELECT * FROM study_effect WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)} if ids else {}
                rows = []
                for ref in refs:
                    row = by_id.get(ref[0])
                    if row is None or [row[k] for k in ("series_accession", "feature_type", "contrast", "analysis_version", "feature_id")] != [series, native_type, contrast, analysis_version, ref[1]]:
                        raise QueryUnavailable("analysis_key_mismatch")
                    rows.append(row)
        else:
            total = type_counts[native_type] if type_counts is not None and not feature_id else db.execute("SELECT count(*) FROM study_effect WHERE " + condition, args).fetchone()[0]
            rows = [dict(r) for r in db.execute("SELECT * FROM study_effect WHERE " + condition + " ORDER BY feature_id,id LIMIT ? OFFSET ?", [*args, page_size, offset])]
        for row in rows:
            row["metadata"] = _public(_json(row.pop("metadata_json", None)))
            row["analysis_key"] = [
                series,
                native_type,
                row["feature_id"],
                contrast,
                analysis_version,
            ]
    return {
        "release_id": release,
        "feature_type": native_type,
        "requested_feature_type": feature_type,
        "available_feature_types": available_types,
        "items": _public(rows),
        "total": total,
        "page": page,
        "page_size": page_size,
        "status": "available",
        "next_cursor": base64.urlsafe_b64encode(canonical({"key": cursor_key, "offset": offset + len(rows)}).encode()).decode().rstrip("=") if rows and offset + len(rows) < total else None,
        "note": "원 분석 결과입니다. 서로 다른 통계량·가설군의 결과를 합치지 않습니다.",
    }
