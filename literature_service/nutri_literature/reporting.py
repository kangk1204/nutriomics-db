"""Freeze a source-linked report; back up SQLite through its online backup API."""

import csv
import html
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .db import connect, digest, dumps, now, uid
from .registry_v6 import ENGINE_CHOICES, default_engine, engine_manifest_validation, validate_engine
from .review_eligibility import (
    EVENT_CORRECTION_FIELDS,
    event_correction_reasons,
    event_review_reasons,
    has_explicit_relation,
)


def _object(value):
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, ValueError):
        return {}


def _engine_where():
    # Older exports allowed arbitrary v1 engine labels. Retain that legacy
    # representation only when no registered engine is claimed in the result.
    return (
        "(e.engine_version=? OR (?='heuristic-v1' AND e.engine_version NOT IN ('heuristic-v1','heuristic-v2') "
        "AND json_valid(e.result_json) AND json_extract(e.result_json,'$.engine_version') IS NULL))"
    )


def _engine_state(con, paper, engine, settings):
    extraction = con.execute(
        "SELECT e.*,s.kind AS source_kind,s.url AS source_url,s.license AS source_license,"
        "s.text_hash AS source_text_hash,s.text_content AS source_text FROM extractions e "
        "JOIN sources s USING(source_id) WHERE e.paper_id=? AND e.active=1 AND "
        + _engine_where()
        + " ORDER BY (e.engine_version=?) DESC,e.created_at DESC,e.extraction_id DESC LIMIT 1",
        (paper["paper_id"], engine, engine, engine),
    ).fetchone()
    state = {
        "extraction": dict(extraction) if extraction else None,
        "extraction_current": False,
        "assertions": [],
    }
    if not extraction:
        return state
    saved = state["extraction"]
    source_text = saved.pop("source_text")
    saved["result"] = _object(saved.pop("result_json"))
    reasons = engine_manifest_validation(saved["result"], engine, settings=settings)
    if extraction["source_id"] != paper["current_source_id"]:
        reasons.append("source_changed")
    if not extraction["source_hash"] == extraction["source_text_hash"] == digest(source_text):
        reasons.append("text_integrity")
    saved["stale_reasons"] = reasons
    state["extraction_current"] = not reasons
    for row in con.execute(
        "SELECT * FROM assertions WHERE extraction_id=? ORDER BY start,assertion_id",
        (extraction["extraction_id"],),
    ):
        assertion = dict(row)
        assertion["reviews"] = [
            dict(review)
            for review in con.execute(
                "SELECT reviewer,decision,comment,corrected_payload,fingerprint,created_at FROM reviews "
                "WHERE assertion_id=? ORDER BY created_at,rowid",
                (assertion["assertion_id"],),
            )
        ]
        latest = assertion["reviews"][-1] if assertion["reviews"] else {}
        payload = _object(assertion["payload"])
        event_payload = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        eligibility = event_review_reasons(payload, source_text, engine)
        if engine == "heuristic-v6" and payload.get("observation"):
            from .api import _event_matches_assertion
            if not _event_matches_assertion(payload, {**assertion,"engine_version":engine}):
                eligibility.append("event_assertion_roles_conflict")
        elif engine in {"heuristic-v2", "heuristic-v4", "heuristic-v5", "heuristic-v6"} and (
            event_payload.get("subject") != assertion["subject"]
            or event_payload.get("object") != assertion["object"]
        ):
            eligibility.append("event_assertion_roles_conflict")
        correction = _object(latest.get("corrected_payload")) if latest.get("decision") == "corrected" else {}
        correction_mentions = []
        if (
            engine in {"heuristic-v2", "heuristic-v4", "heuristic-v5", "heuristic-v6"}
            and latest.get("decision") == "corrected"
        ):
            allowed = EVENT_CORRECTION_FIELDS | {"subject", "predicate", "object"}
            if engine == "heuristic-v5":
                from .review_eligibility import (
                    V5_CORRECTION_FIELDS,
                    v5_correction_reasons,
                    corrected_v5_event,
                )

                allowed |= V5_CORRECTION_FIELDS
                eligibility = event_review_reasons(
                    {**payload, "event": corrected_v5_event(payload, correction)}, source_text, engine
                )
                eligibility.extend(v5_correction_reasons(correction, payload))
            elif engine == "heuristic-v6":
                from .review_v6 import FIELDS, corrected_event, corrected_observation, correction_reasons
                allowed |= FIELDS
                effective_payload={**payload,"event":corrected_event(payload,correction)}
                if payload.get("observation"):
                    effective_payload["observation"]=corrected_observation(payload,correction)
                    assertion["effective_observation"]=effective_payload["observation"]
                eligibility=event_review_reasons(effective_payload,source_text,engine)
                eligibility.extend(correction_reasons(correction,payload,source_text))
            if not {"subject", "predicate", "object"}.issubset(correction) or set(correction) - allowed:
                eligibility.append("invalid_correction_fields")
            eligibility.extend(
                event_correction_reasons(correction, {**payload, "event": event_payload}, source_text)
            )
            correction_mentions = [event_payload.get("controller", {}), event_payload.get("theme", {})]
            for mention in con.execute(
                "SELECT * FROM mentions WHERE extraction_id=? AND start>=? AND end<=?",
                (extraction["extraction_id"], assertion["start"], assertion["end"]),
            ):
                if source_text[mention["start"] : mention["end"]] == mention["text"]:
                    correction_mentions.append(dict(mention))
            anchors = {assertion["subject"], assertion["object"]}
            if engine == "heuristic-v6" and payload.get("observation"):
                for field in ("intervention","outcome"):
                    value=effective_payload["observation"].get(field) or {}
                    anchors.update(v for v in (value.get("text"),value.get("normalized_id")) if isinstance(v,str))
            for mention in correction_mentions:
                anchors.update(
                    value
                    for value in (
                        mention.get("text"),
                        mention.get("normalized_id"),
                        _object(mention.get("payload")).get("symbol"),
                    )
                    if isinstance(value, str)
                )
                if engine in {"heuristic-v5","heuristic-v6"}:
                    anchors.update(_object(mention.get("payload")).get("taxon_compatible_candidate_ids", []))
            if any(
                not isinstance(correction.get(field), str) or correction[field] not in anchors
                for field in ("subject", "object")
            ):
                eligibility.append("unanchored_correction_roles")
            directions = {
                "increases": {"increase"},
                "decreases": {"decrease"},
                "associated_with": {"association"},
                "affects": {"change", "no_effect"},
            }
            if engine in {"heuristic-v5","heuristic-v6"}:
                directions["binds"] = {"change"}
            if engine == "heuristic-v6" and payload.get("observation"):
                directions["has_observation"] = set()
            predicate = correction.get("predicate")
            if (
                not isinstance(predicate, str)
                or predicate not in directions
                or (
                    "direction" in correction
                    and (
                        not isinstance(correction["direction"], str)
                        or correction["direction"] not in directions.get(predicate, set())
                    )
                )
            ):
                eligibility.append("invalid_correction_direction")
        review_matches = (
            latest.get("fingerprint") == assertion["fingerprint"]
            and latest.get("decision") in ("approved", "corrected")
            and has_explicit_relation(payload)
            and not eligibility
        )
        assertion["review_eligibility_reasons"] = eligibility
        assertion["effective"] = {field: assertion[field] for field in ("subject", "predicate", "object")}
        if assertion["status"] == "approved" and correction:
            assertion["effective"].update(
                {field: value for field, value in correction.items() if field in assertion["effective"]}
            )
        if engine in {"heuristic-v2", "heuristic-v4", "heuristic-v5", "heuristic-v6"}:
            event = dict(event_payload)
            if assertion["status"] == "approved" and correction:
                event.update(
                    {
                        field: value
                        for field, value in correction.items()
                        if field in EVENT_CORRECTION_FIELDS | {"subject", "object"}
                    }
                )
                if "direction" not in correction and correction.get("predicate") != assertion["predicate"]:
                    event["direction"] = {
                        "increases": "increase",
                        "decreases": "decrease",
                        "associated_with": "association",
                        "affects": "change",
                    }.get(correction.get("predicate"), event.get("direction"))
                event["statistical_significance"] = event.get("significance", "unknown")
                event["polarity"] = (
                    "null"
                    if event.get("direction") == "no_effect"
                    else "negated"
                    if event.get("negation")
                    else "positive"
                )
                for role, endpoint in (("controller", "subject"), ("theme", "object")):
                    mention = next(
                        (
                            m
                            for m in correction_mentions
                            if event.get(endpoint) in (m.get("text"), m.get("normalized_id"))
                        ),
                        None,
                    )
                    if mention:
                        event[role] = mention
            assertion["effective_event"] = event
            if engine == "heuristic-v5" and assertion["status"] == "approved" and correction:
                assertion["effective_event"] = corrected_v5_event(payload, correction)
            if engine == "heuristic-v6" and assertion["status"] == "approved" and correction and payload.get("event"):
                assertion["effective_event"] = corrected_event(payload, correction)
        assertion["source_span_valid"] = (
            0 <= assertion["start"] < assertion["end"] <= len(source_text)
            and source_text[assertion["start"] : assertion["end"]] == assertion["quote"]
        )
        assertion["display_status"] = (
            assertion["status"]
            if (
                state["extraction_current"]
                and assertion["source_span_valid"]
                and not paper["retracted"]
                and (assertion["status"] != "approved" or review_matches)
            )
            else "needs_review"
        )
        state["assertions"].append(assertion)
    return state


