"""Internal Korean review workspace. Start with streamlit run ui/app.py."""

import html
import json
import math
import os

import plotly.graph_objects as go
import requests
import streamlit as st

API = os.environ.get("LITERATURE_API_URL", "http://127.0.0.1:8010").rstrip("/")
st.set_page_config(page_title="문헌 근거 검수", page_icon="📚", layout="wide")


def api(path, method="GET", *, params=None, body=None, download=False):
    headers = {}
    if st.session_state.get("token"):
        headers["Authorization"] = "Bearer " + st.session_state["token"]
    if method == "GET" and path.startswith(
        (
            "/api/v1/papers",
            "/api/v1/evidence-map",
            "/api/v1/events",
            "/api/v1/observations",
            "/api/v1/runs",
            "/api/v1/soak-status",
        )
    ):
        params = dict(params or {})
        params.setdefault("engine", st.session_state.get("engine", "heuristic-v1"))
    try:
        response = requests.request(method, API + path, params=params, json=body, headers=headers, timeout=30)
        if response.status_code == 401:
            st.session_state.pop("token", None)
            st.session_state.pop("username", None)
        if response.status_code >= 400:
            try:
                message = response.json().get("detail", "요청을 처리하지 못했습니다.")
            except ValueError:
                message = "요청을 처리하지 못했습니다."
            st.error(str(message))
            return None
        return response.content if download else response.json()
    except requests.RequestException:
        st.error("문헌 API에 연결하지 못했습니다. 서버 실행 상태와 SSH 포워딩을 확인하세요.")
        return None


def render_links(links):
    for key, url in links.items():
        if url.startswith(("https://pubmed.ncbi.nlm.nih.gov/", "https://doi.org/", "https://europepmc.org/")):
            st.link_button({"pubmed": "PubMed", "doi": "DOI", "europe_pmc": "Europe PMC"}.get(key, key), url)


def render_summary(result, *, results_label="주요 결과"):
    # The engine result contains exact excerpts and structured fields, never generated prose.
    summary = result.get("summary", result) if isinstance(result, dict) else result
    if not summary:
        st.info("아직 요약이 완료되지 않았습니다.")
    elif isinstance(summary, dict):
        for key, label in [("method", "연구설계"), ("results", results_label), ("limitations", "한계")]:
            st.caption(label)
            value = summary.get(key, "미확인")
            excerpts = value if isinstance(value, list) else [value]
            if not excerpts:
                st.text(
                    "관측 결과 미확인" if key == "results" and summary.get("missing_results") else "미확인"
                )
            for excerpt in excerpts:
                if isinstance(excerpt, dict):
                    st.text(str(excerpt.get("text", excerpt.get("value", "미확인"))))
                    if "start" in excerpt and "end" in excerpt:
                        st.caption(f"원문 문자 위치 [{excerpt['start']}, {excerpt['end']})")
                else:
                    st.text(str(excerpt))
        if isinstance(result, dict) and result.get("context"):
            labels = {
                "study_design": "연구 유형",
                "participants": "대상자 수",
                "organism": "연구 대상 종",
                "tissue": "조직",
                "dose": "용량",
                "response_time": "관찰 시점",
                "comparator": "비교군",
            }
            with st.expander("추출된 연구 특성"):
                for key, label in labels.items():
                    value = result["context"].get(key, "미확인")
                    st.caption(label)
                    if isinstance(value, dict):
                        st.text(str(value.get("value", "미확인")))
                        if value.get("quote"):
                            st.code(value["quote"], language=None, wrap_lines=True)
                    else:
                        st.text(str(value))
        st.caption("규칙으로 선택한 원문 인용입니다. 통계적·인과적 해석은 검수자가 확인합니다.")
    else:
        st.text(str(summary))


def render_event(edge):
    payload = edge.get("payload", {})
    event = edge.get("effective_event", edge.get("event", payload.get("event", {})))
    if not event:
        st.code(edge.get("quote", ""), language=None, wrap_lines=True)
        return
    if event.get("role_direction") == "undirected":
        st.caption("대칭적 관계 · 표시 순서는 주체·대상이나 인과 방향을 뜻하지 않습니다.")
    st.caption(
        f"결과 항목 {event.get('outcome_type', 'unknown')} · 방향 {event.get('direction', 'unknown')} · "
        f"종 {event.get('species', 'unknown')} · 연구모델 {event.get('study_model', 'unknown')}"
    )
    st.text(
        f"부정: {'있음' if event.get('negation') else '없음'} · 추정: {'있음' if event.get('speculation') else '없음'} · "
        f"통계 표현: {event.get('significance', 'unknown')}"
    )
    conditions = event.get("conditions", {})
    with st.expander("군·비교군·시점·수치와 원문 위치"):
        st.json(conditions)
    quotes = event.get("evidence_quotes", [])
    for quote in quotes:
        st.code(quote.get("quote", quote.get("text", "")), language=None, wrap_lines=True)
        st.caption(f"문자 위치 [{quote.get('start')}, {quote.get('end')})")
    if not quotes:
        st.code(edge.get("quote", event.get("quote", "")), language=None, wrap_lines=True)
        if "start" in event and "end" in event:
            st.caption(f"문자 위치 [{event['start']}, {event['end']})")
    reasons = event.get("triage_reasons", []) + event.get("abstention_reasons", [])
    if reasons:
        st.warning("검수·보류 사유: " + ", ".join(dict.fromkeys(reasons)))
    if edge.get("review_eligibility_reasons"):
        st.caption("승인 제한: " + ", ".join(edge["review_eligibility_reasons"]))


