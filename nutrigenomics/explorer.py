"""Read-only, bounded explorer over a separately versioned evidence projection.

Connections describe source records, never personal efficacy. Exports reconstruct
the requested selection server-side and withhold unassessed source payloads.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import sqlite3
from collections import deque
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator

from .explorer_context import configured_path
from .explorer_graph_reads import GraphReads
from .explorer_performance import (
    ExplorerReadMiddleware,
    QueryUnavailable,
    has_index,
    logical_connection_path,
    read_connection,
)
from .explorer_performance import fingerprint as source_fingerprint
from .explorer_scientific import (
    annotate_effect_context,
    edge_scientific,
    feature_match_identity,
    gene_feature_identifiers,
    path_scientific,
    scientific_matches,
)
from .food_names import annotate as annotate_food_name
from .food_names import meta as food_name_meta
from .food_names import verified_matches as food_name_matches
from .food_search_aliases import SHA256 as SEARCH_ALIAS_SHA256
from .food_search_aliases import VERSION as SEARCH_ALIAS_VERSION
from .food_search_aliases import context as search_context
from .food_search_aliases import search_clause
from .release_contract import metadata_with_contract
from .settings import DATA_DIR, PROJECT_ROOT

router = APIRouter(prefix="/api/v1/explorer", tags=["Explorer"])
DOMAINS = [
    {"id": "blood_pressure", "label": "혈압"},
    {"id": "lipids", "label": "지질"},
    {"id": "obesity", "label": "비만"},
]
KINDS = {
    "food": ("food", "diet"),
    # Preserve the legacy mixed tab while allowing exact food/exposure searches.
    "food_record": ("food",),
    "exposure": ("diet",),
    "component": ("nutrient", "chemical", "compound"),
    "gene": ("gene",),
    "health": ("disease", "outcome"),
    "pathway": ("pathway",),
    "cpg": ("cpg", "methylation_probe"),
    "regulatory_region": ("regulatory_region",),
}
TIERS = {
    "observed": (
        "observed",
        "observed_composition",
        "observed_intervention",
        "human_randomized",
        "human_nonrandomized",
        "human_observational",
        "composition",
    ),
    "curated": ("curated", "curated_mechanism"),
    "candidate": ("candidate", "inferred", "predicted", "regulatory_identity"),
    "extracted": ("extracted",),
}
NOTE = "연결은 출처에 기록된 관계 또는 후보 경로입니다. 개인의 건강 효과·질병 위험을 예측하지 않습니다."


def _json(value, default=None):
    if value is None:
        return {} if default is None else default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {} if default is None else default


def _public(value):
    """Operational file locations are not public scientific provenance."""
    if isinstance(value, dict):
        return {
            k: _public(v)
            for k, v in value.items()
            if k
            not in {
                "local_path",
                "path",
                "source_path",
                "source_file",
                "output_dir",
                "input_dir",
                "data_path",
                "raw_json",
            }
        }
    if isinstance(value, list):
        return [_public(v) for v in value]
    if isinstance(value, str) and (
        value.startswith(("/home/", "/mnt/", "/tmp/"))
        or (len(value) > 2 and value[1:3] in (":\\", ":/"))
    ):
        return "[연구 환경 경로 비공개]"
    return value


def _path() -> Path:
    return configured_path("NUTRIGENOMICS_EXPLORER_DATABASE", DATA_DIR / "explorer" / "current" / "explorer.sqlite").expanduser()


@contextmanager
def database():
    path = _path()
    if not path.is_file():
        raise HTTPException(503, "탐색용 release가 아직 활성화되지 않았습니다.")
    try:
        with read_connection(path) as db:
            yield db
    except QueryUnavailable as exc:
        raise HTTPException(503, {"code": exc.status, "message": "조회 실패는 근거 부재를 의미하지 않습니다."}) from exc


def _meta(db) -> dict:
    return {r["key"]: _json(r["value_json"], None) for r in db.execute("SELECT * FROM meta")}


def _node(row) -> dict:
    item = dict(row)
    for source, target, default in [
        ("aliases_json", "aliases", []),
        ("metadata_json", "metadata", {}),
        ("domains_json", "domains", []),
    ]:
        item[target] = _public(_json(item.pop(source, None), default))
    return annotate_food_name(item)


@lru_cache(maxsize=64)
def _extracted_only_ids(path, source_bytes, source_mtime_ns, kinds, wal_identity=None):
    """Eligibility is fixed for an immutable release, independent of the query."""
    with read_connection(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='extracted_only'").fetchone():
            return tuple(
                r[0]
                for r in db.execute(
                    "SELECT n.id FROM extracted_only v JOIN node n ON n.id=v.node_id WHERE n.kind IN ("
                    + ",".join("?" for _ in kinds)
                    + ")",
                    kinds,
                )
            )
        if db.execute("SELECT 1 FROM edge WHERE tier='extracted' LIMIT 1").fetchone() is None:
            return ()
        return tuple(
            row[0]
            for row in db.execute(
                "WITH candidates(id) AS MATERIALIZED (SELECT subject FROM edge WHERE tier='extracted' UNION SELECT object FROM edge WHERE tier='extracted') SELECT n.id FROM candidates c CROSS JOIN node n ON n.id=c.id WHERE n.kind IN ("
                + ",".join("?" for _ in kinds)
                + ") "
                "AND NOT EXISTS(SELECT 1 FROM edge e WHERE (e.subject=n.id OR e.object=n.id) AND e.tier!='extracted')",
                kinds,
            )
        )


def _edge(row) -> dict:
    item = dict(row)
    for source, target, default in [
        ("context_json", "context", {}),
        ("source_ids_json", "source_ids", []),
        ("pmids_json", "pmids", []),
        ("domains_json", "domains", []),
    ]:
        item[target] = _public(_json(item.pop(source, None), default))
    item["evidence_note"] = NOTE
    item["scientific"] = edge_scientific(item)
    return item


def _options(domain: str, tier: str):
    if domain != "all" and domain not in {d["id"] for d in DOMAINS}:
        raise HTTPException(422, "지원하지 않는 건강 범위")
    if tier != "all" and tier not in TIERS:
        raise HTTPException(422, "지원하지 않는 근거 필터")


def _edge_clause(domain, tier, include_extracted=True, prefix="e"):
    conditions, args = [], []
    if domain != "all":
        conditions.append(
            f"EXISTS(SELECT 1 FROM edge_domain ed WHERE ed.edge_id={prefix}.id AND ed.domain=?)"
        )
        args.append(domain)
    if tier != "all":
        values = TIERS[tier]
        conditions.append(f"{prefix}.tier IN ({','.join('?' for _ in values)})")
        args.extend(values)
    if not include_extracted and tier != "extracted":
        conditions.append(f"{prefix}.tier!='extracted'")
    return conditions, args


@router.get("/meta")
def meta():
    with database() as db:
        result = metadata_with_contract(db)
        result.update(
            {
                "domains": DOMAINS,
                "note": NOTE,
                "prediction_available": False,
                "food_search_aliases": {"version": SEARCH_ALIAS_VERSION, "sha256": SEARCH_ALIAS_SHA256, "purpose": "search_only"},
                "food_names_ko": food_name_meta(),
                "available_filters": {
                    "entity_kinds": list(KINDS),
                    "evidence_tiers": ["all", *TIERS],
                },
                "graph_limits": {
                    "default_nodes": 80,
                    "default_edges": 200,
                    "max_nodes": 250,
                    "max_edges": 750,
                },
            }
        )
        result.setdefault("counts", {})
        counts = result["counts"]
        for table, key in [("node", "nodes"), ("edge", "edges"), ("measurement", "measurements")]:
            if key not in counts:
                counts[key] = (
                    counts[table]
                    if table in counts
                    else db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                )
        if "by_kind" not in counts:
            counts["by_kind"] = dict(db.execute("SELECT kind,count(*) FROM node GROUP BY kind").fetchall())
        return _public(result)


@router.get("/entities")
def entities(
    kind: str = "food",
    q: str = Query("", max_length=512),
    domain: str = "all",
    tier: str = "all",
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(50, ge=1, le=200),
    sort: Literal["label", "degree"] = "label",
    order: Literal["asc", "desc"] = "asc",
    include_extracted: bool = False,
):
    _options(domain, tier)
    if kind not in KINDS:
        raise HTTPException(422, "지원하지 않는 항목 종류")
    allowed = KINDS[kind]
    conditions = [f"n.kind IN ({','.join('?' for _ in allowed)})"]
    args = list(allowed)
    if kind == "exposure":
        # A clinical-study context or generic diet is not an administered product.
        conditions.extend([
            "json_extract(n.metadata_json,'$.entity_type')='exposure'",
            "n.id='Exposure:' || json_extract(n.metadata_json,'$.original_exposure.id')",
        ])
    if domain != "all":
        conditions.append(
            "EXISTS(SELECT 1 FROM node_domain nd WHERE nd.node_id=n.id AND nd.domain=?)"
        )
        args.append(domain)
    if tier != "all":
        ec, ea = _edge_clause("all", tier)
        conditions.append(
            "EXISTS(SELECT 1 FROM edge e WHERE (e.subject=n.id OR e.object=n.id) AND "
            + " AND ".join(ec)
            + ")"
        )
        args.extend(ea)
    with database() as db:
        matches = food_name_matches(db, q) if "food" in allowed and q.strip() else ()
        if q.strip():
            clause, values = search_clause(q, indexed=has_index(db, "perf_search"), korean_foods="food" in allowed)
            if matches:
                clause = "(" + clause + " OR n.id IN (" + ','.join('?' for _ in matches) + "))"
                values.extend(identifier for identifier, _ in matches)
            conditions.append(clause)
            args.extend(values)
        if not include_extracted and tier != "extracted":
            source = Path(logical_connection_path(db)).resolve()
            fingerprint = source.stat()
            excluded = _extracted_only_ids(
                str(source), fingerprint.st_size, fingerprint.st_mtime_ns, allowed, source_fingerprint(source)
            )
            if excluded:
                conditions.append("n.id NOT IN (" + ",".join("?" for _ in excluded) + ")")
                args.extend(excluded)
        where = " AND ".join(conditions)
        total = db.execute("SELECT count(*) FROM node n WHERE " + where, args).fetchone()[0]
        priority, priority_args = "", []
        if matches and sort == 'label':
            branches = []
            for rank in (0, 1):
                identifiers = [identifier for identifier, value in matches if value == rank]
                if identifiers:
                    branches.append("WHEN n.id IN (" + ','.join('?' for _ in identifiers) + ") THEN " + str(rank))
                    priority_args.extend(identifiers)
            if branches:
                priority = "CASE " + ' '.join(branches) + " ELSE 2 END,"
        rows = db.execute(
            f"SELECT n.* FROM node n WHERE {where} ORDER BY {priority}n.{sort} {order.upper()},n.id LIMIT ? OFFSET ?",
            [*args, *priority_args, page_size, (page - 1) * page_size],
        ).fetchall()
        return {
            "items": [_node(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "release_id": _meta(db).get("release_id"),
            **({"search_context": mapping} if "food" in allowed and (mapping := search_context(q)) else {}),
        }


def _source(db, identifier):
    row = db.execute("SELECT metadata_json FROM source WHERE id=?", (identifier,)).fetchone()
    original = _json(row[0]) if row else {}
    permitted = (
        original.get("export_allowed") is True
        or original.get("redistribution_verified") is True
        or original.get("rights_status") == "verified"
    )
    policy = None
    # New affirmative source-specific assessments may resolve an unassessed
    # redistribution flag. An explicit record export prohibition still wins.
    if original.get("export_allowed") is not False:
        for candidate in _export_policies():
            if original.get("source") == candidate.get("source") and identifier.startswith(
                candidate.get("source_id_prefix", "\0")
            ):
                policy = candidate
                permitted = candidate.get("export_allowed") is True
                break
    else:
        permitted = False
    result = {
        "id": identifier,
        "name": original.get("source", original.get("label", identifier)),
        "url": (policy or {}).get("policy_url")
        or original.get(
            "source_url",
            original.get(
                "url",
                "https://ctdbase.org/downloads/"
                if str(original.get("source", "")).startswith("CTD")
                else None,
            ),
        ),
        "version": original.get(
            "version", original.get("release", original.get("report_created", original.get("recorded_at")))
        ),
        "sha256": original.get("sha256"),
        "license": (policy or {}).get("license") or original.get("license", original.get("rights")),
        "export_allowed": permitted,
        "access_status": original.get("access_status", "unknown"),
        "rights_policy_sha256": (policy or {}).get("policy_sha256"),
        "rights_reviewed_at": (policy or {}).get("reviewed_at")
        or original.get("rights_reviewed_at"),
    }
    url = result.get("url")
    if (
        not isinstance(url, str)
        or urlsplit(url).scheme not in ("http", "https")
        or not urlsplit(url).netloc
    ):
        result["url"] = None
    return result


def _export_policies():
    path = configured_path("NUTRIGENOMICS_EXPLORER_RIGHTS", PROJECT_ROOT / "config" / "explorer_export_rights.json")
    if not path.is_file():
        return []
    return _cached_export_policies(str(path.resolve()), source_fingerprint(path))


@lru_cache(maxsize=8)
def _cached_export_policies(path, identity):
    return _json(Path(path).read_text(encoding="utf-8")).get("policies", [])


def _provenance(db, ids):
    return [_source(db, i) for i in dict.fromkeys(ids)]


def _study_url(accession):
    if accession.startswith("GSE") and accession[3:].isdigit():
        return "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=" + accession
    if accession.startswith("E-MTAB-") and accession[7:].isdigit():
        return "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/" + accession
    return None


_EFFECT_PREVIEW_COLUMNS = (
    "series_accession", "feature_type", "feature_id", "feature_label", "contrast", "tissue",
    "timepoint", "intervention", "dose", "duration", "effect_estimate", "standard_error",
    "p_value", "q_value", "effect_unit", "status", "n_people", "n_samples",
    "analysis_version", "metadata_json",
)
_GENE_EFFECT_TYPES = {"gene_expression", "expression", "gene", "transcript"}


def _effect_preview_value(node, row):
    """Keep scalar and export-batch serialization byte-for-byte equivalent."""
    value = {key: row[key] for key in _EFFECT_PREVIEW_COLUMNS}
    value["metadata"] = _public(_json(value.pop("metadata_json")))
    value["feature_mapping"] = feature_match_identity(node, value)
    if value["metadata"].get("contrast_assay_count") is not None:
        value["contrast_assay_count"] = value["metadata"]["contrast_assay_count"]
    value["export_allowed"] = value["metadata"].get("export_allowed") is True
    value["original_url"] = _study_url(value["series_accession"])
    value["interpretation"] = "연구의 관찰 결과; 식품의 독립 효과나 개인 건강 효과가 아님"
    return value


def _effects(node, limit=50):
    """Indexed source-feature lookup only. Do not scan/guess CpG-gene links."""
    if node["kind"] != "gene":
        return []
    core = configured_path("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", DATA_DIR / "db" / "nutrigenomics.sqlite3")
    if not core.is_file():
        raise QueryUnavailable("source_unavailable")
    identifiers = gene_feature_identifiers(node)
    try:
        with read_connection(core) as db:
            # On the research table SQLite can prefer the broad feature_type
            # index (millions of rows) over the selective feature_id lookup.
            # Keep the immutable source unchanged; only pin a verified index.
            # The public release exposes a view and retains its own planner.
            feature_index = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_study_effect_feature_id' AND tbl_name='study_effect'"
            ).fetchone()
            columns = db.execute("PRAGMA index_info(ix_study_effect_feature_id)").fetchall() if feature_index else []
            source = "study_effect INDEXED BY ix_study_effect_feature_id" if columns and columns[0]["name"] == "feature_id" else "study_effect"
            rows = db.execute(
                "SELECT series_accession,feature_type,feature_id,feature_label,contrast,tissue,timepoint,intervention,dose,duration,effect_estimate,standard_error,p_value,q_value,effect_unit,status,n_people,n_samples,analysis_version,metadata_json FROM " + source + " WHERE feature_type IN ('gene_expression','expression','gene','transcript') AND feature_id IN ("
                + ",".join("?" for _ in identifiers)
                + ") ORDER BY series_accession,analysis_version,contrast LIMIT ?",
                [*identifiers, limit],
            ).fetchall()
            return [_effect_preview_value(node, row) for row in rows]
    except sqlite3.OperationalError as exc:
        raise QueryUnavailable("source_unavailable") from exc


def _batch_export_effects(nodes, limit=50):
    """Export-only native-table reads; ordinary node details keep scalar reads.

    First gather covering feature-ID/native-ID references. Hydrate their union
    in native-ID order with one caller-owned source connection, then retain the
    original per-node type filter/order/limit and source metadata/rights. No
    scalar or failed response is cached. Unsupported views/indexes use the
    unchanged scalar route; missing/changed sources and failed reads do not.
    """
    genes = {node["id"]: node for node in nodes if node["kind"] == "gene"}
    if not genes:
        return {}
    core = configured_path("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", DATA_DIR / "db" / "nutrigenomics.sqlite3")
    if not core.is_file():
        raise QueryUnavailable("source_unavailable")
    before = source_fingerprint(core)
    aliases = {identifier: set(gene_feature_identifiers(node)) for identifier, node in genes.items()}
    names = sorted({name for values in aliases.values() for name in values})
    with read_connection(core) as source:
        native = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='study_effect'").fetchone()
        feature_index = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_study_effect_feature_id' AND tbl_name='study_effect'"
        ).fetchone()
        columns = source.execute("PRAGMA index_info(ix_study_effect_feature_id)").fetchall() if feature_index else []
        primary = source.execute("PRAGMA table_info(study_effect)").fetchall() if native else []
        integer_id = any(row["name"] == "id" and row["type"].upper() == "INTEGER" and row["pk"] == 1 for row in primary)
        supported = bool(native and integer_id and columns and columns[0]["name"] == "feature_id")
        if not supported:
            if source_fingerprint(core) != before:
                raise QueryUnavailable("source_changed")
            return None
        references = {}
        for offset in range(0, len(names), 400):
            batch = names[offset:offset + 400]
            for row in source.execute(
                "SELECT id,feature_id FROM study_effect INDEXED BY ix_study_effect_feature_id WHERE feature_id IN ("
                + ",".join("?" for _ in batch) + ") ORDER BY feature_id,id", batch
            ):
                if row["id"] in references and references[row["id"]] != row["feature_id"]:
                    raise QueryUnavailable("analysis_key_mismatch")
                references[row["id"]] = row["feature_id"]
        from .effect_batch import read_rows_by_ids

        rows = read_rows_by_ids(source, references)
        if set(rows) != set(references):
            raise QueryUnavailable("analysis_key_mismatch")
        candidates = []
        for identifier, row in rows.items():
            if row["id"] != identifier or row["feature_id"] != references[identifier]:
                raise QueryUnavailable("analysis_key_mismatch")
            if row["feature_type"] not in _GENE_EFFECT_TYPES:
                continue
            if any(not isinstance(row[key], str) or not row[key] for key in
                   ("series_accession", "feature_type", "feature_id", "contrast", "analysis_version")):
                raise QueryUnavailable("analysis_key_mismatch")
            candidates.append(row)
        # The pinned scalar feature-ID B-tree visits feature_id then native rowid
        # before its stable ORDER BY sorter. Retain that tie order as well as the
        # three explicitly ordered keys; no parallel aliases are collapsed.
        candidates.sort(key=lambda row: (row["series_accession"], row["analysis_version"], row["contrast"], row["feature_id"], row["id"]))
        result = {}
        for identifier, node in genes.items():
            selected = [row for row in candidates if row["feature_id"] in aliases[identifier]][:limit]
            result[identifier] = [_effect_preview_value(node, row) for row in selected]
    if source_fingerprint(core) != before:
        raise QueryUnavailable("source_changed")
    return result


def _methylation_annotations(projection, node, limit=30):
    """Reference preserved probe annotations; never imply gene regulation."""
    if node["kind"] != "gene":
        return [], 0
    if not projection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='omics_reference'"
    ).fetchone():
        return [], 0
    total = projection.execute(
        "SELECT count(*) FROM omics_reference WHERE node_id=?", (node["id"],)
    ).fetchone()[0]
    refs = projection.execute(
        "SELECT * FROM omics_reference WHERE node_id=? ORDER BY series_accession,analysis_version,contrast,feature_id LIMIT ?",
        (node["id"], limit),
    ).fetchall()
    if not refs:
        return [], total
    core = configured_path("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", DATA_DIR / "db" / "nutrigenomics.sqlite3")
    if not core.is_file():
        raise QueryUnavailable("source_unavailable")
    try:
        with read_connection(core) as db:
            rows = db.execute(
                "SELECT * FROM study_effect WHERE id IN (" + ",".join("?" for _ in refs) + ")",
                [r["effect_id"] for r in refs],
            ).fetchall()
            by_id = {row["id"]: dict(row) for row in rows}
            values = []
            for ref in refs:
                row = by_id.get(ref["effect_id"])
                if row is None or row["feature_type"] not in ("cpg", "dmr", "methylation"):
                    continue
                keys = (
                    "series_accession",
                    "feature_type",
                    "feature_id",
                    "contrast",
                    "analysis_version",
                )
                if any(row[k] != ref[k] for k in keys):
                    continue
                context = _json(ref["context_json"])
                expected = context.get("effect_record_sha256")
                encoded = json.dumps(
                    row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
                ).encode()
                if not expected or hashlib.sha256(encoded).hexdigest() != expected:
                    continue
                value = _public(row)
                value.pop("id", None)
                value["metadata"] = _public(_json(value.pop("metadata_json")))
                if value["metadata"].get("contrast_assay_count") is not None:
                    value["contrast_assay_count"] = value["metadata"]["contrast_assay_count"]
                value["export_allowed"] = value["metadata"].get("export_allowed") is True
                value["gene_mapping_relation"] = ref["relation"]
                value["gene_mapping_review_status"] = "candidate_exact_annotation_label"
                value["gene_mapping_context"] = _public(context)
                value["gene_mapping_note"] = (
                    "원본 CpG 플랫폼 주석 이름과의 대응 후보입니다. 유전자 식별자 동일성은 별도 미확인이며 발현 조절·eQTM·인과성 근거가 아닙니다."
                )
                value["original_url"] = _study_url(value["series_accession"])
                values.append(value)
            return values, total
    except sqlite3.OperationalError as exc:
        raise QueryUnavailable("source_unavailable") from exc


def _node_detail(db, identifier, *, preloaded_effects=None):
    row = db.execute("SELECT * FROM node WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise HTTPException(404, "항목을 찾을 수 없습니다.")
    item = _node(row)
    measurements = []
    assessments = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='composition_assessment'").fetchone():
        assessments = {r[0]: _json(r[1]) for r in db.execute(
            "SELECT a.measurement_id,a.assessment_json FROM composition_assessment a "
            "JOIN measurement m ON m.id=a.measurement_id WHERE m.food=?", (identifier,))}
    for r in db.execute(
        "SELECT m.*,n.label AS component_label FROM measurement m JOIN node n ON n.id=m.component WHERE m.food=? ORDER BY n.label,m.id LIMIT 300",
        (identifier,),
    ):
        value = dict(r)
        value.pop("raw_json", None)
        if value["id"] in assessments:
            value["assessment"] = assessments[value["id"]]
        measurements.append(value)
    srcs = [m["source_id"] for m in measurements]
    srcs += item["metadata"].get("source_ids", [])
    if item["metadata"].get("active_source"):
        srcs.append(item["metadata"]["active_source"])
    effects = preloaded_effects[identifier] if preloaded_effects is not None and identifier in preloaded_effects else _effects(item)
    methylation, methylation_count = _methylation_annotations(db, item)
    annotate_effect_context(db, effects)
    annotate_effect_context(db, methylation)
    item.update(
        {
            "measurements": measurements,
            "effects": effects,
            "methylation_annotations": methylation,
            "total_methylation_annotations": methylation_count,
            "studies": item["metadata"].get("studies", []),
            "provenance": _provenance(db, srcs),
            "gaps": [],
            "note": NOTE,
        }
    )
    for key in ("clinical_endpoints", "clinical_endpoint_audit", "clinical_context_only",
                "mediation_established", "expert_review_status", "study_id"):
        if key in item["metadata"]:
            item[key] = item["metadata"][key]
    if item.get("degree", 0) == 0:
        item["gaps"].append("이 단계의 연결 근거 없음")
    if item["kind"] == "gene" and not effects:
        item["gaps"].append(
            "현재 식별자로 직접 조회할 수 있는 사람 오믹스 결과 없음; 유전자 주석으로 결과를 추정하지 않음"
        )
    if item["kind"] == "gene" and not methylation:
        item["gaps"].append("현재 release에서 검증해 조회할 수 있는 DNAm 플랫폼 주석 결과 없음")
    if methylation_count > len(methylation):
        item["gaps"].append(
            f"DNAm 플랫폼 주석 참조 {methylation_count}건 중 원본 결과와 일치하는 최대 {len(methylation)}건을 표시합니다. 유전자 동일성·조절 근거는 별도 미확인입니다."
        )
    from .explorer_workbench import _present, record

    # Use the request's pinned connection, including during a release swap.
    if _present(db, "workbench_evidence"):
        from .evidence_paging import page_rows
        source_path = configured_path('NUTRIGENOMICS_EXPLORER_DATABASE', DATA_DIR / 'explorer/current/explorer.sqlite')
        release_id = _meta(db).get('release_id')
        reference_page = page_rows(db, source_path, release_id, 'all', 1, 100, identifier)
        identity_page = page_rows(db, source_path, release_id, 'molecular_identity', 1, 1000, identifier)
        rows = reference_page[1] if reference_page is not None else db.execute(
            "SELECT * FROM workbench_evidence WHERE subject_id=? OR object_id=? ORDER BY layer,study_id,tissue,id LIMIT 100",
            (identifier, identifier),
        ).fetchall()
        item["regulatory_evidence"] = [record(r, db) for r in rows]
        item["total_regulatory_evidence"] = reference_page[0] if reference_page is not None else db.execute(
            "SELECT count(*) FROM workbench_evidence WHERE subject_id=? OR object_id=?",
            (identifier, identifier),
        ).fetchone()[0]
        item["verified_identifier_links"] = [
            record(r, db)
            for r in (identity_page[1] if identity_page is not None and identity_page[0] <= 1000 else db.execute(
                "SELECT * FROM workbench_evidence WHERE (subject_id=? OR object_id=?) AND relation='verified_gene_identifier_cross_reference'",
                (identifier, identifier),
            ))
            if r['relation'] == 'verified_gene_identifier_cross_reference'
        ]
    else:
        item["regulatory_evidence"] = []
        item["total_regulatory_evidence"] = 0
        item["verified_identifier_links"] = []
    return item


@router.get("/nodes/{identifier:path}")
def node_detail(identifier: str):
    with database() as db:
        result = _node_detail(db, identifier)
        result["release_id"] = _meta(db).get("release_id")
        return result


def _edge_detail(db, identifier):
    row = db.execute("SELECT * FROM edge WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise HTTPException(404, "관계를 찾을 수 없습니다.")
    item = _edge(row)
    _composition_context(db, item)
    item["provenance"] = _provenance(db, item["source_ids"])
    item["export_allowed"] = bool(item["provenance"]) and all(
        s["export_allowed"] for s in item["provenance"]
    )
    return item


@router.get("/edges/{identifier:path}")
def edge_detail(identifier: str):
    with database() as db:
        result = _edge_detail(db, identifier)
        result["release_id"] = _meta(db).get("release_id")
        return result


def _composition_context(db, edge):
    identifier = edge["context"].get("measurement_id")
    if identifier:
        row = db.execute(
            "SELECT amount,unit,basis,status FROM measurement WHERE id=?", (identifier,)
        ).fetchone()
        if row:
            edge["context"]["measurement"] = dict(row)


def _usable_path_edge(edge):
    if edge["tier"] == "restricted":
        return False
    measurement = edge["context"].get("measurement")
    if measurement:
        amount = measurement.get("amount")
        return amount is not None and amount > 0
    return True


def _paths(nodes, edges, seed):
    kinds = {n["id"]: n["kind"] for n in nodes}
    labels = {n["id"]: n["label"] for n in nodes}
    adjacency = {}
    for e in edges:
        if _usable_path_edge(e):
            adjacency.setdefault(e["subject"], []).append(e)
    if kinds.get(seed) not in ("food", "diet", "nutrient", "chemical", "compound"):
        return []
    q = deque([(seed, [seed], [])])
    found, expanded = [], 0
    while q and len(found) < 48 and expanded < 3000:
        node, nids, es = q.popleft()
        expanded += 1
        if kinds.get(node) in ("disease", "outcome") and es:
            weak = next(
                (
                    e
                    for e in es
                    if e["tier"] in ("candidate", "inferred", "predicted", "extracted")
                    or "correspondence" in e["relation"]
                    or "cross_reference" in e["relation"]
                ),
                None,
            )
            found.append(
                {
                    "id": "path:" + ":".join(e["id"] for e in es),
                    "node_ids": nids,
                    "edge_ids": [e["id"] for e in es],
                    "label": " → ".join(labels[i] for i in nids),
                    "weakest_link": weak["id"] if weak else None,
                    "interpretation": "여러 근거를 조합한 후보 경로; 전체 경로의 인체 효과는 검증되지 않음",
                    "scientific": path_scientific(nodes, edges, nids, [e["id"] for e in es]),
                }
            )
            continue
        if len(es) >= 5:
            continue
        for e in adjacency.get(node, []):
            if e["object"] not in nids:
                q.append((e["object"], [*nids, e["object"]], [*es, e]))
    # Show a gene-mediated path where available, rather than exhausting the
    # display with parallel short chemical-disease records. This is a display
    # convention, not a clinical evidence ranking.
    found.sort(
        key=lambda p: (not any(kinds[i] == "gene" for i in p["node_ids"]), len(p["edge_ids"]))
    )
    return found[:12]


def _health_anchors(db, seed, depth, filter_sql, filter_args, node_budget, reads=None):
    """Reserve real directed paths before a high-degree frontier fills the view.

    This is bounded display selection, not an evidence or relevance ranking.
    Only existing, filter-matching relationships are traversed.
    """
    if seed["kind"] not in ("food", "diet", "nutrient", "chemical", "compound"):
        return {}, {}, {}
    nodes, edges, levels = {}, {}, {}
    visited, expanded, found = set(), 0, 0

    def walk(current, trail_nodes, trail_edges):
        nonlocal expanded, found
        if found >= 8 or expanded >= 240 or current in visited:
            return
        visited.add(current)
        expanded += 1
        indexed = reads is not None and reads.indexed
        row = None if indexed else db.execute("SELECT * FROM node WHERE id=?", (current,)).fetchone()
        kind = reads.node_kind(current) if indexed else row["kind"] if row is not None else None
        if kind is None:
            return
        if kind in ("disease", "outcome") and trail_edges:
            added = set(trail_nodes) - set(nodes)
            if len(nodes) + len(added) > node_budget:
                return
            if indexed:
                reads.load_nodes(trail_nodes)
            for level, identifier in enumerate(trail_nodes):
                n = reads.node(identifier) if reads else db.execute("SELECT * FROM node WHERE id=?", (identifier,)).fetchone()
                nodes[identifier] = _node(n)
                levels[identifier] = min(level, levels.get(identifier, level))
            selected_edges = reads.load_edges([e["id"] for e in trail_edges]) if indexed else trail_edges
            edges.update({e["id"]: e for e in selected_edges})
            found += 1
            return
        if len(trail_edges) >= depth:
            return
        # A source food can have >60 measurements. Examine them all; lower
        # levels remain bounded and prefer explicit health endpoints.
        limit = 100 if not trail_edges else 24
        gene_first = kind in ("chemical", "compound")
        order = (
            "CASE WHEN relation='chemical_gene_influence' THEN 0 WHEN relation LIKE '%disease%' THEN 1 "
            "WHEN relation IN ('candidate_name_correspondence','source_identifier_cross_reference') THEN 2 ELSE 3 END"
            if gene_first
            else "CASE WHEN relation LIKE '%disease%' OR relation='randomized_difference' THEN 0 "
            "WHEN relation IN ('candidate_name_correspondence','source_identifier_cross_reference') THEN 1 "
            "WHEN relation='annotated_to_pathway' THEN 4 ELSE 2 END"
        )
        rows = reads.outbound(current, "rank_gene" if gene_first else "rank_health", limit, hydrate=False) if indexed else db.execute(
            "SELECT e.* FROM edge e WHERE e.subject=?"
            + filter_sql
            + " ORDER BY "
            + order
            + ",id LIMIT ?",
            [current, *filter_args, limit],
        )
        for e_row in rows:
            e = e_row if reads and reads.indexed else _edge(e_row)
            if not (reads and reads.indexed):
                _composition_context(db, e)
            usable = bool(e["usable"]) if indexed else _usable_path_edge(e)
            if usable and e["object"] not in trail_nodes:
                walk(e["object"], [*trail_nodes, e["object"]], [*trail_edges, e])
            if found >= 8 or expanded >= 240:
                break

    walk(seed["id"], [seed["id"]], [])
    return nodes, edges, levels


@router.get("/graph")
def graph(
    seed_id: str = Query(..., min_length=1, max_length=300),
    domain: str = "all",
    tier: str = "all",
    depth: int = Query(4, ge=1, le=5),
    max_nodes: int = Query(80, ge=1, le=250),
    max_edges: int = Query(200, ge=1, le=750),
    include_extracted: bool = False,
):
    _options(domain, tier)
    with database() as db:
        seed = db.execute("SELECT * FROM node WHERE id=?", (seed_id,)).fetchone()
        if seed is None:
            raise HTTPException(404, "항목을 찾을 수 없습니다.")
        seen = {seed_id: _node(seed)}
        chosen, q, omitted_edges, omitted_nodes = {}, deque([(seed_id, 0)]), set(), set()
        ec, ea = _edge_clause(domain, tier, include_extracted)
        filter_sql = " AND " + " AND ".join(ec) if ec else ""
        reads = GraphReads(db, _node, _edge, domain, TIERS.get(tier), include_extracted or tier == "extracted")
        anchors, anchor_edges, anchor_levels = _health_anchors(
            db, seed, depth, filter_sql, ea, min(max_nodes, max_edges + 1, 35), reads
        )
        if len(anchor_edges) <= max_edges:
            seen.update(anchors)
            chosen.update(anchor_edges)
            q.extend(
                (identifier, level)
                for identifier, level in anchor_levels.items()
                if identifier != seed_id
            )
        while q and len(chosen) < max_edges:
            if reads.indexed and q[0][1] < depth:
                reads.prefetch_adjacent([i for i, level in q if level == q[0][1]], q[0][1], max_edges + 1)
            current, level = q.popleft()
            if level >= depth:
                continue
            # Union of indexed adjacency queries avoids a full-edge OR scan.
            # Do not fill a food->component->gene exploration with thousands of
            # other foods, or walk a gene back into unrelated chemical assays.
            inbound = ""
            if level > 0:
                inbound = " AND e.relation NOT IN ('contains','contains_nutrient','contains_analytical_flavonoid','chemical_gene_influence')"
            rows = reads.adjacent(current, level, max_edges + 1, hydrate=False) if reads.indexed else db.execute(
                # Rank the small identifier projection first. UNION on full
                # provenance/context JSON made high-degree gene neighborhoods
                # expensive even though only <=751 records could be returned.
                "WITH adjacent AS (SELECT e.id,e.relation,e.tier FROM edge e WHERE e.subject=?"
                + filter_sql
                + " UNION SELECT e.id,e.relation,e.tier FROM edge e WHERE e.object=?"
                + filter_sql
                + inbound
                + "), ranked AS (SELECT * FROM adjacent ORDER BY CASE WHEN relation LIKE '%disease%' THEN 0 WHEN relation IN ('candidate_name_correspondence','source_identifier_cross_reference') THEN 1 WHEN tier='observed_composition' THEN 3 ELSE 2 END,id LIMIT ?) SELECT e.* FROM ranked r JOIN edge e ON e.id=r.id ORDER BY CASE WHEN e.relation LIKE '%disease%' THEN 0 WHEN e.relation IN ('candidate_name_correspondence','source_identifier_cross_reference') THEN 1 WHEN e.tier='observed_composition' THEN 3 ELSE 2 END,e.id",
                [current, *ea, current, *ea, max_edges + 1],
            ).fetchall()
            if reads.indexed:
                candidates = dict.fromkeys(r['object'] if r['subject'] == current else r['subject'] for r in rows)
                reads.load_nodes([i for i in candidates if i not in seen][:max_nodes - len(seen)])
            for row in rows:
                e = row if reads.indexed else _edge(row)
                if not reads.indexed:
                    _composition_context(db, e)
                if e["id"] in chosen:
                    continue
                neighbor = e["object"] if e["subject"] == current else e["subject"]
                if len(chosen) >= max_edges or (neighbor not in seen and len(seen) >= max_nodes):
                    omitted_edges.add(e["id"])
                    if neighbor not in seen:
                        omitted_nodes.add(neighbor)
                    continue
                if neighbor not in seen:
                    n = reads.node(neighbor)
                    if n is None:
                        continue
                    seen[neighbor] = _node(n)
                    q.append((neighbor, level + 1))
                chosen[e["id"]] = e
        # Traversal needs only endpoint IDs. Hydrate the selected records once;
        # omitted neighbors retain their exact counts without reading large
        # provenance JSON for thousands of records that will not be displayed.
        nodes = list(seen.values())
        edges = reads.load_edges(list(chosen)) if reads.indexed else list(chosen.values())
        paths = _paths(nodes, edges, seed_id)
        gaps = []
        if not edges:
            gaps.append("현재 필터에서 이 단계의 연결 근거 없음")
        elif not paths and seen[seed_id]["kind"] in (
            "food",
            "diet",
            "nutrient",
            "chemical",
            "compound",
        ):
            gaps.append(
                "현재 표시 범위에서 건강지표·질환까지의 완전한 경로 없음; 단계 확장으로 추가 근거를 탐색할 수 있습니다."
            )
        if paths:
            gaps.append(
                "연결된 개별 관계가 있어도 전체 경로의 용량·시간·조직 일치나 인체 건강 효과를 입증하지 않습니다."
            )
        omitted = {
            "nodes": len(omitted_nodes),
            "edges": len(omitted_edges),
            "count_type": "encountered_lower_bound",
            "depth_limited": bool(q),
        }
        return {
            "nodes": nodes,
            "edges": edges,
            "paths": paths,
            "gaps": gaps,
            "omitted": omitted,
            "truncated": bool(omitted_edges or omitted_nodes),
            "release_id": _meta(db).get("release_id"),
            "note": NOTE,
        }


class ReferenceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tissue: str | None = Field(default=None, max_length=160)
    response_time_h: FiniteFloat | None = Field(default=None, ge=0, le=100000)
    regimen_duration_days: FiniteFloat | None = Field(default=None, ge=0, le=100000)
    frequency: Literal["single", "daily"] | None = None
    population: str | None = Field(default=None, max_length=400)

    @field_validator("response_time_h", "regimen_duration_days", mode="before")
    @classmethod
    def finite_context(cls, value):
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise HTTPException(422, "시간·기간은 유한한 숫자여야 합니다.")
        return value


class MatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed_id: str = Field(min_length=1, max_length=250)
    intake_g: FiniteFloat | None = Field(default=None, gt=0, le=20000)
    reference_context: ReferenceContext = Field(default_factory=ReferenceContext)
    gene_node_id: str | None = Field(default=None, max_length=250)

    @field_validator("intake_g", mode="before")
    @classmethod
    def finite_intake(cls, value):
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise HTTPException(422, "섭취량은 유한한 숫자여야 합니다.")
        return value


def _match(db, payload):
    row = db.execute("SELECT * FROM node WHERE id=?", (payload.seed_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "선택 항목을 찾을 수 없습니다.")
    seed, gene = _node(row), None
    if payload.gene_node_id:
        row = db.execute(
            "SELECT * FROM node WHERE id=? AND kind='gene'", (payload.gene_node_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(422, "선택한 유전자 항목을 찾을 수 없습니다.")
        gene = _node(row)
    reference = payload.reference_context.model_dump(exclude_none=True)
    if seed["kind"] == "food":
        reference["intake_g"] = payload.intake_g if payload.intake_g is not None else 100
    core = configured_path(
        "NUTRIGENOMICS_EXPLORER_CORE_DATABASE", DATA_DIR / "db" / "nutrigenomics.sqlite3"
    )
    result = scientific_matches(db, seed, core, reference, gene)
    result["release_id"] = _meta(db).get("release_id")
    return _public(result)


@router.post("/match")
def match_evidence(payload: MatchInput):
    with database() as db:
        return _match(db, payload)


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: FiniteFloat = Field(ge=-1e6, le=1e6)
    y: FiniteFloat = Field(ge=-1e6, le=1e6)

    @field_validator("x", "y", mode="before")
    @classmethod
    def finite_coordinate(cls, value):
        if isinstance(value, (int, float)) and not math.isfinite(value):
            # Do not echo a NaN/Infinity into FastAPI's JSON validation response.
            raise HTTPException(422, "좌표는 유한한 숫자여야 합니다.")
        return value


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["html", "json", "csv"]
    release_id: str = Field(min_length=1, max_length=160)
    node_ids: list[str] = Field(min_length=1, max_length=250)
    edge_ids: list[str] = Field(default_factory=list, max_length=750)
    positions: dict[str, Point] = Field(default_factory=dict, max_length=250)
    filters: dict = Field(default_factory=dict)

    @field_validator("filters", mode="before")
    @classmethod
    def finite_filters(cls, value):
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise HTTPException(422, "필터는 유한한 값의 JSON이어야 합니다.") from None
        if len(encoded) > 16000:
            raise HTTPException(422, "필터 크기 초과")
        return value


def _export_exposure_metadata(db, node, metadata):
    """Preserve only rights-cleared, exact-source exposure fields in snapshots."""
    original = metadata.get("original_exposure")
    if original is None:
        return {}

    def withheld(reason):
        return {"exposure_export_status": "withheld", "exposure_withheld_reason": reason}

    if (not isinstance(original, dict) or node.get("kind") not in {"diet", "exposure"}
            or not isinstance(original.get("id"), str)
            or node.get("id") != "Exposure:" + original["id"]
            or metadata.get("entity_type") != "exposure"):
        return withheld("canonical_exposure_identifier_mismatch")
    source_id = original.get("source_id")
    if not isinstance(source_id, str) or not source_id or db is None:
        return withheld("exposure_source_permission_unavailable")
    if _source(db, source_id).get("export_allowed") is not True:
        return withheld("exposure_source_redistribution_not_permitted")
    safe = {}
    for key in ("id", "study_id", "label", "kind", "unit", "dose_basis", "formulation",
                "source_id", "publication_review_status", "expert_review_status"):
        value = original.get(key)
        if isinstance(value, str):
            safe[key] = _public(value)
    for key in ("amount", "duration_days"):
        value = original.get(key)
        if value is None and key in original:
            safe[key] = None
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            safe[key] = value
    for key in ("record_sha256", "original_record_sha256", "canonical_record_sha256",
                "source_record_sha256"):
        value = original.get(key)
        if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
            safe[key] = value
    original_metadata = original.get("metadata")
    if not isinstance(original_metadata, dict):
        original_metadata = _json(original.get("metadata_json"), {})
    if isinstance(original_metadata, dict):
        safe["metadata"] = {
            key: _public(original_metadata[key]) for key in ("dose_frequency", "expert_review_status")
            if isinstance(original_metadata.get(key), str)
        }
    omitted = []
    if "food_terms" in original:
        terms = _json(original["food_terms"], None) if isinstance(original["food_terms"], str) else original["food_terms"]
        if (isinstance(terms, list) and len(terms) <= 50
                and all(isinstance(term, str) and len(term) <= 250 for term in terms)):
            safe["food_terms"] = [_public(term) for term in terms]
        else:
            omitted.append("food_terms")
    result = {"original_exposure": safe, "exposure_export_status": "partial" if omitted else "permitted"}
    if omitted:
        result.update(exposure_withheld_fields=omitted,
                      exposure_withheld_reason="invalid_or_oversized_source_alias_list")
    return result


def _clean_node(node, db=None):
    """Snapshot metadata is an allowlist, never embedded local paths/raw rows."""
    item = dict(node)
    metadata = item.get("metadata", {})
    item["metadata"] = {
        k: metadata[k]
        for k in (
            "preparation",
            "preparation_state",
            "core_food_id",
            "core_active_release",
            "active_source",
            "food_code",
            "source_name",
            "entity_type",
            "component_key",
            "canonical_unit",
            "mixture_or_nutrient_group",
            "single_molecule_identity_verified",
            "food_basis",
            "original_id",
            "clinical_endpoints",
            "clinical_endpoint_audit",
            "clinical_context_only",
            "mediation_established",
            "expert_review_status",
            "study_id",
        )
        if k in metadata
    }
    item["metadata"].update(_export_exposure_metadata(db, node, metadata))
    return item


def _safe_export_edge(edge):
    if edge["export_allowed"]:
        return edge
    return {
        **{
            k: edge[k]
            for k in ("id", "subject", "object", "source_ids", "pmids", "domains", "provenance")
        },
        "relation": "restricted_source_reference",
        "tier": "restricted",
        "review_status": "withheld",
        "taxon": None,
        "context": {"note": "출처 이용조건 확인 전 상세 내보내기 제한"},
        "export_allowed": False,
    }


def _export_scientific(db, result, seed_id):
    """Curator context is exportable; unassessed raw effects stay source links."""
    permitted = db.execute("SELECT source_id FROM measurement WHERE food=?", (seed_id,)).fetchall()
    composition_allowed = bool(permitted) and all(
        _source(db, r[0])["export_allowed"] for r in permitted
    )
    filtered = []
    for item in result["observed_evidence"]:
        if not item["export_allowed"]:
            continue
        if (
            item["mapping_identity"]["method"] == "shared_measured_component"
            and not composition_allowed
        ):
            result["evidence_gaps"].append(
                "이용조건 미확인 식품 조성에 의존한 공유 성분 연결을 제외했습니다."
            )
            continue
        for contrast in item["contrasts"]:
            original = contrast["effects"]
            contrast["effects"] = [e for e in original if e["export_allowed"]]
            contrast["withheld_effect_previews"] = len(original) - len(contrast["effects"])
        clinical = item.get("clinical_results", [])
        item["clinical_results"] = [r for r in clinical if r.get("export_allowed") is True]
        item["withheld_clinical_results"] = len(clinical) - len(item["clinical_results"])
        filtered.append(item)
    result["observed_evidence"] = filtered
    result["evidence_gaps"] = list(dict.fromkeys(result["evidence_gaps"]))
    result["offline_context_frozen"] = True
    return result


@router.post("/export")
def export_selection(payload: ExportInput):
    if len(json.dumps(payload.filters, ensure_ascii=False)) > 16000:
        raise HTTPException(422, "필터 크기 초과")
    with database() as db:
        metadata = _meta(db)
        release = metadata.get("release_id")
        if release != payload.release_id:
            raise HTTPException(409, "release가 변경되었습니다. 화면을 새로고침한 뒤 내보내세요.")
        nodes, details, edges, edetails = [], {}, [], {}
        ids = set(payload.node_ids)
        selected = list(dict.fromkeys(payload.node_ids))
        selected_rows = {row["id"]: row for row in db.execute(
            "SELECT * FROM node WHERE id IN (" + ",".join("?" for _ in selected) + ")", selected
        )} if selected else {}
        for identifier in selected:
            if identifier not in selected_rows:
                raise HTTPException(404, "항목을 찾을 수 없습니다.")
        preloaded_effects = _batch_export_effects([_node(selected_rows[identifier]) for identifier in selected])
        for identifier in dict.fromkeys(payload.node_ids):
            full = _node_detail(db, identifier, preloaded_effects=preloaded_effects)
            measurements = [
                m for m in full["measurements"] if _source(db, m["source_id"])["export_allowed"]
            ]
            withheld = len(full["measurements"]) - len(measurements)
            detail = _clean_node(full, db)
            detail["measurements"] = measurements
            detail["effects"] = [e for e in full["effects"] if e["export_allowed"]]
            detail["withheld_effect_previews"] = len(full["effects"]) - len(detail["effects"])
            detail["methylation_annotations"] = [
                e for e in full["methylation_annotations"] if e["export_allowed"]
            ]
            detail["studies"] = []  # study detail stays a source link until its rights are assessed
            regulatory = full.get("regulatory_evidence", [])
            detail["regulatory_evidence"] = [
                e for e in regulatory if e.get("export_allowed") is True
            ]
            detail["total_regulatory_evidence"] = full.get("total_regulatory_evidence", 0)
            detail["verified_identifier_links"] = [
                e
                for e in full.get("verified_identifier_links", [])
                if e.get("export_allowed") is True
            ]
            detail["gaps"] = [
                *full["gaps"],
                *(
                    [f"이용조건 미확인 성분 기록 {withheld}건의 상세 값을 제외했습니다."]
                    if withheld
                    else []
                ),
            ]
            if detail["withheld_effect_previews"]:
                detail["gaps"].append(
                    f"발현 결과 미리보기 {detail['withheld_effect_previews']}건은 내보내기 권한이 확인되지 않아 제외했습니다. 원본 서비스의 결과 부재를 뜻하지 않습니다."
                )
            if len(regulatory) > len(detail["regulatory_evidence"]):
                detail["gaps"].append(
                    "재배포 이용조건이 확인되지 않은 조절 근거의 상세 값은 제외했습니다. 원본 서비스에서 확인하십시오."
                )
            methylation_withheld = len(full["methylation_annotations"]) - len(
                detail["methylation_annotations"]
            )
            if methylation_withheld:
                detail["gaps"].append(
                    f"이용조건 미확인 DNAm 결과 {methylation_withheld}건의 상세 값을 제외했습니다."
                )
            details[identifier] = detail
            nodes.append(
                _clean_node(
                    {
                        k: full[k]
                        for k in (
                            "id",
                            "kind",
                            "label",
                            "label_en",
                            "aliases",
                            "metadata",
                            "domains",
                            "degree",
                        )
                    }, db
                )
            )
        for identifier in dict.fromkeys(payload.edge_ids):
            edge = _edge_detail(db, identifier)
            if edge["subject"] not in ids or edge["object"] not in ids:
                raise HTTPException(422, "관계의 양 끝 항목을 함께 선택해야 합니다.")
            safe = _safe_export_edge(edge)
            edges.append(safe)
            edetails[identifier] = safe
        context = payload.filters.get("scientific_context")
        if context:
            try:
                matching = MatchInput.model_validate(context)
            except ValueError:
                raise HTTPException(422, "과학적 매칭 내보내기 조건이 올바르지 않습니다.") from None
            if matching.seed_id not in details:
                raise HTTPException(422, "매칭의 시작 항목을 함께 내보내세요.")
            details[matching.seed_id]["scientific_matches"] = _export_scientific(
                db, _match(db, matching), matching.seed_id
            )
        snapshot = {
            "nodes": nodes,
            "edges": edges,
            "node_details": details,
            "edge_details": edetails,
            "paths": _paths(nodes, [e for e in edges if e["tier"] != "restricted"], nodes[0]["id"]),
            "gaps": [
                NOTE,
                "내장된 선택 자료 안에서만 검색합니다. 이용조건 미확인 원본 상세는 제외됩니다.",
            ],
            "release_id": release,
            "created_at": metadata.get("created_at"),
            "filters": payload.filters,
            "positions": {k: v.model_dump() for k, v in payload.positions.items() if k in ids},
            "offline": True,
            "export_withheld_edges": sum(e["tier"] == "restricted" for e in edges),
        }
    headers = {
        "Content-Disposition": f'attachment; filename="nutriomics-explorer.{payload.format}"',
        "X-NutriOmics-Release": str(release),
        "Cache-Control": "no-store",
    }
    if payload.format == "json":
        return Response(
            json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
            media_type="application/json",
            headers=headers,
        )
    if payload.format == "csv":
        stream = io.StringIO()
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "record_type",
                "id",
                "kind",
                "label",
                "subject",
                "object",
                "relation",
                "tier",
                "review_status",
                "source_ids",
                "pmids",
                "component",
                "amount",
                "unit",
                "basis",
                "status",
                "source_id",
                "context",
                "provenance",
                "series_accession",
                "feature_type",
                "feature_id",
                "contrast",
                "analysis_version",
                "effect_estimate",
                "standard_error",
                "p_value",
                "q_value",
                "effect_unit",
                "n_people",
                "n_samples",
                "release_id",
            ],
        )
        writer.writeheader()
        for node in nodes:
            writer.writerow(
                {
                    "record_type": "node",
                    "id": node["id"],
                    "kind": node["kind"],
                    "label": _csv_text(node["label"]),
                    "context": json.dumps(node["metadata"], ensure_ascii=False),
                    "release_id": release,
                }
            )
        for edge in edges:
            writer.writerow(
                {
                    "record_type": "edge",
                    **{
                        k: _csv_text(edge[k])
                        for k in ("id", "subject", "object", "relation", "tier", "review_status")
                    },
                    "source_ids": json.dumps(edge["source_ids"]),
                    "pmids": json.dumps(edge["pmids"]),
                    "context": json.dumps(edge["context"], ensure_ascii=False),
                    "provenance": json.dumps(edge["provenance"], ensure_ascii=False),
                    "release_id": release,
                }
            )
        for detail in details.values():
            scientific = detail.get("scientific_matches")
            if scientific:
                for item in scientific["observed_evidence"]:
                    writer.writerow(
                        {
                            "record_type": "scientific_evidence",
                            "id": _csv_text(item["id"]),
                            "subject": detail["id"],
                            "context": json.dumps(item, ensure_ascii=False),
                            "release_id": release,
                        }
                    )
            for m in detail["measurements"]:
                writer.writerow(
                    {
                        "record_type": "measurement",
                        "id": m["id"],
                        "subject": m["food"],
                        **{
                            k: _csv_text(m[k])
                            for k in ("component", "amount", "unit", "basis", "status", "source_id")
                        },
                        "release_id": release,
                    }
                )
            for effect_kind, key in (
                ("expression_effect", "effects"),
                ("probe_annotation_effect", "methylation_annotations"),
            ):
                for effect in detail.get(key, []):
                    columns = (
                        "series_accession",
                        "feature_type",
                        "feature_id",
                        "contrast",
                        "analysis_version",
                        "effect_estimate",
                        "standard_error",
                        "p_value",
                        "q_value",
                        "effect_unit",
                        "n_people",
                        "n_samples",
                    )
                    writer.writerow(
                        {
                            "record_type": effect_kind,
                            "subject": detail["id"],
                            **{k: _csv_text(effect.get(k)) for k in columns},
                            "context": json.dumps(effect, ensure_ascii=False),
                            "release_id": release,
                        }
                    )
        return Response("\ufeff" + stream.getvalue(), media_type="text/csv", headers=headers)
    assets = _dist()
    runtime = assets / "offline-runtime.js"
    if not runtime.is_file():
        raise HTTPException(503, "독립 HTML runtime이 아직 빌드되지 않았습니다.")
    css = (
        (assets / "offline-runtime.css").read_text(encoding="utf-8")
        if (assets / "offline-runtime.css").is_file()
        else ""
    )
    encoded = (
        json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    js = runtime.read_text(encoding="utf-8").replace("</script", "<\\/script")
    html = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data: blob:; font-src data:; connect-src \'none\'; base-uri \'none\'; form-action \'none\'; object-src \'none\'"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NutriOmics Explorer · Snapshot</title><style>'
        + css
        + '</style></head><body><div id="root"></div><script>window.__NUTRIOMICS_SNAPSHOT__='
        + encoded
        + ";</script><script>"
        + js
        + "</script></body></html>"
    )
    return Response(html, media_type="text/html", headers=headers)


def _csv_text(value):
    return (
        "'" + value
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
        else value
    )


def _dist():
    return Path(
        os.environ.get(
            "NUTRIGENOMICS_EXPLORER_DIST", PROJECT_ROOT / "frontend" / "explorer" / "dist"
        )
    )


def attach_explorer(app: FastAPI, *, public=False):
    app.add_middleware(ExplorerReadMiddleware, include_public=public)
    from .source_replicas import ReplicaGuardMiddleware
    app.add_middleware(ReplicaGuardMiddleware)
    app.include_router(router)
    from .explorer_workbench import router as workbench_router

    app.include_router(workbench_router)
    from .clinical_api import router as clinical_router

    app.include_router(clinical_router)

    @app.get("/explorer", include_in_schema=False)
    def explorer_redirect():
        return RedirectResponse("/explorer/")

    @app.get("/explorer/{asset:path}", include_in_schema=False)
    def explorer_asset(asset: str):
        root = _dist().resolve()
        path = (root / (asset or "index.html")).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "Explorer asset를 찾을 수 없습니다.")
        return FileResponse(path)