def _period(value, default):
    moment = datetime.fromisoformat(value) if isinstance(value, str) else (value or default)
    if moment.tzinfo is None:
        raise ValueError("Report dates must include a timezone")
    return moment.astimezone(UTC).isoformat()


def _period_activity(con, paper, start, end, engine=None):
    """Retain actual event identities separately from current assertion state."""
    pid = paper["paper_id"]
    engine_clause = " AND " + _engine_where() if engine else ""
    args = (pid, start, end, engine, engine) if engine else (pid, start, end)
    return {
        "paper_created": start <= paper["created_at"] < end,
        "paper_updated": start <= paper["updated_at"] < end,
        "sources": [
            dict(row)
            for row in con.execute(
                "SELECT source_id,kind,sha256,text_hash,url,license,retrieved_at FROM sources "
                "WHERE paper_id=? AND retrieved_at>=? AND retrieved_at<? ORDER BY retrieved_at,source_id",
                (pid, start, end),
            )
        ],
        "extractions": [
            dict(row)
            for row in con.execute(
                "SELECT extraction_id,source_id,source_hash,engine_version,dictionary_version,created_at "
                "FROM extractions e WHERE paper_id=? AND created_at>=? AND created_at<? "
                + engine_clause
                + " ORDER BY created_at,extraction_id",
                args,
            )
        ],
        "reviews": [
            dict(row)
            for row in con.execute(
                "SELECT r.review_id,r.assertion_id,a.extraction_id,e.source_id,e.source_hash,"
                "r.fingerprint,a.fingerprint AS assertion_fingerprint,r.reviewer,r.decision,r.created_at "
                "FROM reviews r JOIN assertions a USING(assertion_id) "
                "JOIN extractions e USING(extraction_id) WHERE a.paper_id=? "
                "AND r.created_at>=? AND r.created_at<? " + engine_clause + " ORDER BY r.created_at,r.rowid",
                args,
            )
        ],
    }


