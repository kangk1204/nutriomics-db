"""Loopback API. Automatic candidates and reviewed evidence remain distinct."""

from contextlib import asynccontextmanager
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from . import auth
from .db import connect, digest, dumps, initialize, now, uid
from .registry_v7 import (
    ENGINE_CHOICES,
    default_engine,
    engine_manifest_validation,
    engine_rules_version,
    validate_engine,
)
from .review_eligibility import (
    EVENT_CORRECTION_FIELDS,
    event_correction_reasons,
    event_review_reasons,
    has_explicit_relation,
)
from .settings import Settings


def _json(value, default=None):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _links(row):
    result = {"pubmed": f"https://pubmed.ncbi.nlm.nih.gov/{quote(str(row['pmid']), safe='')}/"}
    if row.get("doi"):
        result["doi"] = "https://doi.org/" + quote(row["doi"], safe="/")
    if row.get("pmcid"):
        result["europe_pmc"] = "https://europepmc.org/articles/" + quote(row["pmcid"], safe="")
    return result


def _paper(row):
    result = dict(row)
    for field in ("publication_types", "mesh", "authors"):
        if field in result:
            result[field] = _json(result[field], [])
    result["retracted"] = bool(result["retracted"])
    result["official_links"] = _links(result)
    return result


def _source(row, with_text=False):
    result = dict(row)
    result.pop("raw_path", None)
    result["raw_hash"] = result.pop("sha256")
    result["sections"] = _json(result["sections"], [])
    if not with_text:
        result.pop("text_content", None)
    return result


def _extraction(row, settings=None):
    result = dict(row)
    result.update(_extraction_validity(row, settings))
    for field in ("current_source_id", "text_hash", "text_content"):
        result.pop(field, None)
    result["result"] = _json(result.pop("result_json"), {})
    result["active"] = bool(result["active"])
    return result


def _assertion(row):
    result = dict(row)
    if "payload" in result:
        result["payload"] = _json(result["payload"], {})
        if not isinstance(result["payload"], dict):
            result["payload"] = {}
    return result


def _corrected(con, assertion):
    """Use reviewed labels without ever changing the saved original quotation."""
    latest = con.execute(
        "SELECT * FROM reviews WHERE assertion_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (assertion["assertion_id"],),
    ).fetchone()
    if latest:
        assertion["review"] = dict(latest)
        assertion["review"]["corrected_payload"] = _json(latest["corrected_payload"])
        if latest["decision"] == "corrected" and assertion["status"] == "approved":
            correction = _json(latest["corrected_payload"], {})
            assertion["original_relation"] = {
                field: assertion[field] for field in ("subject", "predicate", "object")
            }
            for field in ("subject", "predicate", "object"):
                assertion[field] = correction[field]
            if isinstance(assertion.get("payload", {}).get("event"), dict):
                assertion["effective_event"] = dict(assertion["payload"]["event"])
                for field in EVENT_CORRECTION_FIELDS | {
                    "subject",
                    "object",
                    "benchmark_relation_type",
                    "role_direction",
                }:
                    if field in correction:
                        assertion["effective_event"][field] = correction[field]
                event = assertion["effective_event"]
                if (
                    "direction" not in correction
                    and correction["predicate"] != assertion["original_relation"]["predicate"]
                ):
                    event["direction"] = {
                        "increases": "increase",
                        "decreases": "decrease",
                        "associated_with": "association",
                        "affects": "change",
                        "binds": "change",
                    }[correction["predicate"]]
                candidates = [
                    assertion["payload"]["event"].get("controller", {}),
                    assertion["payload"]["event"].get("theme", {}),
                ]
                candidates.extend(
                    dict(m)
                    for m in con.execute(
                        "SELECT * FROM mentions WHERE extraction_id=? AND start>=? AND end<=?",
                        (assertion["extraction_id"], assertion["start"], assertion["end"]),
                    )
                )
                for role, endpoint in (("controller", "subject"), ("theme", "object")):
                    selected = next(
                        (m for m in candidates if event[endpoint] in (m.get("text"), m.get("normalized_id"))),
                        None,
                    )
                    if selected is None and assertion.get("engine_version") in {"heuristic-v5", "heuristic-v6", "heuristic-v7"}:
                        for mention in candidates:
                            payload = mention.get("payload", {})
                            payload = _json(payload, {}) if isinstance(payload, str) else payload
                            if event[endpoint] in payload.get("taxon_compatible_candidate_ids", []):
                                selected = {
                                    **mention,
                                    "normalized_id": event[endpoint],
                                    "payload": {**payload, "identity_reviewed": True},
                                }
                                break
                    if selected:
                        event[role] = selected
                event["statistical_significance"] = event.get("significance", "unknown")
                event["polarity"] = (
                    "null"
                    if event.get("direction") == "no_effect"
                    else "negated"
                    if event.get("negation")
                    else "positive"
                )
                if assertion.get("engine_version") == "heuristic-v5":
                    from .review_eligibility import corrected_v5_event

                    assertion["effective_event"] = corrected_v5_event(assertion["payload"], correction)
                elif assertion.get("engine_version") in {"heuristic-v6", "heuristic-v7"}:
                    from .review_v6 import corrected_event
                    assertion["effective_event"] = corrected_event(assertion["payload"], correction)
            if assertion.get("engine_version") in {"heuristic-v6", "heuristic-v7"} and assertion["payload"].get("observation"):
                from .review_v6 import corrected_observation
                assertion["effective_observation"] = corrected_observation(assertion["payload"], correction)
    return assertion


def _read_context(con, settings):
    """Take one consistent profile/model snapshot for a saved-result request."""
    if not con.in_transaction:
        con.execute("BEGIN")
    profiles = {
        row["engine_version"]: row["dictionary_version"]
        for row in con.execute("SELECT engine_version,dictionary_version FROM engine_profiles")
    }
    artifacts = {}
    for engine in ENGINE_CHOICES:
        path = settings.home / "engine-artifacts" / (engine + ".json")
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if engine in {"heuristic-v5", "heuristic-v6", "heuristic-v7"} and record.get("rules_version") != engine_rules_version(engine):
                    record = {}
                artifacts[engine] = (
                    record.get(
                        "artifact_identity" if engine in {"heuristic-v4", "heuristic-v5", "heuristic-v6", "heuristic-v7"} else "model_sha256"
                    )
                    if isinstance(record, dict)
                    else False
                )
            except (OSError, ValueError, TypeError):
                artifacts[engine] = False
        elif engine in {"heuristic-v4", "heuristic-v5", "heuristic-v6", "heuristic-v7"}:
            artifacts[engine] = False
    return {
        "profiles": profiles,
        "artifacts": artifacts,
        "source_digests": {},
        "rules": {engine: engine_rules_version(engine) for engine in ENGINE_CHOICES},
    }