def detail(paper_id):
    item = api(f"/api/v1/papers/{paper_id}")
    if not item:
        return
    st.subheader(item["title"])
    if item["retracted"]:
        st.error("철회 문헌입니다. 검수된 Evidence Map에서 제외됩니다.")
    st.caption(f"PMID {item['pmid']} · 출판일 {item.get('pub_date') or '미확인'} · OA {item['oa_status']}")
    render_links(item["official_links"])
    comparison_label = "v6·v7 비교" if st.session_state.get("engine") == "heuristic-v7" else ("v5·v6 비교" if st.session_state.get("engine") == "heuristic-v6" else "v1·v2 비교")
    tabs = st.tabs(["구조화 요약", "원문·출처", "유전자·성분", "관계·검수 이력", comparison_label])
    with tabs[0]:
        current = next((e for e in item["extractions"] if e["current_valid"]), None)
        if current:
            st.caption(f"엔진 {current['engine_version']} · 사전 {current['dictionary_version']}")
            render_summary(current["result"])
            if current["engine_version"] == "heuristic-v6":
                st.caption(
                    "유전자 ID와 독립적으로 추출한 관측 후보입니다. 연구 조건과 1차·2차 근거를 확인하세요."
                )
                page = api("/api/v1/observations", params={"paper_id": paper_id, "limit": 100})
                for item in (page or {}).get("items", []):
                    observation = item["observation"]
                    with st.expander(
                        observation["outcome"]["text"]
                        + " · "
                        + ("2차 근거" if observation["evidence_level"] == "secondary" else "1차 근거")
                    ):
                        st.text("중재: " + (observation.get("intervention") or {}).get("text", "미확인"))
                        st.text("비교군: " + (observation.get("comparator") or {}).get("text", "미확인"))
                        st.text(
                            "유의성: "
                            + {
                                "not_significant": "유의하지 않음",
                                "significant": "유의함",
                                "unknown": "미확인",
                            }.get(observation["significance"], "미확인")
                        )
                        st.text(
                            "결과 구분: "
                            + {
                                None: "미확인",
                                "no_effect_reported": "효과 없음으로 보고됨",
                                "non_significant": "유의하지 않음",
                                "not_increased": "증가하지 않음",
                            }.get(observation["null_interpretation"], "미확인")
                        )
                        for quote in observation["evidence_quotes"]:
                            st.text(quote["quote"])
                            st.caption(f"원문 위치 [{quote['start']}, {quote['end']})")
                        if observation["reasons"]:
                            st.caption("확인 사유: " + ", ".join(observation["reasons"]))
                        if item.get("assertion_id") and st.session_state.get("token"):
                            decision = st.selectbox(
                                "관측 검수",
                                ["보류", "승인", "수정", "기각"],
                                key="observation-decision-" + item["assertion_id"],
                            )
                            correction = None
                            comment = ""
                            if decision == "수정":
                                key = item["assertion_id"]
                                roles = {}
                                for field, label in (("intervention", "중재"), ("outcome", "결과 항목")):
                                    original = observation.get(field) or {}
                                    value = st.text_input(
                                        label + " 원문",
                                        value=original.get("text", ""),
                                        key=key + "-" + field + "-text",
                                    )
                                    start = st.number_input(
                                        label + " 시작 문자 위치",
                                        min_value=0,
                                        value=original.get("start", 0),
                                        step=1,
                                        key=key + "-" + field + "-start",
                                    )
                                    roles[field] = {
                                        **original,
                                        "text": value,
                                        "start": start,
                                        "end": start + len(value),
                                    }
                                    if value != original.get("text"):
                                        roles[field]["normalized_id"] = None
                                comment = st.text_area("수정 사유", key=key + "-reason")
                                correction = {
                                    "subject": roles["intervention"].get("normalized_id")
                                    or roles["intervention"]["text"],
                                    "predicate": "has_observation",
                                    "object": roles["outcome"]["text"],
                                    "observation": roles,
                                }
                                st.caption("저장된 원문과 문자 위치가 일치해야 수정할 수 있습니다.")
                            if decision != "보류" and st.button(
                                "검수 기록", key="observation-save-" + item["assertion_id"]
                            ):
                                body = {
                                    "assertion_id": item["assertion_id"],
                                    "decision": {"승인": "approved", "수정": "corrected", "기각": "rejected"}[
                                        decision
                                    ],
                                    "comment": comment,
                                }
                                if correction is not None:
                                    body["corrected_payload"] = correction
                                saved = api("/api/v1/reviews", "POST", body=body)
                                if saved:
                                    st.success("검수 판단을 기록했습니다.")
            if current["engine_version"] in {"heuristic-v5", "heuristic-v6"}:
                with st.expander("ID 후보·관계 가설·판단 경로"):
                    st.caption("점수는 검수 우선순위입니다. 영양 문헌 정확도 확률은 미측정입니다.")
                    for name in ("entity_hypotheses", "relation_hypotheses", "decision_trace"):
                        st.write(name)
                        st.json(current["result"].get(name, []))
        elif item.get("summary_status") == "stale":
            st.warning(
                "원문·추출 버전 또는 구문분석기 검증 조건을 확인해야 합니다. 현재 결과로 사용할 수 없습니다."
            )
            if item["extractions"]:
                st.caption("확인 사유: " + ", ".join(item["extractions"][0].get("stale_reasons", [])))
        else:
            st.info("추출 대기 중입니다.")
    with tabs[1]:
        for source in item["sources"]:
            with st.expander(f"{source['kind']} · {source['retrieved_at']}"):
                st.text(
                    f"원본 SHA-256: {source['raw_hash']}\n텍스트 SHA-256: {source['text_hash']}\n라이선스: {source.get('license') or '미확인'}"
                )
                if source["url"].startswith("https://"):
                    st.text("수집 출처: " + source["url"])
                st.code(source["text_content"], language=None, wrap_lines=True)
    with tabs[2]:
        if item["mentions"]:
            st.dataframe(
                [
                    {k: m.get(k) for k in ("kind", "text", "normalized_id", "start", "end", "status")}
                    for m in item["mentions"]
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("탐지된 후보가 없습니다.")
    with tabs[3]:
        for edge in item["assertions"]:
            with st.expander(f"{edge['subject']} → {edge['object']} · {edge['display_status']}"):
                if not edge["current_valid"]:
                    st.warning("과거 추출·검수 이력입니다. 현재 근거로 사용할 수 없습니다.")
                st.text(edge["predicate"])
                render_event(edge)
                st.caption(f"문자 위치 [{edge['start']}, {edge['end']}) · 규칙 {edge['rule_id']}")
                if edge.get("review"):
                    st.json(edge["review"])
    with tabs[4]:
        comparison_engines = (
            ("heuristic-v6", "heuristic-v7") if st.session_state.get("engine") == "heuristic-v7" else
            ("heuristic-v5", "heuristic-v6")
            if st.session_state.get("engine") == "heuristic-v6"
            else ("heuristic-v1", "heuristic-v2")
        )
        for column, selected_engine in zip(st.columns(2), comparison_engines):
            with column:
                st.subheader(selected_engine)
                compared = (
                    item
                    if item.get("engine") == selected_engine
                    else api(f"/api/v1/papers/{paper_id}", params={"engine": selected_engine})
                )
                if compared:
                    current = next((e for e in compared["extractions"] if e["current_valid"]), None)
                    render_summary(current["result"] if current else None)
                    st.caption(f"관계 후보·이력 {len(compared['assertions'])}건")
                    for edge in compared["assertions"]:
                        with st.expander(f"{edge['subject']} → {edge['object']} · {edge['display_status']}"):
                            render_event(edge)
        st.caption(
            "동일 원문을 대상으로 엔진별 후보를 비교합니다. 한 엔진의 승인은 다른 엔진에 이전되지 않습니다."
        )


st.title("문헌 근거 검수")
st.caption(
    "후보 검수용 베타 · 한국어 구조화 요약과 원문 인용 · 영양 문헌 정확도와 검수 시간은 별도 검증 전입니다."
)
with st.sidebar:
    engine_info = api("/api/v1/engines")
    engine_choices = (
        [item["engine"] for item in engine_info["items"]] if engine_info else ["heuristic-v1", "heuristic-v2"]
    )
    if st.session_state.get("engine") not in engine_choices:
        st.session_state["engine"] = (engine_info or {}).get("default", "heuristic-v1")
    st.selectbox("추출 엔진", engine_choices, key="engine")
    st.caption("운영 기본: " + (engine_info or {}).get("default", "heuristic-v1"))
    st.link_button("개인 ChatGPT 연결·성능 비교", "http://127.0.0.1:8521")
    st.caption("사용자 PC의 개인 연결 화면입니다. 검수자 계정과 별도로 로그인합니다.")
    st.subheader("검수자 계정")
    if st.session_state.get("token"):
        st.text(st.session_state.get("username", "로그인됨"))
        if st.button("로그아웃", width="stretch"):
            api("/api/v1/auth/logout", "POST")
            st.session_state.pop("token", None)
            st.session_state.pop("username", None)
            st.rerun()
    else:
        with st.form("login"):
            username = st.text_input("계정")
            password = st.text_input("비밀번호", type="password")
            submitted = st.form_submit_button("로그인", width="stretch")
        if submitted:
            session = api("/api/v1/auth/login", "POST", body={"username": username, "password": password})
            if session:
                st.session_state["token"] = session["access_token"]
                st.session_state["username"] = session["username"]
                st.rerun()
        st.caption("계정은 서버 CLI에서 개인별로 생성합니다.")
    st.divider()
    view = st.radio(
        "작업",
        ["문헌 검색", "논문별 요약", "Evidence Map", "검수 대기열", "실행 상태·리포트", "알고리즘 개선"],
    )
    topics = api("/api/v1/topics")
    choices = {"전체 주제": None}
    if topics:
        choices.update({t["name"]: t["topic_id"] for t in topics["items"] if t["enabled"]})
    topic_label = st.selectbox("주제", list(choices))
    topic = choices[topic_label]
    st.caption("2025년 이후 문헌 · 초록 및 이용 가능한 공개 XML")

if view == "문헌 검색":
    query = st.text_input("제목·초록 검색", placeholder="gene expression, hypertension, omega-3…")
    page = st.number_input("페이지", min_value=1, value=1, step=1)
    listing = api(
        "/api/v1/papers", params={"q": query, "topic": topic, "limit": 20, "offset": (page - 1) * 20}
    )
    if listing:
        st.caption(f"고유 문헌 {listing['total']:,}편 · 현재 {len(listing['items'])}편")
        for paper in listing["items"]:
            with st.expander(f"{paper['title']} · PMID {paper['pmid']}"):
                st.text(f"출판일 {paper.get('pub_date') or '미확인'} · OA {paper['oa_status']}")
                if paper["retracted"]:
                    st.error("철회 문헌")
                if paper.get("summary_status") == "stale":
                    st.warning("원문·추출 버전 또는 구문분석기 검증 조건을 확인해야 합니다.")
                    st.caption("확인 사유: " + ", ".join(paper.get("summary_stale_reasons", [])))
                else:
                    render_summary(paper.get("summary"))
                st.caption(f"요약 상세에서 사용할 문헌 ID: {paper['paper_id']}")
                render_links(paper["official_links"])
elif view == "논문별 요약":
    paper_id = st.number_input("문헌 ID", min_value=1, value=1, step=1)
    if st.button("문헌 열기", type="primary"):
        st.session_state["detail_id"] = paper_id
    if st.session_state.get("detail_id"):
        detail(st.session_state["detail_id"])
elif view == "Evidence Map":
    filters = st.columns(2)
    with filters[0]:
        statuses = {
            "연구자 확인 근거": "approved",
            "자동 후보": "candidate",
            "재검토": "needs_review",
            "전체 현재 관계": "all",
        }
        status = statuses[st.selectbox("근거 상태", list(statuses))]
    with filters[1]:
        gene = st.text_input("유전자 필터", placeholder="예: NOS3 또는 HGNC ID")
    mapping = api(
        "/api/v1/evidence-map", params={"topic": topic, "gene": gene or None, "status": status, "limit": 200}
    )
    st.info("각 연결은 한 문헌의 원문 근거입니다. 여러 연결을 합쳐 식이 인과 효과로 해석하지 않습니다.")
    if mapping and mapping["edges"]:
        nodes = mapping["nodes"]
        positions = {
            n["id"]: (math.cos(2 * math.pi * i / len(nodes)), math.sin(2 * math.pi * i / len(nodes)))
            for i, n in enumerate(nodes)
        }
        figure = go.Figure()
        for reviewed, label, color in [(False, "자동 후보", "#d09b3c"), (True, "연구자 확인", "#26756a")]:
            x, y = [], []
            for edge in mapping["edges"]:
                if edge["is_reviewed"] != reviewed:
                    continue
                start, end = positions[edge["subject"]], positions[edge["object"]]
                x.extend([start[0], end[0], None])
                y.extend([start[1], end[1], None])
            if x:
                figure.add_trace(
                    go.Scatter(
                        x=x,
                        y=y,
                        mode="lines",
                        name=label,
                        line={"color": color, "width": 2, "dash": "solid" if reviewed else "dot"},
                        hoverinfo="skip",
                    )
                )
        figure.add_trace(
            go.Scatter(
                x=[positions[n["id"]][0] for n in nodes],
                y=[positions[n["id"]][1] for n in nodes],
                mode="markers+text",
                name="요소",
                text=[html.escape(n["label"]) for n in nodes],
                textposition="top center",
                hoverinfo="text",
                marker={"color": "#253d59", "size": 12},
            )
        )
        figure.update_layout(
            height=520,
            margin={"t": 35, "b": 35, "l": 50, "r": 50},
            xaxis={"visible": False},
            yaxis={"visible": False},
            plot_bgcolor="#f8fafb",
        )
        st.plotly_chart(figure, width="stretch")
        st.caption(
            f"관계 {mapping['total']:,}건 중 {len(mapping['edges'])}건 표시 · 문헌 수와 관계 수는 다릅니다."
        )
        st.dataframe(
            [
                {
                    k: edge[k]
                    for k in ("paper_id", "pmid", "subject", "predicate", "object", "status", "quote")
                }
                for edge in mapping["edges"]
            ],
            hide_index=True,
            width="stretch",
        )
    elif mapping:
        st.info("선택한 조건의 현재 관계가 없습니다.")
elif view == "검수 대기열":
    if not st.session_state.get("token"):
        st.warning("문헌과 후보는 열람할 수 있습니다. 승인·수정·기각을 기록하려면 로그인하세요.")
    statuses = {"자동 후보": "candidate", "재검토": "needs_review", "승인 이력": "approved"}
    if st.session_state.get("engine") in {"heuristic-v2", "heuristic-v4", "heuristic-v5", "heuristic-v6"}:
        with st.expander("추출 보류·배경 이벤트"):
            event_statuses = {"추출 보류": "abstained", "배경·선행연구 인용": "background"}
            event_status = event_statuses[st.selectbox("미승인 이벤트 유형", list(event_statuses))]
            reason_filter = st.text_input(
                "보류 사유 필터", placeholder="예: parser_unavailable, ambiguous_roles"
            )
            abstention_page = st.number_input("보류 페이지", min_value=1, value=1, step=1)
            abstentions = api(
                "/api/v1/events",
                params={
                    "topic": topic,
                    "status": event_status,
                    "reason": reason_filter or None,
                    "limit": 10,
                    "offset": (abstention_page - 1) * 10,
                },
            )
            if abstentions:
                st.caption(
                    f"{'보류' if event_status == 'abstained' else '배경'} {abstentions['total']:,}건 · 이 이벤트는 승인할 수 없습니다."
                )
                for event in abstentions["items"]:
                    st.text(f"PMID {event['pmid']} · {event['title']}")
                    render_event(event)
    status = statuses[st.selectbox("대기열 상태", list(statuses))]
    page = st.number_input("검수 페이지", min_value=1, value=1, step=1)
    queue = api(
        "/api/v1/evidence-map",
        params={"topic": topic, "status": status, "limit": 20, "offset": (page - 1) * 20},
    )
    if queue:
        st.caption(f"관계 {queue['total']:,}건")
        for edge in queue["edges"]:
            with st.expander(f"PMID {edge['pmid']} · {edge['subject']} → {edge['object']}"):
                st.text(edge["title"])
                render_event(edge)
                st.caption(
                    f"위치 [{edge['start']}, {edge['end']}) · 규칙 {edge['rule_id']} · {edge['engine_version']}"
                )
                st.text(f"텍스트 SHA-256: {edge['text_hash']}")
                st.json(edge["payload"], expanded=False)
                render_links(edge["official_links"])
                with st.form("review_" + edge["assertion_id"]):
                    decisions = {"승인": "approved", "기각": "rejected", "관계 수정 후 승인": "corrected"}
                    decision = st.selectbox("판단", list(decisions))
                    subject = st.text_input("주어", value=edge["subject"])
                    predicate = st.text_input("관계", value=edge["predicate"])
                    target = st.text_input("목적어", value=edge["object"])
                    semantic = {}
                    conditions_text = None
                    event = edge.get("effective_event", edge["payload"].get("event", {}))
                    if event:
                        options = {
                            "outcome_type": (
                                "결과 항목",
                                [
                                    "expression",
                                    "activity",
                                    "methylation",
                                    "concentration",
                                    "health_outcome",
                                    "unknown",
                                ],
                            ),
                            "direction": (
                                "결과 방향",
                                ["increase", "decrease", "association", "change", "no_effect"],
                            ),
                            "significance": ("통계 표현", ["significant", "not_significant", "unknown"]),
                            "species": ("생물종", ["human", "animal", "mixed", "unknown"]),
                            "study_model": ("연구모델", ["clinical", "cell", "animal", "mixed", "unknown"]),
                        }
                        for key, (label, values) in options.items():
                            value = event.get(key)
                            semantic[key] = st.selectbox(
                                label,
                                values,
                                index=values.index(value) if value in values else len(values) - 1,
                            )
                        semantic["negation"] = st.checkbox("부정 표현", value=bool(event.get("negation")))
                        semantic["speculation"] = st.checkbox(
                            "추정 표현", value=bool(event.get("speculation"))
                        )
                        if st.session_state.get("engine") == "heuristic-v5" and event.get(
                            "benchmark_relation_type"
                        ):
                            types = [
                                "Association",
                                "Positive_Correlation",
                                "Negative_Correlation",
                                "Bind",
                                "Comparison",
                                "Cotreatment",
                                "Conversion",
                                "Drug_Interaction",
                            ]
                            semantic["benchmark_relation_type"] = st.selectbox(
                                "벤치마크 관계 유형",
                                types,
                                index=types.index(event["benchmark_relation_type"]),
                            )
                            roles = ["chemical_to_gene", "gene_to_chemical"]
                            semantic["role_direction"] = st.selectbox(
                                "관계 역할",
                                roles,
                                index=roles.index(event.get("role_direction", "chemical_to_gene")),
                            )
                        conditions_text = st.text_area(
                            "연구 조건 수정 (JSON · 원문 위치를 유지해야 합니다)",
                            value=json.dumps(event.get("conditions", {}), ensure_ascii=False, indent=2),
                        )
                    comment = st.text_area("검수 메모 · 수정 시 사유 필수")
                    submitted = st.form_submit_button(
                        "판단 저장", disabled=not bool(st.session_state.get("token"))
                    )
                if submitted:
                    body = {
                        "assertion_id": edge["assertion_id"],
                        "decision": decisions[decision],
                        "comment": comment,
                    }
                    if body["decision"] == "corrected":
                        if conditions_text is not None:
                            try:
                                semantic["conditions"] = json.loads(conditions_text)
                            except ValueError:
                                st.error("연구 조건 JSON 형식을 확인하세요.")
                                st.stop()
                        body["corrected_payload"] = {
                            "subject": subject,
                            "predicate": predicate,
                            "object": target,
                            **semantic,
                        }
                    result = api("/api/v1/reviews", "POST", body=body)
                    if result:
                        st.success("개인 계정과 원문 버전에 연결해 판단을 저장했습니다.")
                        st.rerun()
elif view == "알고리즘 개선":
    st.subheader("heuristic-v7 관계·ID·원문 근거 학습")
    v7=api("/api/v1/experiments",params={"engine":"heuristic-v7"})
    st.caption("관계 우선 개발 단계입니다. 임상 관측은 v6를 유지하며, 목표 충족 전 기본 엔진은 v1입니다.")
    if v7:
        st.json(v7.get("training_states",{}))
        for report in v7.get("items",[]):
            with st.expander(f"{report.get('id')} · {report.get('mode')} · {report.get('papers')}편"):
                st.dataframe([{"항목":k,**v} for k,v in (report.get("metrics") or {}).items()],hide_index=True)
    st.subheader("heuristic-v6 후보·ID·관측 개선")
    v6 = api("/api/v1/experiments", params={"engine": "heuristic-v6"})
    st.caption("자동 선택과 검수 후보를 구분합니다. 영양학 정확도와 검수 시간 절감은 미측정입니다.")
    for report in (v6 or {}).get("items", []):
        with st.expander(f"{report['corpus']} · {report['mode']} · {report['papers']}편"):
            st.dataframe(
                [{"평가 항목": name, **metric} for name, metric in report["metrics"].items()], hide_index=True
            )
            st.json(
                {
                    "후보 재현율": report.get("candidate_coverage"),
                    "ID 검색": report.get("id_search_coverage"),
                    "실패 단계": report.get("failure_counts"),
                }
            )
    st.subheader("heuristic-v5 관계·ID 개선")
    v5 = api("/api/v1/experiments", params={"engine": "heuristic-v5"})
    st.caption("관계 유형·역할 방향·검증된 이벤트를 각각 측정합니다. 목표 미달 시 기본 엔진 v1을 유지합니다.")
    if v5 and v5.get("items"):
        for report in v5["items"]:
            with st.expander(
                f"v5 · {report['corpus']} · {report['provider']} · {report['mode']} · {report['ablation']} · {report['source_level']} · {report['papers']}편"
            ):
                st.dataframe(
                    [
                        {
                            "항목": k,
                            "정밀도": m.get("precision"),
                            "재현율": m.get("recall"),
                            "F1": m.get("f1"),
                            "TP": m.get("tp"),
                            "FP": m.get("fp"),
                            "FN": m.get("fn"),
                        }
                        for k, m in report["metrics"].items()
                    ],
                    hide_index=True,
                    width="stretch",
                )
                st.json(
                    {
                        "유형·ID 최초 실패": report.get("failure_counts"),
                        "역할 실패": report.get("direction_failure_counts"),
                        "오탐 사유": report.get("false_positive_counts"),
                        "ID 미확정 후보": report.get("candidate_id_unresolved"),
                        "전체 후보": report.get("candidate_pairs"),
                        "의미 검증": report.get("semantic_verified"),
                    }
                )
                st.caption(report.get("relation_evaluation_scope", ""))
        st.json(v5.get("selection"))
        st.download_button(
            "v5 평가 JSON 내려받기",
            json.dumps(v5, ensure_ascii=False, indent=2),
            file_name="heuristic-v5-development.json",
            mime="application/json",
        )
    else:
        st.info("v5 학습·개발 평가를 준비 중입니다.")
    st.subheader("heuristic-v4 CPU 모델·자체 이벤트 규칙")
    v4 = api("/api/v1/experiments", params={"engine": "heuristic-v4"})
    if v4 and v4.get("items"):
        for report in v4["items"]:
            with st.expander(
                f"v4 · {report['corpus']} · {report['provider']} · {report['mode']} · {report['profile']} · {report['papers']}편 · {report['id'][:8]}"
            ):
                st.caption(f"실행: {report.get('at', '미확인')} · 평가 ID: {report['id']}")
                st.caption(
                    "평가 범위: "
                    + report.get(
                        "relation_evaluation_scope",
                        "이전 v4 개발 평가 정의; 실행 JSON에서 범위를 확인하세요.",
                    )
                )
                if report.get("mode") == "gold_entities":
                    st.info("정답 개체 제공 실험입니다. 유전자 탐지 100%는 자동 탐지 성능을 뜻하지 않습니다.")
                st.dataframe(
                    [
                        {
                            "항목": k,
                            "정밀도": v.get("precision"),
                            "재현율": v.get("recall"),
                            "F1": v.get("f1"),
                        }
                        for k, v in report["metrics"].items()
                    ],
                    hide_index=True,
                    width="stretch",
                )
                st.json(
                    {
                        "최초 실패 단계": report.get("failure_counts"),
                        "유전자 ID 연결": report.get("gene_identity_reasons"),
                    }
                )
                st.caption("공개 개발 평가입니다. 영양 문헌 정확도와 검수 시간 절감은 미측정입니다.")
        st.download_button(
            "v4 평가 JSON 내려받기",
            json.dumps(v4, ensure_ascii=False, indent=2),
            file_name="heuristic-v4-development.json",
            mime="application/json",
        )
    else:
        st.info("v4 평가를 준비 중입니다. 기본 엔진은 검증 기준 충족 전까지 유지합니다.")
    st.subheader("heuristic-v3 개발 비교")
    st.info(
        "동일 개발 논문에서 측정한 탐색 결과입니다. 최종 평가 성능·영양 문헌 정확도·검수 시간 절감은 미측정입니다."
    )
    experiment = api("/api/v1/experiments")
    if experiment:
        st.caption("기본 엔진은 v1입니다. 개선 실험은 자동 승인이나 기본 엔진 전환에 사용하지 않습니다.")
        if not experiment["reports"]:
            st.info("개발 비교를 준비 중입니다. 서버의 실험 결과가 저장되면 이 화면에 표시됩니다.")
        else:
            st.caption(
                f"저장된 실험 {len(experiment['reports'])}건 · {experiment.get('at', '')} · 논문 bootstrap 2,000회"
            )
            for report in experiment["reports"]:
                with st.expander(
                    f"{report['corpus']} · {report['mode']} · 개발 {report['papers']}편", expanded=True
                ):
                    rows = []
                    for endpoint, candidate in report["metrics"]["heuristic-v3"].items():
                        if endpoint.startswith("scopes_exact") or endpoint in {"scopes", "annotated_spans"}:
                            continue
                        baseline = report["metrics"]["heuristic-v2"][endpoint]
                        row = {"항목": endpoint}
                        for name, values in [("v2", baseline), ("v3", candidate)]:
                            for key, label in [("precision", "정밀도"), ("recall", "재현율"), ("f1", "F1")]:
                                number = values[key]
                                row[f"{name} {label}"] = (
                                    "미정의" if number is None else f"{number * 100:.2f}%"
                                )
                        rows.append(row)
                    st.dataframe(rows, hide_index=True, width="stretch")
                    st.caption(
                        f"전체 개발 자료 {report['available_development_papers']}편 중 {report['papers']}편 · 원문 위치 검사: {'통과' if report['provenance_valid'] else '실패'}"
                    )
                    with st.expander("성능 차이의 95% 구간·사전 출처"):
                        st.json(
                            {
                                "comparisons": report["comparisons"],
                                "chemical_lexicon": report["chemical_lexicon"],
                            }
                        )
            st.download_button(
                "개발 비교 JSON 내려받기",
                json.dumps(experiment, ensure_ascii=False, indent=2),
                file_name="heuristic-v3-development.json",
                mime="application/json",
            )
    previews = api("/api/v1/experiments/nutrition")
    st.subheader("영양 문헌의 v3 원문 미리보기")
    st.caption("개발 문헌만 사용합니다. 자동 후보이며 전문가 검수·정확도 평가는 아직 수행하지 않았습니다.")
    if previews and previews["items"]:
        selected = st.selectbox(
            "미리보기 문헌",
            previews["items"],
            format_func=lambda item: f"{item['topic']} · PMID {item['pmid']} · {item['title']}",
        )
        if st.button("v3 요약·원문 후보 열기"):
            case = api("/api/v1/experiments/nutrition/" + selected["id"])
            if case:
                st.caption(
                    "결과 슬롯에는 연구 계획·추정 표현이 포함될 수 있습니다. 실제 관측 결과인지는 원문에서 확인하세요."
                )
                render_summary(case["result"], results_label="결과 문장 후보")
                st.caption(f"원문 SHA-256: {case['source_hash']} · {case['source_level']} · 미검수")
                candidates = [e for e in case["result"]["events"] if e["status"] == "candidate"]
                st.caption(
                    f"관계 이벤트 후보 {len(candidates)}건 · 보류 {len(case['result']['abstentions'])}건"
                )
                for event in candidates[:10]:
                    with st.expander(f"{event['subject']} → {event['object']} · {event['direction']}"):
                        render_event({"event": event})
                with st.expander("연구 조건·PICO 구절 후보"):
                    st.json(case["result"]["annotated_spans"], expanded=False)
                with st.expander("보류 사유"):
                    st.json(case["result"]["abstentions"][:20], expanded=False)
elif view == "실행 상태·리포트":
    soak = api("/api/v1/soak-status")
    if soak:
        st.caption(
            f"{soak.get('engine', st.session_state.get('engine'))} 7일 운영 관측: {soak['status']} · 관측 {soak['samples']}회 · 실제 경과 {soak.get('elapsed_hours', 0)}시간"
        )
    runs = api("/api/v1/runs")
    if runs:
        cols = st.columns(4)
        for col, key, label in zip(
            cols,
            ["papers", "summarized_papers", "oa_papers", "retracted_papers"],
            ["고유 문헌", "요약 문헌", "공개 본문 문헌", "철회 문헌"],
        ):
            col.metric(label, f"{runs['counts'][key]:,}편")
        st.subheader("실행 기록")
        if runs.get("engine_counts"):
            st.dataframe(
                [{"엔진": name, **counts} for name, counts in runs["engine_counts"].items()],
                hide_index=True,
                width="stretch",
            )
            st.caption("엔진별 요약·관계·보류 수를 구분합니다. 후보 수는 정확도 측정값이 아닙니다.")
        st.dataframe(runs["items"], hide_index=True, width="stretch")
        with st.expander("예약 작업·재처리 대기열"):
            st.json(
                {"jobs": runs["jobs"], "queue": runs["queue"], "engine_queue": runs.get("engine_queue", [])}
            )
    reports = api("/api/v1/reports", params={"include_snapshot": False})
    st.subheader("주간 리포트")
    if reports:
        for report in reports["items"]:
            report_engine = report.get("snapshot", {}).get("engine_version") or "heuristic-v1"
            with st.expander(
                f"{report_engine} · {report['period_start']} ~ {report['period_end']} · {report['generated_at']}"
            ):
                st.text("스냅샷 SHA-256: " + report["snapshot_hash"])
                st.json(report["snapshot"], expanded=False)
                for name, path in report["downloads"].items():
                    if st.button(f"{name.upper()} 파일 준비", key=report["report_id"] + name):
                        content = api(path, download=True)
                        if content is not None:
                            suffix = {"html": "html", "markdown": "md", "csv": "csv"}[name]
                            st.download_button(
                                f"{name.upper()} 내려받기",
                                content,
                                file_name=f"report-{report['report_id']}.{suffix}",
                                key="download_" + report["report_id"] + name,
                            )