def _study_counts(papers):
    """Count explicit trial identities without treating unlinked papers as studies."""
    import re

    families = set()
    linked = 0
    for paper in papers:
        saved = (paper.get("extraction") or {}).get("result", {})
        ids = (
            {
                item.get("id")
                for item in saved.get("study_families", [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and re.fullmatch(r"NCT\d{8}", item["id"])
            }
            if paper.get("extraction_current")
            else set()
        )
        families.update(ids)
        linked += bool(ids)
    return {
        "identified_study_families": len(families),
        "papers_with_study_family": linked,
        "papers_without_study_family": len(papers) - linked,
        "study_count_status": "partial_explicit_NCT_only",
    }


def _snapshot(settings, start, end, engine=None):
    selected_engine = validate_engine(engine or default_engine(settings))
    with connect(settings) as con:
        con.execute("BEGIN")
        papers = [
            dict(row)
            for row in con.execute(
                "SELECT p.* FROM papers p WHERE (p.created_at>=? AND p.created_at<?) "
                "OR (p.updated_at>=? AND p.updated_at<?) "
                "OR EXISTS(SELECT 1 FROM sources s WHERE s.paper_id=p.paper_id "
                "AND s.retrieved_at>=? AND s.retrieved_at<?) "
                "OR EXISTS(SELECT 1 FROM extractions e WHERE e.paper_id=p.paper_id "
                "AND e.created_at>=? AND e.created_at<? AND " + _engine_where() + ") "
                "OR EXISTS(SELECT 1 FROM reviews r JOIN assertions a USING(assertion_id) "
                "JOIN extractions e USING(extraction_id) WHERE a.paper_id=p.paper_id AND r.created_at>=? AND r.created_at<? AND "
                + _engine_where()
                + ") ORDER BY p.pmid",
                (start, end) * 3 + (start, end, selected_engine, selected_engine) * 2,
            )
        ]
        engine_diagnostics = {
            name: {"identity_deferred": Counter(), "relation_deferred": Counter()} for name in ENGINE_CHOICES
        }
        for paper in papers:
            pid = paper["paper_id"]
            paper["period_activity"] = _period_activity(con, paper, start, end, selected_engine)
            paper["topics"] = [
                dict(row)
                for row in con.execute(
                    "SELECT DISTINCT t.topic_id,t.name,t.query_version FROM paper_topics p JOIN topics t USING(topic_id) "
                    "WHERE p.paper_id=? AND p.query_version=t.query_version AND t.enabled=1 ORDER BY t.topic_id",
                    (pid,),
                )
            ]
            paper.update(_engine_state(con, paper, selected_engine, settings))
            paper["engine_comparison"] = {}
            for name in ENGINE_CHOICES:
                state = paper if name == selected_engine else _engine_state(con, paper, name, settings)
                extraction = state["extraction"] or {}
                result = extraction.get("result", {})
                abstentions = result.get("abstentions", [])
                saved_events = result.get("events", [])
                if state["extraction_current"]:
                    engine_diagnostics[name]["identity_deferred"].update(
                        h["reason"] for h in result.get("entity_hypotheses", []) if h.get("reason")
                    )
                    engine_diagnostics[name]["relation_deferred"].update(
                        reason
                        for h in result.get("relation_hypotheses", [])
                        if h.get("type_selected")
                        for reason in h.get("reasons", [])
                    )
                paper["engine_comparison"][name] = {
                    "summarized_papers": int(state["extraction_current"]),
                    "relation_candidates": len(state["assertions"]),
                    "approved_relations": sum(a["display_status"] == "approved" for a in state["assertions"]),
                    "abstained_events": len(abstentions) if isinstance(abstentions, list) else 0,
                    "background_events": sum(
                        e.get("status") == "background" for e in saved_events if isinstance(e, dict)
                    )
                    if isinstance(saved_events, list)
                    else 0,
                    "duplicate_events_grouped": result.get("duplicate_events_grouped", 0)
                    if type(result.get("duplicate_events_grouped", 0)) is int
                    else 0,
                    "extraction_id": extraction.get("extraction_id"),
                    "source_hash": extraction.get("source_hash"),
                    "rules_version": result.get("rules_version"),
                    "dictionary_version": extraction.get("dictionary_version"),
                    "manifest": result.get("manifest"),
                }
        counts = {
            "papers_total": con.execute("SELECT count(*) FROM papers").fetchone()[0],
            "new_papers": con.execute(
                "SELECT count(*) FROM papers WHERE created_at>=? AND created_at<?", (start, end)
            ).fetchone()[0],
            "changed_papers": sum(paper["created_at"] < start for paper in papers),
            "papers_in_report": len(papers),
            "abstract_only": sum(
                not p["extraction"] or p["extraction"]["source_kind"] == "pubmed" for p in papers
            ),
            "full_text": sum(
                bool(p["extraction"] and p["extraction"]["source_kind"] == "fulltext") for p in papers
            ),
            "retracted": sum(p["retracted"] for p in papers),
            "relation_candidates": sum(len(p["assertions"]) for p in papers),
            "approved_relations": sum(
                a["display_status"] == "approved" for p in papers for a in p["assertions"]
            ),
            "summarized_papers": sum(p["extraction_current"] for p in papers),
            "full_text_rate": (
                sum(bool(p["extraction"] and p["extraction"]["source_kind"] == "fulltext") for p in papers)
                / len(papers)
                if papers
                else None
            ),
            "review_decisions": sum(len(p["period_activity"]["reviews"]) for p in papers),
        }
        runs = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM runs WHERE started_at>=? AND started_at<? ORDER BY started_at", (start, end)
            )
        ]
        counts["failed_runs"] = sum(r["status"] in ("failed", "partial") for r in runs)
        counts.update(_study_counts(papers))
        processing_kinds = {f"process_extract_{selected_engine}"}
        if selected_engine == "heuristic-v1":
            processing_kinds.add("process_extract")
        counts["processing_run_count"] = sum(r["kind"] in processing_kinds for r in runs)
        counts["processed_unique_papers_in_period"] = sum(
            bool(p["period_activity"]["extractions"]) for p in papers
        )
        metric_fields = (
            "summarized_papers",
            "relation_candidates",
            "approved_relations",
            "abstained_events",
            "background_events",
            "duplicate_events_grouped",
        )
        engine_counts = {
            name: {key: sum(p["engine_comparison"][name][key] for p in papers) for key in metric_fields}
            for name in ENGINE_CHOICES
        }
        counts["abstained_events"] = engine_counts[selected_engine]["abstained_events"]
        counts["duplicate_events_grouped"] = engine_counts[selected_engine]["duplicate_events_grouped"]
    return {
        "schema_version": "literature-report-v2",
        "generated_at": now(),
        "period_start": start,
        "period_end": end,
        "engine_version": selected_engine,
        "engine_counts": engine_counts,
        "engine_diagnostics": {
            name: {key: dict(value) for key, value in reasons.items()}
            for name, reasons in engine_diagnostics.items()
        },
        "engine_counts_basis": "same_report_papers_current_saved_state_at_generation",
        "counts": counts,
        "papers": papers,
        "runs": runs,
        "temporal_basis": {
            "selection": "recorded_activity_in_half_open_period",
            "state": "current_saved_state_at_generation",
            "review_decisions": "review_events_created_in_period_selected_engine_assertions",
            "note": "기간 내 생성·변경·원문·추출·검토 기록이 있는 문헌의 생성 시점 현재 상태입니다. "
            "검토 기록 수는 기간 내 이벤트이며, 승인 관계 수는 현재 유효한 상태입니다. 기간 말 상태를 재구성하지 않습니다.",
        },
        "quality": {
            "status": "not_evaluated",
            "precision": None,
            "recall": None,
            "f1": None,
            "note": "수동 검토한 정답셋의 평가 전에는 추출 정확도를 산출하지 않습니다.",
            "release_status": "candidate_beta",
            "evaluation_scope": "public_benchmark_results_do_not_establish_nutrition_domain_accuracy",
            "domain_accuracy": "not_measured",
            "review_time_saved": "not_measured",
            "training_evaluation_overlap": "not_established",
        },
        "interpretation": "자동 추출은 후보입니다. 원문 공기출현이나 규칙 점수는 인과관계 또는 정확도가 아닙니다.",
    }