@lru_cache(maxsize=512)
def _immutable_manifest_validation(encoded, engine, rules_version):
    """Cache only immutable small metadata; mutable profile gates stay per request."""
    decoded = _json(encoded, {})
    decoded = decoded if isinstance(decoded, dict) else {}
    return decoded, tuple(engine_manifest_validation(decoded, engine))


def _extraction_validity(row, settings=None, read_context=None):
    cached = read_context is not None and len(row["result_json"]) <= 65536
    if cached:
        decoded, cached_reasons = _immutable_manifest_validation(
            row["result_json"], row["engine_version"], read_context["rules"][row["engine_version"]]
        )
        reasons = list(cached_reasons)
    else:
        decoded = _json(row["result_json"], {})
    if isinstance(decoded, dict):
        decoded = {**decoded, "dictionary_version": row["dictionary_version"]}
    decoded = decoded if isinstance(decoded, dict) else {}
    if not cached:
        reasons = engine_manifest_validation(
            decoded, row["engine_version"], None if read_context is not None else settings
        )
    if read_context is not None:
        expected_dictionary = read_context["profiles"].get(row["engine_version"])
        if expected_dictionary is not None and decoded.get("dictionary_version") != expected_dictionary:
            reasons.append("dictionary_changed")
        if row["engine_version"] in read_context["artifacts"]:
            artifact = read_context["artifacts"][row["engine_version"]]
            manifest = decoded.get("manifest") if isinstance(decoded.get("manifest"), dict) else {}
            parser = manifest.get("parser") if isinstance(manifest.get("parser"), dict) else {}
            if artifact is False:
                reasons.append("invalid_model_artifact_record")
            elif (
                manifest.get("artifact_identity")
                if row["engine_version"] in {"heuristic-v4", "heuristic-v5", "heuristic-v6", "heuristic-v7"}
                else parser.get("model_sha256")
            ) != artifact:
                reasons.append("model_artifact_changed")
    text = row["text_content"]
    if not row["active"]:
        reasons.append("inactive")
    if row["current_source_id"] != row["source_id"]:
        reasons.append("source_changed")
    if read_context is not None:
        actual_digest = read_context["source_digests"].get(row["source_id"])
        if actual_digest is None:
            actual_digest = row["verified_text_digest"] if "verified_text_digest" in row.keys() else None
            actual_digest = actual_digest or digest(text)
            read_context["source_digests"][row["source_id"]] = actual_digest
    else:
        actual_digest = digest(text)
    if row["source_hash"] != row["text_hash"] or row["text_hash"] != actual_digest:
        reasons.append("text_integrity")
    return {"current_valid": not reasons, "stale_reasons": reasons}


def _engine(settings, requested):
    try:
        return validate_engine(requested or default_engine(settings))
    except ValueError as exc:
        raise HTTPException(422, "지원하지 않는 추출 엔진입니다.") from exc


def _review_valid(con, row):
    latest = con.execute(
        "SELECT * FROM reviews WHERE assertion_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (row["assertion_id"],),
    ).fetchone()
    payload = _json(row["payload"], {})
    semantic_payload = payload
    if latest and latest["decision"] == "corrected" and row["engine_version"] in {"heuristic-v6", "heuristic-v7"}:
        from .review_v6 import corrected_event, corrected_observation
        correction=_json(latest["corrected_payload"],{})
        semantic_payload={**payload,"event":corrected_event(payload,correction)}
        if payload.get("observation"):
            semantic_payload["observation"]=corrected_observation(payload,correction)
    if latest and latest["decision"] == "corrected" and row["engine_version"] == "heuristic-v5":
        from .review_eligibility import corrected_v5_event

        semantic_payload = {
            **payload,
            "event": corrected_v5_event(payload, _json(latest["corrected_payload"], {})),
        }
    return bool(
        latest
        and latest["decision"] in ("approved", "corrected")
        and latest["fingerprint"] == row["fingerprint"]
        and has_explicit_relation(payload)
        and not event_review_reasons(semantic_payload, row["text_content"], row["engine_version"])
        and _event_matches_assertion(payload, row)
    )


def _event_matches_assertion(payload, row):
    if row["engine_version"] in {"heuristic-v6", "heuristic-v7"} and payload.get("observation"):
        observation = payload["observation"]
        return row["subject"] in {observation["intervention"].get("normalized_id"), observation["intervention"]["text"]} and row["object"] == observation["outcome"]["text"]
    if row["engine_version"] not in {"heuristic-v2", "heuristic-v4", "heuristic-v5", "heuristic-v6", "heuristic-v7"}:
        return True
    event = payload.get("event", {}) if isinstance(payload, dict) else {}
    return event.get("subject") == row["subject"] and event.get("object") == row["object"]


def _evidence_valid(row, settings=None, read_context=None):
    text = row["text_content"]
    return (
        _extraction_validity(row, settings, read_context)["current_valid"]
        and not row["retracted"]
        and 0 <= row["start"] < row["end"] <= len(text)
        and text[row["start"] : row["end"]] == row["quote"]
    )


_EXTRACTION_QUERY = """SELECT e.*,p.current_source_id,s.text_hash,s.text_content
 FROM extractions e JOIN papers p ON p.paper_id=e.paper_id
 JOIN sources s ON s.source_id=e.source_id"""


_READ_RESULT = "e.metadata_json"

_EXTRACTION_META_QUERY = (
    """SELECT e.extraction_id,e.paper_id,e.source_id,e.source_hash,e.engine_version,
 e.dictionary_version,e.created_at,e.active,p.current_source_id,s.text_hash,sr.text_digest AS verified_text_digest,
 CASE WHEN sr.text_digest IS NULL THEN s.text_content ELSE '' END AS text_content,"""
    + _READ_RESULT
    + """ AS result_json
 FROM extraction_read_index e JOIN papers p ON p.paper_id=e.paper_id JOIN sources s ON s.source_id=e.source_id
 LEFT JOIN source_read_index sr ON sr.source_id=s.source_id"""
)