def _text(value):
    if isinstance(value, dict):
        return value.get("text", dumps(value))
    return str(value) if value else "미확인"


def _render(snapshot):
    counts = snapshot["counts"]
    lines = [
        "# 영양유전체 문헌 정리",
        "",
        f"기간: {snapshot['period_start']} — {snapshot['period_end']}",
        "",
        f"신규 {counts['new_papers']}편 · 변경 {counts['changed_papers']}편 · 관계 후보 {counts['relation_candidates']}건",
        "",
        snapshot["interpretation"],
        "",
        snapshot["quality"]["note"],
        "",
        snapshot["temporal_basis"]["note"],
        "",
    ]

    def esc(value):
        return html.escape(str(value), quote=True)

    overview = (
        f"누적 고유 문헌 {counts['papers_total']}편 · 기간 신규/변경 고유 문헌 {counts['papers_in_report']}편 · "
        f"기간 문헌의 현재 요약 {counts['summarized_papers']}편 · 본문 확보 {counts['full_text']}편 · "
        f"검수 승인 관계 {counts['approved_relations']}건 · 철회 {counts['retracted']}편 · 실패 실행 {counts['failed_runs']}건"
    )
    study_overview = (
        f"기간 추출 고유 문헌 {counts.get('processed_unique_papers_in_period', 0)}편 · "
        f"추출 작업 실행 {counts.get('processing_run_count', 0)}회 · "
        f"명시적 NCT 연구 가족 {counts.get('identified_study_families', 0)}개 · "
        f"연구 식별자 미연결 문헌 {counts.get('papers_without_study_family', 0)}편. "
        "연구 수는 명시적 NCT 연결 범위이며 전체 연구 수는 미확인입니다."
    )
    lines += [overview, ""]
    lines += [study_overview, ""]
    sections = [
        "<h1>영양유전체 문헌 정리</h1>",
        f"<p>기간: {esc(snapshot['period_start'])} — {esc(snapshot['period_end'])}</p>",
        f"<p>신규 {counts['new_papers']}편 · 변경 {counts['changed_papers']}편 · 관계 후보 {counts['relation_candidates']}건</p>",
        f"<p>{esc(snapshot['interpretation'])}</p><p>{esc(snapshot['quality']['note'])}</p>",
        f"<p>{esc(snapshot['temporal_basis']['note'])}</p>",
        f"<p>{esc(overview)}</p>",
        f"<p>{esc(study_overview)}</p>",
    ]
    selected = snapshot.get("engine_version", "heuristic-v1")
    beta_note = (
        "후보 검수용 베타: 공개 평가와 영양 문헌 정확도는 별도로 평가하며 검수 시간 절감은 미측정입니다."
    )
    lines += [
        f"선택 엔진: {selected}",
        "",
        beta_note,
        "",
        "엔진별 비교는 동일 리포트 문헌의 생성 시점 저장 상태를 기준으로 합니다.",
        "",
    ]
    sections += [f"<p><b>선택 엔진:</b> {esc(selected)}</p><p>{esc(beta_note)}</p>"]
    for name, values in snapshot.get("engine_counts", {}).items():
        description = (
            f"{name}: 현재 요약 {values['summarized_papers']}편 · 관계 후보 {values['relation_candidates']}건 · "
            f"검수 승인 {values['approved_relations']}건 · 보류 {values['abstained_events']}건 · "
            f"배경 {values['background_events']}건 · 중복 통합 {values['duplicate_events_grouped']}건"
        )
        lines += [f"- {description}"]
        sections += [f"<p>{esc(description)}</p>"]
    lines += [""]
    for paper in snapshot["papers"]:
        title, pmid = paper["title"], paper["pmid"]
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        lines += [
            f"## PMID {pmid}: {title}",
            "",
            f"원문: {url}",
            "주제: " + ", ".join(t["name"] for t in paper["topics"]),
            "",
        ]
        sections += [
            f"<article><h2>PMID {esc(pmid)}: {esc(title)}</h2>",
            f'<p><a href="{esc(url)}">PubMed 원문</a> · {esc(", ".join(t["name"] for t in paper["topics"]))}</p>',
        ]
        activity = paper["period_activity"]
        activity_text = (
            f"기간 내 기록: 원문 {len(activity['sources'])}건 · 추출 {len(activity['extractions'])}건 · "
            f"검토 {len(activity['reviews'])}건"
        )
        lines += [activity_text, ""]
        sections += [f"<p>{esc(activity_text)}</p>"]
        for event in activity["reviews"]:
            event_text = (
                f"검토 기록 {event['review_id']}: {event['decision']} ({event['created_at']}); "
                f"관계 {event['assertion_id']}, 추출 {event['extraction_id']}"
            )
            lines += [f"- {event_text}"]
            sections += [f"<p>{esc(event_text)}</p>"]
        if paper["retracted"]:
            lines += ["**철회 표시: 해석에 사용하지 않음**", ""]
            sections += ["<p><strong>철회 표시: 해석에 사용하지 않음</strong></p>"]
        extraction = paper["extraction"]
        if not extraction:
            lines += ["요약: 추출 대기", ""]
            sections += ["<p>요약: 추출 대기</p></article>"]
            continue
        summary = extraction["result"].get("summary", {})
        for key, label in [("method", "연구설계"), ("results", "주요 결과"), ("limitations", "한계")]:
            values = summary.get(key, "미확인")
            values = values if isinstance(values, list) else [values]
            if not values:
                values = [
                    "관측 결과 미확인" if key == "results" and summary.get("missing_results") else "미확인"
                ]
            for value in values:
                text = _text(value)
                locator = (
                    f" [문자 {value['start']}:{value['end']}]"
                    if isinstance(value, dict) and "start" in value and "end" in value
                    else ""
                )
                lines += [f"- {label}: {text}{locator}"]
                sections += [f"<p><b>{label}</b>: {esc(text)}{esc(locator)}</p>"]
        lines += [
            "",
            f"근거 SHA256: {extraction['source_hash']}",
            f"엔진: {extraction['engine_version']} · 사전: {extraction['dictionary_version']}",
            f"규칙: {extraction['result'].get('rules_version', '미확인')}",
            "",
        ]
        sections += [
            (
                f"<p class=provenance>근거 SHA256: {esc(extraction['source_hash'])}<br>"
                f"엔진: {esc(extraction['engine_version'])} · 사전: {esc(extraction['dictionary_version'])}<br>"
                f"규칙: {esc(extraction['result'].get('rules_version', '미확인'))}</p>"
            )
        ]
        parser = _object(_object(extraction["result"].get("manifest")).get("parser"))
        if parser:
            model_note = f"구문분석 모델: {parser.get('name')} {parser.get('version')} · SHA256 {parser.get('model_sha256', '미확인')}"
            lines += [model_note, ""]
            sections += [f"<p class=provenance>{esc(model_note)}</p>"]
        for assertion in paper["assertions"]:
            effective = assertion["effective"]
            relation = f"{effective['subject']} → {effective['predicate']} → {effective['object']}"
            lines += [
                f"- 관계 후보 ({assertion['display_status']}): {relation}",
                f"  근거 [{assertion['start']}:{assertion['end']}]: {assertion['quote']}",
            ]
            sections += [
                (
                    f"<p><b>관계 후보 ({esc(assertion['display_status'])})</b>: {esc(relation)}</p>"
                    f"<blockquote>{esc(assertion['quote'])} [{assertion['start']}:{assertion['end']}]</blockquote>"
                )
            ]
            event = assertion.get("effective_event")
            if event:
                detail = (
                    f"이벤트: {event.get('outcome_type')} · 방향 {event.get('direction')} · "
                    f"부정 {event.get('negation')} · 추정 {event.get('speculation')} · 통계 {event.get('significance')} · "
                    f"종 {event.get('species')} · 모델 {event.get('study_model')}"
                )
                lines += [f"  {detail}", f"  연구 조건: {dumps(event.get('conditions', {}))}"]
                sections += [
                    f"<p>{esc(detail)}</p><p>연구 조건: {esc(dumps(event.get('conditions', {})))}</p>"
                ]
                for quote in event.get("evidence_quotes", []):
                    if quote.get("quote") != assertion["quote"]:
                        lines += [
                            f"  추가 근거 [{quote.get('start')}:{quote.get('end')}]: {quote.get('quote')}"
                        ]
                        sections += [
                            f"<blockquote>{esc(quote.get('quote'))} [{quote.get('start')}:{quote.get('end')}]</blockquote>"
                        ]
        sections += ["</article>"]
        lines += [""]
    if not snapshot["papers"]:
        lines += ["해당 기간 신규·변경 문헌이 없습니다."]
        sections += ["<p>해당 기간 신규·변경 문헌이 없습니다.</p>"]
    style = "body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 24px;line-height:1.6}article{border-top:1px solid #ccd;padding:20px 0}blockquote{border-left:3px solid #aac;padding-left:16px}.provenance{font-size:12px;overflow-wrap:anywhere}"
    return (
        "\n".join(lines) + "\n",
        f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>영양유전체 문헌 정리</title><style>{style}</style><body>'
        + "\n".join(sections)
        + "</body></html>",
    )


def generate_report(settings, period_start=None, period_end=None, engine=None):
    current = datetime.now(UTC)
    end = _period(period_end, current)
    start = _period(period_start, datetime.fromisoformat(end) - timedelta(days=7))
    if start >= end:
        raise ValueError("Report period must have start before end")
    snapshot = _snapshot(settings, start, end, engine=engine)
    report_id = uid()
    directory = settings.reports / report_id
    directory.mkdir(parents=True, exist_ok=False)
    markdown, document = _render(snapshot)
    paths = {
        kind: str(directory / filename)
        for kind, filename in [("html", "report.html"), ("markdown", "report.md"), ("csv", "evidence.csv")]
    }
    Path(paths["html"]).write_text(document, encoding="utf-8")
    Path(paths["markdown"]).write_text(markdown, encoding="utf-8")
    fields = [
        "record_type",
        "pmid",
        "title",
        "topics",
        "source_hash",
        "engine_version",
        "dictionary_version",
        "rules_version",
        "subject",
        "predicate",
        "object",
        "status",
        "quote",
        "start",
        "end",
        "pubmed_url",
        "activity_period_start",
        "activity_period_end",
        "state_time_basis",
        "report_engine_version",
        "engine_counts",
        "release_status",
        "evaluation_scope",
        "model_name",
        "model_version",
        "model_sha256",
        "outcome_type",
        "direction",
        "negation",
        "speculation",
        "significance",
        "species",
        "study_model",
        "conditions",
        "evidence_quotes",
    ]
    with Path(paths["csv"]).open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for paper in snapshot["papers"]:
            extraction = paper["extraction"] or {}
            result = extraction.get("result", {})
            parser = _object(_object(result.get("manifest")).get("parser"))
            base = {
                "pmid": paper["pmid"],
                "title": paper["title"],
                "topics": ";".join(t["topic_id"] for t in paper["topics"]),
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{paper['pmid']}/",
                "rules_version": extraction.get("result", {}).get("rules_version", ""),
                "activity_period_start": start,
                "activity_period_end": end,
                "state_time_basis": snapshot["temporal_basis"]["state"],
                "report_engine_version": snapshot["engine_version"],
                "engine_counts": dumps(snapshot["engine_counts"]),
                "release_status": snapshot["quality"]["release_status"],
                "evaluation_scope": snapshot["quality"]["evaluation_scope"],
                "model_name": parser.get("name", ""),
                "model_version": parser.get("version", ""),
                "model_sha256": parser.get("model_sha256", ""),
                **{
                    key: extraction.get(key, "")
                    for key in ["source_hash", "engine_version", "dictionary_version"]
                },
            }
            rows = [{"record_type": "paper", **base}]
            rows += [
                {
                    "record_type": "relation_candidate",
                    **base,
                    **{key: assertion[key] for key in ["quote", "start", "end"]},
                    **assertion["effective"],
                    "status": assertion["display_status"],
                    **{
                        key: assertion.get("effective_event", {}).get(key, "")
                        for key in (
                            "outcome_type",
                            "direction",
                            "negation",
                            "speculation",
                            "significance",
                            "species",
                            "study_model",
                        )
                    },
                    "conditions": dumps(assertion["effective_event"].get("conditions", {}))
                    if assertion.get("effective_event")
                    else "",
                    "evidence_quotes": dumps(assertion["effective_event"].get("evidence_quotes", []))
                    if assertion.get("effective_event")
                    else "",
                }
                for assertion in paper["assertions"]
            ]
            for row in rows:
                writer.writerow(
                    {
                        key: "'" + value
                        if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                        else value
                        for key, value in row.items()
                    }
                )
    serialized = dumps(snapshot)
    (directory / "snapshot.json").write_text(serialized, encoding="utf-8")
    snapshot_hash = digest(serialized)
    with connect(settings) as con:
        con.execute(
            "INSERT INTO reports VALUES(?,?,?,?,?,?,?)",
            (report_id, snapshot["generated_at"], start, end, snapshot_hash, serialized, dumps(paths)),
        )
    return {
        "report_id": report_id,
        "snapshot_hash": snapshot_hash,
        "counts": snapshot["counts"],
        "paths": paths,
        "engine_version": snapshot["engine_version"],
        "engine_counts": snapshot["engine_counts"],
    }