_EXTRACTION_SUMMARY_QUERY = _EXTRACTION_META_QUERY.replace(
    _READ_RESULT + " AS result_json",
    "e.summary_json AS result_json",
)

_ASSERTION_QUERY = (
    """SELECT a.*,e.active,e.source_id,e.source_hash,e.engine_version,e.dictionary_version,"""
    + _READ_RESULT
    + """ AS result_json,
 s.text_hash,s.sha256 AS raw_hash,s.text_content,s.url AS source_url,s.kind AS source_kind,
 p.current_source_id,p.retracted,p.pmid,p.doi,p.pmcid,p.title
 FROM assertions a JOIN extraction_read_index e ON e.extraction_id=a.extraction_id
 JOIN sources s ON s.source_id=e.source_id JOIN papers p ON p.paper_id=a.paper_id"""
)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assertion_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approved", "rejected", "corrected"]
    comment: str = Field(default="", max_length=10000)
    corrected_payload: dict | None = None


def create_app(settings=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_app):
        initialize(settings)
        yield

    service = FastAPI(title="Nutriomics Literature", version="0.2.0", lifespan=lifespan)
    service.state.settings = settings
    bearer = HTTPBearer(auto_error=False)

    def reviewer(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        username = auth.verify_token(settings, credentials.credentials) if credentials else None
        if username is None:
            raise HTTPException(401, "검수자 로그인이 필요합니다.", headers={"WWW-Authenticate": "Bearer"})
        return username

    @service.get("/healthz")
    def healthz():
        with connect(settings) as con:
            con.execute("SELECT 1").fetchone()
        return {"status": "ok", "engine": default_engine(settings), "engines": list(ENGINE_CHOICES)}

    @service.get("/api/v1/engines")
    def engines():
        return {
            "default": default_engine(settings),
            "items": [
                {"engine": name, "quality_status": "candidate_beta", "approved_automatically": False}
                for name in ENGINE_CHOICES
            ],
            "notice": "공개 정답셋 성능과 영양 문헌 정확도는 별도로 평가합니다.",
        }

    @service.get("/api/v1/soak-status")
    def soak_status(engine: str | None = None):
        selected = _engine(settings, engine)
        if selected == "heuristic-v7":
            from .operations_v7 import operation_status
            return operation_status(settings)
        if selected == "heuristic-v6":
            from .operations_v6 import operation_status
            return operation_status(settings)
        if selected == "heuristic-v5":
            from .operations_v5 import operation_status

            return operation_status(settings)
        if selected == "heuristic-v4":
            from .operations_v4 import operation_status

            return operation_status(settings)
        if selected == "heuristic-v2":
            from .operations_v2 import engine_status

            return engine_status(settings, selected)
        from .monitor import status

        return {**status(settings), "engine": selected}

    @service.get("/api/v1/topics")
    def topics():
        with connect(settings) as con:
            rows = con.execute(
                "SELECT t.*,ts.watermark,ts.updated_at AS last_success_at "
                "FROM topics t LEFT JOIN topic_state ts ON ts.topic_id=t.topic_id "
                "AND ts.query_version=t.query_version ORDER BY t.rowid"
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    @service.get("/api/v1/experiments")
    def experiments(engine: str = "heuristic-v3", profile: str | None = None):
        if engine == "heuristic-v7":
            from .operations_v7 import reports
            return reports(settings.home)
        if engine == "heuristic-v6":
            from .evaluation_v6 import reports, selection
            result = reports(settings, profile)
            result["selection"] = selection(result["items"])
            result["items"] = [{**{k:v for k,v in r.items() if k not in {"per_paper","artifact"}},
                                "artifact_identity":r["artifact"]["artifact_identity"],
                                "rules_version":r["artifact"]["rules_version"]} for r in result["items"]]
            return result
        if engine == "heuristic-v5":
            from .evaluation_v5 import reports, selection

            result = reports(settings, profile)
            result["selection"] = selection(result["items"])
            result["items"] = [
                {
                    **{k: v for k, v in r.items() if k not in {"per_paper", "artifact"}},
                    "artifact_identity": r["artifact"]["artifact_identity"],
                    "rules_version": r["artifact"]["rules_version"],
                }
                for r in result["items"]
            ]
            return result
        if engine == "heuristic-v4":
            from .evaluation_v4 import reports

            return reports(settings, profile)
        if engine != "heuristic-v3":
            raise HTTPException(422, "지원하지 않는 비교 엔진입니다.")
        from .experiments_v3 import experiment_summary

        try:
            return experiment_summary(settings)
        except (ValueError, OSError, KeyError) as error:
            raise HTTPException(409, str(error)) from error

    @service.get("/api/v1/experiments/diagnostics")
    def experiment_diagnostics(engine: str = "heuristic-v4", profile: str | None = None):
        if engine == "heuristic-v7":
            from .operations_v7 import reports
            return reports(settings.home)
        if engine == "heuristic-v6":
            from .evaluation_v6 import reports
            result = reports(settings, profile)
            return {"engine":engine,"items":[{k:r.get(k) for k in ("id","corpus","mode","provider","papers","metrics",
                "failure_counts","gene_identity_reasons","chemical_identity_reasons","candidate_pairs","candidate_coverage",
                "id_search_coverage","false_positive_counts")} for r in result["items"]]}
        if engine == "heuristic-v5":
            from .evaluation_v5 import reports

            result = reports(settings, profile)
            return {
                "engine": engine,
                "items": [
                    {
                        k: r.get(k)
                        for k in (
                            "id",
                            "corpus",
                            "mode",
                            "provider",
                            "ablation",
                            "source_level",
                            "papers",
                            "failure_counts",
                            "direction_failure_counts",
                            "false_positive_counts",
                            "gene_identity_reasons",
                            "candidate_pairs",
                            "candidate_id_unresolved",
                            "type_hypotheses",
                            "semantic_verified",
                            "metrics",
                        )
                    }
                    for r in result["items"]
                ],
            }
        if engine != "heuristic-v4":
            raise HTTPException(422, "v4 진단 엔진을 지정하세요.")
        from .evaluation_v4 import reports

        saved = reports(settings, profile)
        return {
            "engine": engine,
            "items": [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "corpus",
                        "mode",
                        "provider",
                        "profile",
                        "papers",
                        "failure_counts",
                        "gene_identity_reasons",
                        "metrics",
                    )
                }
                for r in saved["items"]
            ],
        }

    @service.get("/api/v1/experiments/nutrition")
    def experiment_nutrition():
        from .experiments_v3 import nutrition_previews

        try:
            return nutrition_previews(settings)
        except (ValueError, OSError, KeyError) as error:
            raise HTTPException(409, str(error)) from error

    @service.get("/api/v1/experiments/nutrition/{case_id}")
    def experiment_nutrition_case(case_id: str):
        from .experiments_v3 import nutrition_previews

        try:
            return nutrition_previews(settings, case_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except (ValueError, OSError, KeyError) as error:
            raise HTTPException(409, str(error)) from error

    @service.get("/api/v1/papers")
    def papers(
        q: str = Query(default="", max_length=512),
        topic: str | None = None,
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        engine: str | None = None,
    ):
        selected_engine = _engine(settings, engine)
        conditions, args = [], []
        tokens = re.findall(r"\w+", q, flags=re.UNICODE)
        if q.strip() and not tokens:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        if tokens:
            conditions.append("p.paper_id IN (SELECT rowid FROM papers_fts WHERE papers_fts MATCH ?)")
            args.append(" AND ".join('"' + token + '"' for token in tokens))
        if topic:
            conditions.append(
                "EXISTS(SELECT 1 FROM paper_topics pt JOIN topics t ON t.topic_id=pt.topic_id "
                "AND t.query_version=pt.query_version WHERE pt.paper_id=p.paper_id "
                "AND pt.topic_id=? AND t.enabled=1)"
            )
            args.append(topic)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with connect(settings) as con:
            read_context = _read_context(con, settings)
            total = con.execute("SELECT COUNT(*) FROM papers p" + where, args).fetchone()[0]
            rows = con.execute(
                "SELECT p.* FROM papers p"
                + where
                + " ORDER BY p.pub_date DESC,p.paper_id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                item = _paper(row)
                results = con.execute(
                    _EXTRACTION_SUMMARY_QUERY
                    + " WHERE e.paper_id=? AND e.engine_version=? ORDER BY e.active DESC,e.created_at DESC LIMIT 10",
                    (row["paper_id"], selected_engine),
                ).fetchall()
                result = next(
                    (r for r in results if _extraction_validity(r, settings, read_context)["current_valid"]),
                    None,
                )
                item["summary_current"] = bool(result)
                item["summary_status"] = "current" if result else ("stale" if results else "pending")
                item["summary_stale_reasons"] = (
                    []
                    if result or not results
                    else _extraction_validity(results[0], settings, read_context)["stale_reasons"]
                )
                item["summary"] = _json(result["result_json"], {}) if result else None
                item["topics"] = [
                    r[0]
                    for r in con.execute(
                        "SELECT DISTINCT pt.topic_id FROM paper_topics pt JOIN topics t "
                        "ON t.topic_id=pt.topic_id AND t.query_version=pt.query_version "
                        "WHERE pt.paper_id=? AND t.enabled=1",
                        (row["paper_id"],),
                    )
                ]
                items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset, "engine": selected_engine}

    @service.get("/api/v1/papers/{paper_id}")
    def paper(paper_id: int, engine: str | None = None):
        selected_engine = _engine(settings, engine)
        with connect(settings) as con:
            row = con.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "문헌을 찾을 수 없습니다.")
            result = _paper(row)
            result["sources"] = [
                _source(s, with_text=True)
                for s in con.execute(
                    "SELECT * FROM sources WHERE paper_id=? ORDER BY retrieved_at DESC", (paper_id,)
                )
            ]
            result["extractions"] = [
                _extraction(e, settings)
                for e in con.execute(
                    _EXTRACTION_QUERY
                    + " WHERE e.paper_id=? AND e.engine_version=? ORDER BY e.created_at DESC",
                    (paper_id, selected_engine),
                )
            ]
            current_ids = [e["extraction_id"] for e in result["extractions"] if e["current_valid"]]
            result["summary_current"] = bool(current_ids)
            result["summary_status"] = (
                "current" if current_ids else ("stale" if result["extractions"] else "pending")
            )
            result["mentions"] = [
                _assertion(m)
                for m in con.execute(
                    "SELECT m.* FROM mentions m JOIN extractions e ON e.extraction_id=m.extraction_id "
                    "WHERE m.paper_id=? AND e.active=1 AND e.engine_version=? ORDER BY m.start",
                    (paper_id, selected_engine),
                )
                if m["extraction_id"] in current_ids
            ]
            result["assertions"] = []
            for assertion in con.execute(
                _ASSERTION_QUERY + " WHERE a.paper_id=? AND e.engine_version=? ORDER BY a.start",
                (paper_id, selected_engine),
            ):
                item = _corrected(con, _assertion(assertion))
                item["current_valid"] = _evidence_valid(assertion, settings)
                review_valid = _review_valid(con, assertion)
                item["review_eligibility_reasons"] = event_review_reasons(
                    item["payload"], assertion["text_content"], selected_engine
                )
                item["display_status"] = (
                    item["status"]
                    if item["current_valid"] and (item["status"] != "approved" or review_valid)
                    else "needs_review"
                )
                item.pop("text_content", None)
                item.pop("result_json", None)
                result["assertions"].append(item)
            result["topics"] = [
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT pt.topic_id FROM paper_topics pt JOIN topics t ON t.topic_id=pt.topic_id "
                    "AND t.query_version=pt.query_version WHERE pt.paper_id=? AND t.enabled=1",
                    (paper_id,),
                )
            ]
        result["engine"] = selected_engine
        return result

    @service.get("/api/v1/evidence-map")
    def evidence_map(
        topic: str | None = None,
        gene: str | None = Query(default=None, max_length=128),
        status: Literal["all", "candidate", "approved", "needs_review", "rejected"] = "all",
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        engine: str | None = None,
    ):
        selected_engine = _engine(settings, engine)
        conditions = [
            "e.engine_version=?",
            "e.active=1",
            "p.retracted=0",
            "p.current_source_id=e.source_id",
            "e.source_hash=s.text_hash",
            "EXISTS(SELECT 1 FROM paper_topics current_pt JOIN topics current_t "
            "ON current_t.topic_id=current_pt.topic_id AND current_t.query_version=current_pt.query_version "
            "WHERE current_pt.paper_id=a.paper_id AND current_t.enabled=1)",
        ]
        args = [selected_engine]
        if topic:
            conditions.append(
                "EXISTS(SELECT 1 FROM paper_topics pt JOIN topics t ON t.topic_id=pt.topic_id "
                "AND t.query_version=pt.query_version WHERE pt.paper_id=a.paper_id "
                "AND pt.topic_id=? AND t.enabled=1)"
            )
            args.append(topic)
        if status != "all":
            conditions.append("a.status=?")
            args.append(status)
        if gene:
            conditions.append(
                "(a.subject=? COLLATE NOCASE OR a.object=? COLLATE NOCASE OR "
                "EXISTS(SELECT 1 FROM mentions m WHERE m.extraction_id=e.extraction_id "
                "AND m.kind='gene' AND (m.text=? COLLATE NOCASE OR m.normalized_id=? COLLATE NOCASE) "
                "AND m.start>=a.start AND m.end<=a.end))"
            )
            args.extend([gene, gene, gene, gene])
        with connect(settings) as con:
            read_context = _read_context(con, settings)
            rows = con.execute(
                _ASSERTION_QUERY
                + " WHERE "
                + " AND ".join(conditions)
                + " ORDER BY a.paper_id DESC,a.start,a.assertion_id",
                args,
            )
            selected, total = [], 0
            for row in rows:
                if not _evidence_valid(row, settings, read_context):
                    continue
                if row["status"] == "approved":
                    if not _review_valid(con, row):
                        continue
                if offset <= total < offset + limit:
                    selected.append(row)
                total += 1
            edges = []
            nodes = {}
            for row in selected:
                edge = _corrected(con, _assertion(row))
                edge.pop("text_content")
                edge.pop("result_json", None)
                edge["official_links"] = _links(edge)
                edge["id"] = edge["assertion_id"]
                edge["is_reviewed"] = edge["status"] == "approved"
                edge["relation_is_causal"] = False  # no graph-path causal inference
                effective_payload={**edge["payload"]}
                if edge.get("effective_event"):
                    effective_payload["event"]=edge["effective_event"]
                if edge.get("effective_observation"):
                    effective_payload["observation"]=edge["effective_observation"]
                edge["role_direction"]=effective_payload.get("event",{}).get("role_direction","unknown")
                edge["review_eligibility_reasons"] = event_review_reasons(
                    effective_payload, row["text_content"], selected_engine
                )
                for name in (edge["subject"], edge["object"]):
                    label, kind = name, "entity"
                    for endpoint in ("subject_mention", "object_mention"):
                        mention = edge["payload"].get(endpoint, {})
                        if name in (mention.get("normalized_id"), mention.get("text")):
                            label = mention.get("payload", {}).get("symbol") or mention.get("text") or name
                            kind = mention.get("kind", "entity")
                    nodes[name] = {"id": name, "label": label, "kind": kind}
                edges.append(edge)
        return {
            "nodes": list(nodes.values()),
            "edges": edges,
            "total": total,
            "limit": limit,
            "offset": offset,
            "engine": selected_engine,
            "notice": "자동 후보와 검수 근거를 구분합니다. 연결 경로는 인과 효과를 뜻하지 않습니다.",
        }

    @service.get("/api/v1/observations")
    def observations(engine: str = "heuristic-v6", paper_id: int | None = Query(default=None, ge=1),
                     topic: str | None = None, status: Literal["all","candidate","needs_review","approved","rejected"] = "all",
                     limit: int = Query(default=25,ge=1,le=100), offset: int = Query(default=0,ge=0)):
        selected = _engine(settings,engine)
        conditions=["e.engine_version=?","e.active=1","p.retracted=0","e.source_id=p.current_source_id"]
        args=[selected]
        if paper_id:
            conditions.append("e.paper_id=?")
            args.append(paper_id)
        if topic:
            conditions.append("EXISTS(SELECT 1 FROM paper_topics pt JOIN topics t ON pt.topic_id=t.topic_id AND pt.query_version=t.query_version WHERE pt.paper_id=e.paper_id AND pt.topic_id=? AND t.enabled=1)")
            args.append(topic)
        else:
            conditions.append("EXISTS(SELECT 1 FROM paper_topics pt JOIN topics t ON pt.topic_id=t.topic_id AND pt.query_version=t.query_version WHERE pt.paper_id=e.paper_id AND t.enabled=1)")
        items,total=[],0
        with connect(settings) as con:
            context=_read_context(con,settings)
            rows=con.execute(_EXTRACTION_QUERY+" WHERE "+" AND ".join(conditions)+" ORDER BY e.paper_id DESC",args)
            for row in rows:
                if not _extraction_validity(row,settings,context)["current_valid"]:
                    continue
                for observation in _json(row["result_json"],{}).get("clinical_observations",[]):
                    item={"paper_id":row["paper_id"],"engine":selected,"extraction_id":row["extraction_id"],
                          "observation":observation,"status":observation["status"],"assertion_id":None}
                    assertion=con.execute(_ASSERTION_QUERY+" WHERE a.extraction_id=? AND json_extract(a.payload,'$.observation.observation_id')=?",
                                          (row["extraction_id"],observation["observation_id"])).fetchone()
                    if assertion:
                        if assertion["status"]=="approved" and not _review_valid(con,assertion):
                            continue
                        projected=_corrected(con,_assertion(assertion))
                        item.update(assertion_id=assertion["assertion_id"],status=projected["status"],review=projected.get("review"))
                        correction=(projected.get("review") or {}).get("corrected_payload") or {}
                        if projected["status"]=="approved" and correction:
                            from .review_v6 import corrected_observation
                            item["observation"]=corrected_observation(projected["payload"],correction)
                    if status != "all" and item["status"] != status:
                        continue
                    if offset <= total < offset+limit:
                        items.append(item)
                    total+=1
        return {"engine":selected,"items":items,"total":total,"limit":limit,"offset":offset,"nutrition_accuracy":"unmeasured"}

    @service.get("/api/v1/events")
    def events(
        engine: str | None = None,
        topic: str | None = None,
        paper_id: int | None = Query(default=None, ge=1),
        status: Literal[
            "all", "candidate", "approved", "needs_review", "rejected", "abstained", "background"
        ] = "all",
        reason: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        selected_engine = _engine(settings, engine)
        conditions = ["e.active=1", "e.engine_version=?", "e.source_id=p.current_source_id", "p.retracted=0"]
        args = [selected_engine]
        if topic:
            conditions.append(
                "EXISTS(SELECT 1 FROM paper_topics pt JOIN topics t ON t.topic_id=pt.topic_id "
                "AND t.query_version=pt.query_version WHERE pt.paper_id=e.paper_id AND pt.topic_id=? AND t.enabled=1)"
            )
            args.append(topic)
        else:
            conditions.append(
                "EXISTS(SELECT 1 FROM paper_topics pt JOIN topics t ON t.topic_id=pt.topic_id "
                "AND t.query_version=pt.query_version WHERE pt.paper_id=e.paper_id AND t.enabled=1)"
            )
        if paper_id:
            conditions.append("e.paper_id=?")
            args.append(paper_id)
        items, total = [], 0

        def include(item):
            nonlocal total
            event = item.get("event", {})
            reasons = event.get("triage_reasons", []) + event.get("abstention_reasons", [])
            if reason and reason not in reasons:
                return
            if offset <= total < offset + limit:
                items.append(item)
            total += 1

        with connect(settings) as con:
            read_context = _read_context(con, settings)
            if status not in ("abstained", "background"):
                current_conditions = [*conditions]
                current_args = [*args]
                if status != "all":
                    current_conditions.append("a.status=?")
                    current_args.append(status)
                for row in con.execute(
                    _ASSERTION_QUERY
                    + " WHERE "
                    + " AND ".join(current_conditions)
                    + " ORDER BY a.paper_id DESC,a.start,a.assertion_id",
                    current_args,
                ):
                    if not _evidence_valid(row, settings, read_context) or (
                        row["status"] == "approved" and not _review_valid(con, row)
                    ):
                        continue
                    assertion = _corrected(con, _assertion(row))
                    event = assertion.get("effective_event", assertion["payload"].get("event", {}))
                    include(
                        {
                            "event_id": row["assertion_id"],
                            "assertion_id": row["assertion_id"],
                            "paper_id": row["paper_id"],
                            "pmid": row["pmid"],
                            "title": row["title"],
                            "engine": selected_engine,
                            "status": row["status"],
                            "event": event,
                            "payload": assertion["payload"],
                            "review": assertion.get("review"),
                            "quote": row["quote"],
                            "start": row["start"],
                            "end": row["end"],
                            "source_hash": row["source_hash"],
                            "fingerprint": row["fingerprint"],
                            "review_eligibility_reasons": event_review_reasons(
                                assertion["payload"], row["text_content"], selected_engine
                            ),
                        }
                    )
            if status in ("all", "abstained", "background"):
                projected = _EXTRACTION_META_QUERY.replace(
                    " FROM extraction_read_index e", ",p.pmid,p.title FROM extraction_read_index e"
                )
                valid_sources = {}
                for row in con.execute(
                    projected + " WHERE " + " AND ".join(conditions) + " ORDER BY e.paper_id DESC", args
                ):
                    stale = _extraction_validity(row, settings, read_context)["stale_reasons"]
                    if set(stale) - {
                        "parser_unavailable",
                        "nonproduction_parser",
                        "parser_version_changed",
                        "model_hash_missing",
                    }:
                        continue
                    valid_sources[row["extraction_id"]] = {
                        "paper_id": row["paper_id"],
                        "pmid": row["pmid"],
                        "title": row["title"],
                        "source_hash": row["source_hash"],
                        "stale": stale,
                    }
                if valid_sources:
                    withheld_conditions = ["w.extraction_id IN (SELECT value FROM json_each(?))"]
                    withheld_args = [dumps(list(valid_sources))]
                    if status != "all":
                        withheld_conditions.append("w.status=?")
                        withheld_args.append(status)
                    if reason:
                        withheld_conditions.append(
                            "EXISTS(SELECT 1 FROM withheld_event_reasons wr WHERE wr.extraction_id=w.extraction_id AND wr.ordinal=w.ordinal AND wr.reason=?)"
                        )
                        withheld_args.append(reason)
                    where = " WHERE " + " AND ".join(withheld_conditions)
                    withheld_total = con.execute(
                        "SELECT COUNT(*) FROM withheld_events w" + where, withheld_args
                    ).fetchone()[0]
                    page_offset, page_limit = max(0, offset - total), max(0, limit - len(items))
                    if page_limit:
                        for row in con.execute(
                            "SELECT w.* FROM withheld_events w JOIN extraction_read_index e ON e.extraction_id=w.extraction_id"
                            + where
                            + " ORDER BY e.paper_id DESC,w.ordinal,w.extraction_id LIMIT ? OFFSET ?",
                            [*withheld_args, page_limit, page_offset],
                        ):
                            source = valid_sources[row["extraction_id"]]
                            event = _json(row["event_json"], {})
                            items.append(
                                {
                                    "event_id": row["event_id"],
                                    "assertion_id": None,
                                    "paper_id": source["paper_id"],
                                    "pmid": source["pmid"],
                                    "title": source["title"],
                                    "engine": selected_engine,
                                    "status": row["status"],
                                    "event": event,
                                    "source_hash": source["source_hash"],
                                    "review_eligibility_reasons": ["event_abstained", *source["stale"]],
                                }
                            )
                    total += withheld_total
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "engine": selected_engine,
            "quality_status": "candidate_beta",
            "notice": "보류 이벤트는 승인 대상이 아닙니다. 영양 문헌 정확도는 별도 검증 전입니다.",
        }

    @service.post("/api/v1/auth/login")
    def login(body: Login):
        if not auth.authenticate(settings, body.username, body.password):
            raise HTTPException(401, "계정 또는 비밀번호가 올바르지 않습니다.")
        return auth.create_session(settings, body.username)

    @service.post("/api/v1/auth/logout")
    def logout(_username=Depends(reviewer), credentials=Depends(bearer)):
        auth.revoke_token(settings, credentials.credentials)
        return {"status": "logged_out"}

    @service.post("/api/v1/reviews")
    def review(body: Review, username=Depends(reviewer)):
        correction = None
        if body.decision == "corrected":
            supplied = body.corrected_payload or {}
            if not {"subject", "predicate", "object"}.issubset(supplied) or not body.comment.strip():
                raise HTTPException(
                    422, "수정은 subject, predicate, object와 수정 사유를 모두 입력해야 합니다."
                )
            if any(
                not isinstance(supplied[f], str) or not supplied[f].strip() or len(supplied[f]) > 512
                for f in ("subject", "predicate", "object")
            ):
                raise HTTPException(422, "수정 관계의 각 항목은 1~512자 문자열이어야 합니다.")
            correction = {**supplied, **{f: supplied[f].strip() for f in ("subject", "predicate", "object")}}
        elif body.corrected_payload is not None:
            raise HTTPException(422, "수정 내용은 corrected 결정에서만 사용하세요.")
        with connect(settings, durable=True) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(_ASSERTION_QUERY + " WHERE a.assertion_id=?", (body.assertion_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "근거 후보를 찾을 수 없습니다.")
            if not _evidence_valid(row, settings):
                raise HTTPException(
                    409, "원문·추출 버전이 변경되었거나 철회된 문헌입니다. 최신 후보를 확인하세요."
                )
            payload = _json(row["payload"], {})
            if body.decision != "rejected" and not has_explicit_relation(payload):
                raise HTTPException(409, "명시적 관계 규칙을 통과한 원문 근거만 승인할 수 있습니다.")
            eligibility_payload = payload
            if correction and row["engine_version"] in {"heuristic-v6", "heuristic-v7"}:
                from .review_v6 import corrected_event, corrected_observation
                eligibility_payload = {**payload,"event":corrected_event(payload,correction)}
                if payload.get("observation"):
                    eligibility_payload["observation"]=corrected_observation(payload,correction)
            if correction and row["engine_version"] == "heuristic-v5":
                from .review_eligibility import corrected_v5_event

                eligibility_payload = {**payload, "event": corrected_v5_event(payload, correction)}
            eligibility = event_review_reasons(
                eligibility_payload, row["text_content"], row["engine_version"]
            )
            if not _event_matches_assertion(payload, row):
                eligibility.append("event_assertion_roles_conflict")
            if body.decision != "rejected" and eligibility:
                raise HTTPException(
                    409, {"message": "이벤트의 역할·원문 연결을 확인하세요.", "reasons": eligibility}
                )
            if correction:
                allowed = {"subject", "predicate", "object"}
                if row["engine_version"] in {"heuristic-v2", "heuristic-v4", "heuristic-v5", "heuristic-v6", "heuristic-v7"}:
                    allowed |= EVENT_CORRECTION_FIELDS
                if row["engine_version"] == "heuristic-v5":
                    from .review_eligibility import V5_CORRECTION_FIELDS, v5_correction_reasons

                    allowed |= V5_CORRECTION_FIELDS
                    extra_errors = v5_correction_reasons(correction, payload)
                    if extra_errors:
                        raise HTTPException(
                            422, {"message": "v5 유형·역할 교정을 확인하세요.", "reasons": extra_errors}
                        )
                if row["engine_version"] in {"heuristic-v6", "heuristic-v7"}:
                    from .review_v6 import FIELDS, correction_reasons
                    allowed |= FIELDS
                    errors=correction_reasons(correction,payload,row["text_content"])
                    if errors:
                        raise HTTPException(422,{"message":"관계·관측 교정 근거를 확인하세요.","reasons":errors})
                if set(correction) - allowed:
                    raise HTTPException(422, "수정할 수 없는 원문·버전·추가 항목이 포함되어 있습니다.")
                semantic_errors = event_correction_reasons(correction, payload, row["text_content"])
                if (
                    row["engine_version"] in {"heuristic-v2", "heuristic-v4", "heuristic-v5", "heuristic-v6", "heuristic-v7"}
                    and semantic_errors
                ):
                    raise HTTPException(
                        422,
                        {"message": "수정 의미 필드와 원문 위치를 확인하세요.", "reasons": semantic_errors},
                    )
                directions = {
                    "increases": {"increase"},
                    "decreases": {"decrease"},
                    "associated_with": {"association"},
                    "affects": {"change", "no_effect"},
                    "binds": {"change"},
                }
                if "direction" in correction and correction["direction"] not in directions.get(
                    correction["predicate"], set()
                ):
                    raise HTTPException(422, "수정 관계와 결과 방향이 일치해야 합니다.")
                anchors = {row["subject"], row["object"]}
                if row["engine_version"] in {"heuristic-v6", "heuristic-v7"} and payload.get("observation"):
                    for field in ("intervention", "outcome"):
                        value=eligibility_payload["observation"].get(field) or {}
                        anchors.update(v for v in (value.get("text"),value.get("normalized_id")) if isinstance(v,str))
                for mention in con.execute(
                    "SELECT * FROM mentions WHERE extraction_id=? AND start>=? AND end<=?",
                    (row["extraction_id"], row["start"], row["end"]),
                ):
                    if row["text_content"][mention["start"] : mention["end"]] != mention["text"]:
                        continue
                    anchors.add(mention["text"])
                    if mention["normalized_id"]:
                        anchors.add(mention["normalized_id"])
                    if row["engine_version"] in {"heuristic-v5", "heuristic-v6", "heuristic-v7"}:
                        anchors.update(
                            _json(mention["payload"], {}).get("taxon_compatible_candidate_ids", [])
                        )
                    symbol = _json(mention["payload"], {}).get("symbol")
                    if symbol:
                        anchors.add(symbol)
                if (
                    correction["subject"] not in anchors
                    or correction["object"] not in anchors
                    or correction["subject"] == correction["object"]
                ):
                    raise HTTPException(422, "수정 주어·목적어는 해당 원문에 연결된 탐지 항목이어야 합니다.")
                permitted = {"increases", "decreases", "affects", "associated_with"}
                if row["engine_version"] in {"heuristic-v6", "heuristic-v7"} and payload.get("observation"):
                    permitted.add("has_observation")
                if row["engine_version"] in {"heuristic-v5", "heuristic-v6", "heuristic-v7"}:
                    permitted.add("binds")
                if correction["predicate"] not in permitted:
                    raise HTTPException(
                        422, "현재 후보 유형에서 지원하는 관계를 선택하세요."
                    )
            review_id = uid()
            con.execute(
                "INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)",
                (
                    review_id,
                    body.assertion_id,
                    username,
                    body.decision,
                    body.comment,
                    dumps(correction) if correction else None,
                    row["fingerprint"],
                    now(),
                ),
            )
            status = "rejected" if body.decision == "rejected" else "approved"
            con.execute("UPDATE assertions SET status=? WHERE assertion_id=?", (status, body.assertion_id))
            con.execute(
                "INSERT INTO audit VALUES(?,?,?,?,?)",
                (
                    uid(),
                    "review",
                    row["paper_id"],
                    dumps(
                        {
                            "review_id": review_id,
                            "reviewer": username,
                            "decision": body.decision,
                            "source_hash": row["source_hash"],
                            "fingerprint": row["fingerprint"],
                        }
                    ),
                    now(),
                ),
            )
        return {
            "review_id": review_id,
            "assertion_id": body.assertion_id,
            "status": status,
            "reviewer": username,
        }

    @service.get("/api/v1/runs")
    def runs(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        engine: str | None = None,
    ):
        selected_engine = _engine(settings, engine)
        with connect(settings) as con:
            read_context = _read_context(con, settings)
            total = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            items = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM runs ORDER BY started_at DESC LIMIT ? OFFSET ?", (limit, offset)
                )
            ]
            for item in items:
                item["detail"] = _json(item["detail"], {})
            jobs = [dict(r) for r in con.execute("SELECT * FROM jobs ORDER BY due_at DESC LIMIT 100")]
            queue = [
                dict(r)
                for r in con.execute("SELECT stage,status,COUNT(*) AS tasks FROM queue GROUP BY stage,status")
            ]
            engine_queue = [
                dict(r)
                for r in con.execute(
                    "SELECT engine_version,status,count(*) AS tasks FROM engine_queue GROUP BY engine_version,status"
                )
            ]
            engine_counts = {
                name: {"summarized_papers": 0, "active_extractions": 0, "relations": 0, "abstained_events": 0}
                for name in ENGINE_CHOICES
            }
            current_papers = {name: set() for name in ENGINE_CHOICES}
            current_extractions = {name: set() for name in ENGINE_CHOICES}
            for row in con.execute(_EXTRACTION_META_QUERY + " WHERE e.active=1"):
                name = row["engine_version"]
                if name not in engine_counts:
                    continue
                engine_counts[name]["active_extractions"] += 1
                if _extraction_validity(row, settings, read_context)["current_valid"]:
                    current_papers[name].add(row["paper_id"])
                    current_extractions[name].add(row["extraction_id"])
            for name in ENGINE_CHOICES:
                engine_counts[name]["summarized_papers"] = len(current_papers[name])
                engine_counts[name]["relations"] = sum(
                    row["relations"]
                    for row in con.execute(
                        "SELECT a.extraction_id,COUNT(*) AS relations FROM assertions a JOIN extraction_read_index e "
                        "ON e.extraction_id=a.extraction_id WHERE e.active=1 AND e.engine_version=? GROUP BY a.extraction_id",
                        (name,),
                    )
                    if row["extraction_id"] in current_extractions[name]
                )
                engine_counts[name]["abstained_events"] = con.execute(
                    "SELECT COALESCE(SUM(e.abstained_count),0) FROM extraction_read_index e WHERE e.active=1 AND e.engine_version=?",
                    (name,),
                ).fetchone()[0]
            counts = {
                "papers": con.execute("SELECT COUNT(*) FROM papers").fetchone()[0],
                "summarized_papers": len(current_papers[selected_engine]),
                "oa_papers": con.execute(
                    "SELECT COUNT(DISTINCT paper_id) FROM sources WHERE kind='fulltext'"
                ).fetchone()[0],
                "retracted_papers": con.execute("SELECT COUNT(*) FROM papers WHERE retracted=1").fetchone()[
                    0
                ],
            }
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "jobs": jobs,
            "queue": queue,
            "engine_queue": engine_queue,
            "counts": counts,
            "engine": selected_engine,
            "engine_counts": engine_counts,
        }

    @service.get("/api/v1/reports")
    def reports(
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        include_snapshot: bool = True,
    ):
        with connect(settings) as con:
            total = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
            items = []
            projection = (
                "*"
                if include_snapshot
                else (
                    "report_id,generated_at,period_start,period_end,snapshot_hash,paths,"
                    "CASE WHEN json_valid(snapshot_json) THEN json_object('counts',json_extract(snapshot_json,'$.counts'),"
                    "'quality',json_extract(snapshot_json,'$.quality'),'engine_counts',json_extract(snapshot_json,'$.engine_counts'),"
                    "'interpretation',json_extract(snapshot_json,'$.interpretation'),"
                    "'engine_version',json_extract(snapshot_json,'$.engine_version'),"
                    "'engine_counts',json_extract(snapshot_json,'$.engine_counts')) ELSE '{}' END AS snapshot_json"
                )
            )
            for row in con.execute(
                "SELECT " + projection + " FROM reports ORDER BY generated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ):
                item = dict(row)
                item["snapshot"] = _json(item.pop("snapshot_json"), {})
                item["snapshot_included"] = include_snapshot
                item["downloads"] = {
                    format_name: f"/api/v1/reports/{quote(item['report_id'], safe='')}/download/{format_name}"
                    for format_name in _json(item.pop("paths"), {})
                    if format_name in ("html", "markdown", "csv")
                }
                items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @service.get("/api/v1/reports/{report_id}/download/{format_name}")
    def download(report_id: str, format_name: Literal["html", "markdown", "csv"]):
        with connect(settings) as con:
            row = con.execute("SELECT paths FROM reports WHERE report_id=?", (report_id,)).fetchone()
        path = _json(row["paths"], {}).get(format_name) if row else None
        if not isinstance(path, str):
            raise HTTPException(404, "리포트 파일이 없습니다.")
        report_root = settings.reports.resolve()
        resolved = Path(path).resolve() if Path(path).is_absolute() else (report_root / path).resolve()
        if not resolved.is_relative_to(report_root) or not resolved.is_file():
            raise HTTPException(404, "리포트 파일이 없습니다.")
        suffixes = {"html": ".html", "markdown": ".md", "csv": ".csv"}
        if resolved.suffix.lower() != suffixes[format_name]:
            raise HTTPException(404, "리포트 형식이 일치하지 않습니다.")
        types = {"html": "text/html", "markdown": "text/markdown", "csv": "text/csv"}
        return FileResponse(
            resolved,
            media_type=types[format_name],
            filename=resolved.name,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            },
        )

    return service


app = create_app()