def backup(settings):
    settings.ensure()
    if not settings.database.is_file():
        raise FileNotFoundError("Literature database does not exist")
    current = datetime.now(UTC)
    path = settings.backups / ("literature-" + current.strftime("%Y%m%dT%H%M%SZ") + "-" + uid() + ".sqlite3")
    source = sqlite3.connect(settings.database.resolve().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(path)
    try:
        source.execute('BEGIN')
        source.execute('SELECT count(*) FROM papers').fetchone()
        source.backup(destination, pages=256, sleep=0.05)
        checks = [row[0] for row in destination.execute("PRAGMA integrity_check")]
        foreign_keys = destination.execute("PRAGMA foreign_key_check").fetchall()
        if checks != ["ok"] or foreign_keys:
            raise ValueError("Backup integrity check failed")
    finally:
        destination.close()
        source.close()
    receipt = {
        "path": str(path),
        "sha256": digest(path.read_bytes()),
        "created_at": current.isoformat(),
        "integrity": "ok",
        "retention_days": 30,
    }
    path.with_suffix(".json").write_text(dumps(receipt), encoding="utf-8")
    cutoff = current - timedelta(days=30)
    removed = 0
    for candidate in settings.backups.glob("literature-*.sqlite3"):
        try:
            timestamp = datetime.strptime(candidate.name.split("-")[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except (ValueError, IndexError):
            continue
        if timestamp < cutoff and candidate.parent.resolve() == settings.backups.resolve():
            candidate.unlink()
            candidate.with_suffix(".json").unlink(missing_ok=True)
            removed += 1
    return {**receipt, "removed_old_backups": removed}
