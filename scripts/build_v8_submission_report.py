"""Build a Korean, hash-bound progress report without training or opening gold labels."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import html
import io
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = {"v2": (64, 64, 90), "v3": (63, 57, 91),
              "v4": (50, 54, 104), "v5-r1": (65, 63, 89)}
TARGET = {"tp_minimum": 65, "recall_minimum": 65 / 154, "precision_minimum": .85,
          "classification": "internal_development_target", "agreement_target_verified": False}
NEW_ARMS = {"r2": "v6_gpu_r2", "v8": "nutriomics-evidence-v8"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load_receipt(path, role, sources):
    path = Path(path)
    if any("sealed" in part.lower() for part in path.parts) or re.search(r"(^|[-_.])gold([-_.]|$)", path.name, re.I):
        raise ValueError("봉인 또는 정답 라벨 파일은 보고서 입력으로 열 수 없습니다")
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("보고서 입력은 JSON 객체여야 합니다")
    sources.append({"role": role, "file": path.name, "sha256": sha(raw)})
    return data, sha(raw)


def metrics(value, denominator=None):
    counts = {key: value.get(key) for key in ("tp", "fp", "fn")}
    if any(type(v) is not int or v < 0 for v in counts.values()):
        raise ValueError("TP/FP/FN은 음수가 아닌 정수여야 합니다")
    tp, fp, fn = (counts[key] for key in ("tp", "fp", "fn"))
    if denominator is not None and tp + fn != denominator:
        raise ValueError("정답 관계 분모가 다릅니다")
    result = {**counts, "precision": tp / (tp + fp) if tp + fp else None,
              "recall": tp / (tp + fn) if tp + fn else None,
              "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}
    for key in ("precision", "recall", "f1"):
        if key in value:
            v, expected = value[key], result[key]
            if v is None and expected is None:
                continue
            if (not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
                    or expected is None or abs(v - expected) > 1e-8):
                raise ValueError("저장 성능과 TP/FP/FN 재계산이 일치하지 않습니다")
    return result


def paper_scores(row, metric="relation", expected=100):
    values = row.get("per_paper")
    if isinstance(values, list):
        if any(not isinstance(v, dict) or not v.get("pmid") for v in values):
            raise ValueError("논문별 평가 식별자가 없습니다")
        mapped = {str(v["pmid"]): v for v in values}
        if len(mapped) != len(values):
            raise ValueError("논문별 평가 식별자가 중복됐습니다")
        values = mapped
    if not isinstance(values, dict) or len(values) != expected:
        raise ValueError("논문별 평가 건수를 확인할 수 없습니다")
    totals = {k: 0 for k in ("tp", "fp", "fn")}
    for value in values.values():
        m = metrics(value[metric])
        for key in totals:
            totals[key] += m[key]
    return set(values), totals


def target_passed(m):
    return m["tp"] >= 65 and m["recall"] >= 65 / 154 and m["precision"] is not None and m["precision"] >= .85


def historical_rows(paths, sources):
    rows = []
    cohort = None
    gold_hash = None
    for label, expected in HISTORICAL.items():
        data, digest = load_receipt(paths[label], "historical_" + label, sources)
        if data.get("papers") != 100 or data.get("gold_relations") != 154:
            raise ValueError("과거 비교는 같은 100편·154건이어야 합니다")
        if not re.fullmatch("[a-f0-9]{64}", str(data.get("gold_sha256", ""))):
            raise ValueError("평가 정답의 저장 해시가 없습니다")
        if gold_hash is not None and gold_hash != data["gold_sha256"]:
            raise ValueError("과거 비교의 정답 해시가 다릅니다")
        gold_hash = data["gold_sha256"]
        arm = data["arms"]["combined"]
        m = metrics(arm["metrics"], 154)
        if tuple(m[k] for k in ("tp", "fp", "fn")) != expected:
            raise ValueError("확인한 과거 기준선과 저장 원본이 다릅니다: " + label)
        paper_ids, totals = paper_scores(arm)
        if totals != {k: m[k] for k in totals}:
            raise ValueError("논문별 합계와 전체 평가가 다릅니다")
        if cohort is not None and cohort != paper_ids:
            raise ValueError("과거 비교의 논문 집합이 다릅니다")
        cohort = paper_ids
        rows.append({"model": label, "status": "완료·반복 개발 비교", "metrics": m,
                     "papers": 100, "gold_relations": 154, "predictions": m["tp"] + m["fp"],
                     "target_passed": target_passed(m), "independent_final_performance": False,
                     "source_sha256": digest, "source_arm": "combined"})
    return rows, gold_hash, cohort


def new_evaluation(label, path, sources, gold_hash, cohort):
    if path is None or not Path(path).is_file():
        return {"model": label, "status": "미평가", "metrics": None, "target_passed": None,
                "papers": None, "gold_relations": None, "predictions": None,
                "reason": "저장된 evaluation.json이 없어 0점으로 계산하지 않음"}
    data, digest = load_receipt(path, "evaluation_" + label, sources)
    if (data.get("papers") != 100 or data.get("gold_relations") != 154
            or data.get("gold_sha256") != gold_hash):
        raise ValueError("새 평가의 100편·154건·정답 해시가 기준선과 다릅니다")
    arm = data.get("scores", {}).get(NEW_ARMS[label])
    if not arm:
        raise ValueError("새 평가에 요청한 엔진 결과가 없습니다")
    m = metrics(arm["metrics"], 154)
    ids, totals = paper_scores(arm)
    if ids != cohort or totals != {k: m[k] for k in totals}:
        raise ValueError("새 평가의 논문 집합 또는 논문별 합계가 다릅니다")
    return {"model": label, "status": "완료·반복 개발 비교", "metrics": m,
            "papers": 100, "gold_relations": 154, "predictions": m["tp"] + m["fp"],
            "target_passed": target_passed(m), "independent_final_performance": False,
            "source_sha256": digest, "source_arm": NEW_ARMS[label]}


def count(value):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("건수는 음수가 아닌 정수여야 합니다")
    return value


def collection_rows(progress):
    c = progress.get("collection", {})
    fixed = c.get('frozen_search_inventory')
    if fixed is not None:
        if (not isinstance(fixed, dict) or fixed.get('search_complete') is not True
                or not re.fullmatch('[a-f0-9]{64}', str(fixed.get('manifest_sha256', '')))
                or fixed.get('expected_papers') != fixed.get('selected_unique')
                or any(type(fixed.get(k)) is not int or fixed[k] < 0 for k in
                       ('expected_papers', 'selected_unique', 'stored_selected', 'missing_count'))
                or fixed['stored_selected'] + fixed['missing_count'] != fixed['selected_unique']
                or (c.get('contract_sha256') is not None and fixed.get('query_version') != c['contract_sha256'])):
            raise ValueError('고정 검색 목록과 현재 DB 집계의 계약이 다릅니다')
    inventory = fixed if fixed is not None else c
    rows = []
    def add(key, label, value, unit, scope, state="관측", denominator=None):
        rows.append({"key": key, "label": label, "value": count(value), "unit": unit,
                     "scope": scope, "status": state if value is not None else "미확인",
                     "denominator": count(denominator)})
    add("search_expected", "고정 검색 결과", inventory.get("expected_papers"), "편", "고정 검색 목록" if fixed is not None else "검색 목록")
    add("search_enumerated", "중복 제거 검색 PMID 목록", inventory.get("selected_unique"), "편", "고정 검색 목록" if fixed is not None else "검색 목록")
    add("selected_stored", "검색 목록 중 DB 저장", inventory.get("stored_selected"), "편", "고정 목록·최신 DB 읽기" if fixed is not None else "수집 상태 시점", denominator=inventory.get("selected_unique"))
    add("stored_live", "전체 DB 고유 문헌", c.get("live_stored_unique_papers"), "편", "최신 DB 조회")
    add("abstract_live", "최신 DB 초록 확보", c.get("live_abstract_papers"), "편", "최신 DB 조회", denominator=c.get("live_stored_unique_papers"))
    add("fulltext_live", "최신 DB 공개 본문 확보", c.get("live_oa_available_papers"), "편", "최신 DB 조회", denominator=c.get("live_stored_unique_papers"))
    live_metadata = c.get('live_ingest_metadata_status')
    metadata = live_metadata if live_metadata is not None else c.get("metadata_status", {})
    metadata_scope = '수집 전용 장부 별도 읽기' if live_metadata is not None else '수집 작업'
    if not isinstance(metadata, dict):
        raise ValueError('수집 전용 장부의 상태별 건수가 잘못됐습니다')
    add("metadata_completed", "새 수집 작업 메타데이터 처리 완료", metadata.get("completed"), "편", metadata_scope, denominator=inventory.get("expected_papers"))
    add("metadata_pending", "메타데이터 대기", metadata.get("pending"), "편", metadata_scope)
    add("metadata_failed", "메타데이터 수집 실패", metadata.get('failed', 0) if live_metadata is not None else c.get("failed"), "편", metadata_scope)
    # A collector's processed counter describes metadata work, not extraction success.
    add("extraction_success", "현재 원문 일치 추출 문헌·전체 엔진 합집합", c.get("extraction_success_papers"), "편",
        "최신 DB·현재 원문 ID·해시 일치·최소 1개 엔진·v8 성능 아님", denominator=c.get("live_stored_unique_papers"))
    add("extraction_failed", "추출 처리 실패 문헌", c.get("extraction_failed_papers"), "편", "추출 엔진")
    engines = c.get('live_current_extraction_papers_by_engine')
    if engines is not None:
        if not isinstance(engines, dict) or any(not isinstance(e, str) or not e for e in engines):
            raise ValueError("엔진별 현재 원문 일치 문헌 집계가 잘못됐습니다")
        for engine, value in sorted(engines.items()):
            if count(value) is None:
                raise ValueError("엔진별 문헌 집계에 건수가 없습니다")
            if (c.get('extraction_success_papers') is not None
                    and count(value) > count(c['extraction_success_papers'])):
                raise ValueError("엔진별 문헌 수가 전체 엔진 합집합보다 큽니다")
            add('extraction_engine:' + engine, '현재 원문 일치 추출 문헌 · ' + engine, value, '편',
                '최신 DB·엔진별 고유 문헌·다른 엔진과 중복 가능', denominator=c.get('live_stored_unique_papers'))
    queue = c.get('live_queue_counts')
    if queue is not None:
        if not isinstance(queue, list):
            raise ValueError("대기열은 단계·상태별 작업 행 목록이어야 합니다")
        seen = set()
        for item in queue:
            if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k] for k in ('stage', 'status')):
                raise ValueError("대기열 단계와 상태가 없습니다")
            pair = (item['stage'], item['status'])
            if count(item.get('queue_items')) is None:
                raise ValueError("대기열 집계에 작업 행수가 없습니다")
            if pair in seen:
                raise ValueError("대기열 단계·상태 집계가 중복됐습니다")
            seen.add(pair)
            add('queue:' + item['stage'] + ':' + item['status'], '대기열 작업 · ' + item['stage'] + ' · ' + item['status'],
                item['queue_items'], '행', 'queue 작업 행수·고유 문헌 수나 실패 문헌 수가 아님')
    internal, public = c.get("internal_snapshot", {}), c.get("public_snapshot", {})
    for key, label, field, unit in [
        ("snapshot_papers", "고정 snapshot 고유 문헌", "unique_papers", "편"),
        ("snapshot_abstracts", "고정 snapshot 초록 확보", "papers_with_abstract", "편"),
        ("snapshot_fulltext", "고정 snapshot 본문 확보", "fulltext_papers", "편"),
        ("candidate_papers", "후보를 보유한 문헌", "papers_with_visible_events", "편"),
        ("candidate_events", "전체 엔진 후보 이벤트", "candidate_events_all_engines", "건"),
        ("human_approved", "사람 승인 관계", "human_approved_visible_events", "건"),
        ("review_records", "검수 판단 기록", "review_records", "건"),
        ("retracted_papers", "철회 문헌", "retracted_papers", "편"),
        ("stored_reports", "DB 등록 리포트", "stored_reports", "건")]:
        add(key, label, internal.get(field), unit, "고정 내부 snapshot")
    add("public_visible_events", "공개 허용 후보 이벤트", public.get("visible_events"), "건", "고정 공개 snapshot")
    add("public_withheld_events", "공개 제한 후보 이벤트", public.get("withheld_events"), "건", "고정 공개 snapshot")
    complete = (c.get("metadata_complete") is True and c.get("expected_papers") is not None
                and c.get("expected_papers") == c.get("selected_unique") == c.get("stored_selected")
                and c.get("missing_memberships") == 0)
    return rows, {"status": "완료" if complete else "진행·전수 수집 미완료",
                  "collection_complete": complete, "state_observed_at": c.get("updated_at"),
                  "frozen_search_manifest_sha256": fixed.get('manifest_sha256') if fixed else None,
                  "snapshot_time_not_equated_to_live_db": True}


def pipeline_states(progress):
    result = []
    def state(value, *, predictions, alive):
        # A finished process is expected to disappear. Persisted completion
        # evidence therefore outranks process liveness, without implying that
        # predictions passed evaluation or any deployment gate.
        if predictions is True:
            return "100편 예측 저장 완료·평가 별도"
        status = value.get('status')
        if status == 'clinical_training_complete':
            return "임상 모델 학습 완료·평가 별도"
        if status == 'native_training_complete':
            return "보조 관계·이벤트 학습 완료·100편 평가와 별도"
        if status in {'completed', 'complete', 'finished', 'frozen'}:
            return "작업 완료·성능 검증 별도"
        if status in {'failed', 'error'}:
            return "실패 상태 기록·원인 확인 필요"
        if status in {'resource_budget_exceeded','budget_exceeded'}:
            return "자원 한도 초과로 중단·체크포인트 보존"
        if status in {'cancelled', 'interrupted', 'stopped'}:
            return "중단 상태 기록"
        if isinstance(status, str) and status.startswith('waiting'):
            return "대기"
        return "진행" if alive is True else "현재 상태 미확인"
    r2 = progress.get("r2", {})
    r2_details = []
    if 'completed_relation_outer_folds' in r2:
        r2_details.append('관계 외부 fold ' + str(count(r2['completed_relation_outer_folds'])) + '개 완료')
    identity = r2.get('id_training')
    if isinstance(identity, dict):
        for kind, label in [('gene','유전자'),('chemical','물질')]:
            policy = identity.get('policy', {}).get(kind, {})
            for action, title in [('new','신규 ID 연결'),('change','기존 ID 변경')]:
                enabled = policy.get(action, {}).get('enabled')
                if type(enabled) is bool:
                    r2_details.append(label + ' ' + title + (' 활성' if enabled else ' 비활성'))
        if r2_details:
            r2_details.append('학습 내부 정책·100편 성능과 별도')
    result.append({"model": "r2", "stage": r2.get("stage"),
                   "status": state(r2, predictions=r2.get('predictions_ready'), alive=r2.get('process_alive')),
                   "progress_detail": ' · '.join(r2_details),
                   "predictions_ready": r2.get("predictions_ready") is True})
    v8 = progress.get("v8", {})
    if v8.get("status") == "unavailable":
        result.append({"model": "v8", "stage": None, "status": "현재 상태 확인 불가·학습 실패로 판정하지 않음",
                       "predictions_ready": None})
    else:
        for name, value in v8.items():
            if not isinstance(value, dict) or "stage" not in value:
                continue
            details = []
            if value.get('status') != 'clinical_training_complete':
                for field, label in [('completed_ner_models','NER 학습 모델'),('completed_ner_profiles','자동 후보 profile'),
                                     ('completed_id_outer_folds','ID 외부 fold'),('completed_relation_outer_folds','관계 외부 fold')]:
                    if field in value:
                        details.append(label + ' ' + str(count(value[field])) + '개 완료')
                if 'io_memory_probe_peak_rss_bytes' in value:
                    peak=count(value['io_memory_probe_peak_rss_bytes'])
                    if peak is not None:
                        details.append(f'IO 재현 최고 RSS {peak/1024**3:.2f} GiB · 모델 성능 아님')
                if value.get('status') == 'resource_budget_exceeded' and 'peak_child_rss_bytes' in value:
                    peak=count(value['peak_child_rss_bytes'])
                    if peak is not None:
                        details.append(f'중단 전 프로세스 최고 RSS {peak/1024**3:.2f} GiB · 한도 8 GiB')
                child_error=value.get('child_stage_state', {}).get('error')
                if child_error and value.get('status') in {'failed','resource_budget_exceeded'}:
                    details.append(str(child_error))
            result.append({"model": name, "stage": value.get("stage"),
                           "status": state(value, predictions=value.get('prediction_manifest_exists'), alive=value.get('supervisor_alive')),
                           "progress_detail": ' · '.join(details),
                           "predictions_ready": value.get("prediction_manifest_exists") is True})
    spare = progress.get('spare_gpu')
    if isinstance(spare, dict):
        pipeline = spare.get('pipeline') or spare.get('launcher', {})
        result.append({'model': 'v8 보조 관계·이벤트 / ' + str(spare.get('host', '미확인')),
            'stage': pipeline.get('stage', pipeline.get('status')),
            'status': state(pipeline, predictions=False, alive=pipeline.get('process_alive')),
            'progress_detail': 'DrugProt·CG 고유 레이블 학습 · BioRED 100편 평가와 별도 · '
                + '외부 fold ' + str(spare.get('completed_outer_folds', {})) + ' · '
                + str(spare.get('gpu', 'GPU 미확인')),
            'predictions_ready': False})
    return result


def training_ner_rows(progress):
    rows=[]
    for engine,value in progress.get('v8', {}).items():
        if not isinstance(value,dict) or 'public_training_ner_diagnostics' not in value:
            continue
        diagnostic=value['public_training_ner_diagnostics']
        if diagnostic.get('official_training_only') is not True or diagnostic.get('independent_final_performance') is not False:
            raise ValueError('상류 NER 학습 진단의 평가 범위 불명')
        papers=count(diagnostic.get('training_papers'))
        if not papers:
            raise ValueError('상류 NER 학습 문헌 수 누락')
        for arm,m in sorted(diagnostic['boundary_metrics_on_public_train_OOF'].items()):
            rows.append({'engine':engine,'arm':arm,'papers':papers,'metrics':metrics(m),
                         'scope':'공식 학습 자료의 문헌 단위 OOF 경계 진단·100편 관계 평가와 별도',
                         'independent_final_performance':False})
    return rows


def clinical_rows(path, sources):
    if path is None or not Path(path).is_file():
        return [], "미평가"
    summary, _ = load_receipt(path, "clinical_summary", sources)
    if summary.get("independent_final_performance") is not False:
        raise ValueError("임상 진단을 독립 최종 성능으로 바꿀 수 없습니다")
    rows = []
    paper_counts = {r["corpus"]: r["papers"] for r in summary["rows"]}
    for detail in summary["details"]:
        corpus = detail["corpus"]
        if corpus not in {"ebmnlp", "evidenceoutcomes"} or paper_counts.get(corpus) != 200:
            raise ValueError("임상 진단은 자료별 같은 200편이어야 합니다")
        report_path = Path(path).parent / "predictions" / corpus / "report.json"
        report, digest = load_receipt(report_path, "clinical_report_" + corpus, sources)
        if report.get("papers") != 200 or not detail.get("same_input_gold_pmids_denominators_verified"):
            raise ValueError("임상 진단의 동일 코호트 확인이 없습니다")
        m = metrics(report["metrics"]["spans_outcomes"])
        if m != metrics(detail["metrics"]):
            raise ValueError("임상 요약과 저장 평가 수치가 다릅니다")
        _, totals = paper_scores(report, "spans_outcomes", 200)
        if totals != {k: m[k] for k in totals}:
            raise ValueError("임상 논문별 합계가 다릅니다")
        comparisons = {}
        for label in ("v6", "pio_only"):
            described = detail["comparison"][label]
            prefix = described["baseline_report_sha256"][:12]
            saved, _ = load_receipt(report_path.parent / ("comparison-" + prefix + ".json"),
                                    "clinical_comparison_" + corpus + "_" + label, sources)
            if saved != described or saved.get("same_cohort_and_gold_token_denominators") is not True:
                raise ValueError("임상 비교 원본 또는 코호트 확인이 다릅니다")
            if metrics(saved["candidate"]) != m:
                raise ValueError("임상 비교의 후보 수치가 다릅니다")
            baseline = metrics(saved["baseline"])
            if baseline["tp"] + baseline["fn"] != m["tp"] + m["fn"]:
                raise ValueError("임상 토큰 분모가 다릅니다")
            delta = m["f1"] - baseline["f1"]
            if abs(delta - saved["difference"]["f1"]) > 1e-8:
                raise ValueError("임상 F1 변화량이 다릅니다")
            ci = saved["paired_bootstrap_f1_difference_ci95"]
            if (ci.get("iterations") != 2000 or not -1 <= ci["lower"] <= ci["upper"] <= 1):
                raise ValueError("임상 신뢰구간을 확인할 수 없습니다")
            comparisons[label] = {"baseline": baseline, "f1_difference": delta, "ci95": ci,
                                  "difference_excludes_zero": ci["lower"] > 0 or ci["upper"] < 0}
        rows.append({"corpus": corpus, "papers": 200, "unit": "결과 구절의 공백 구분 토큰",
                     "metrics": m, "comparisons": comparisons, "source_sha256": digest,
                     "status": "완료·진단용·승격 불가", "target_plus5pp_vs_v6": comparisons["v6"]["f1_difference"] >= .05,
                     "full_ico_group_dose_time": "미평가", "independent_final_performance": False,
                     "upstream_distribution_mismatch": summary.get("upstream_pio_distribution_mismatch") is True})
    if {r["corpus"] for r in rows} != {"ebmnlp", "evidenceoutcomes"}:
        raise ValueError("임상 진단 자료가 누락됐습니다")
    return rows, "완료·진단 평가, 조건 연결 성능은 미평가"


def build_payload(progress_path, *, history_paths=None, r2_evaluation=None, v8_evaluation=None,
                  clinical_summary=None, previous_verified_progress=None):
    sources = []
    progress, progress_hash = load_receipt(progress_path, "progress", sources)
    if not isinstance(progress.get("observed_at"), str):
        raise ValueError("최신 상태 receipt의 관측 시각이 없습니다")
    observed = datetime.fromisoformat(progress["observed_at"].replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("관측 시각에는 시간대가 필요합니다")
    previous_observations = []
    if previous_verified_progress is not None:
        prior, _ = load_receipt(previous_verified_progress, 'previous_verified_progress', sources)
        previous_time = datetime.fromisoformat(prior['observed_at'].replace('Z','+00:00'))
        if previous_time.tzinfo is None or previous_time > observed:
            raise ValueError('이전 접속 확인 시각이 현재 관측보다 늦습니다')
        if prior.get('v8', {}).get('status') == 'unavailable':
            raise ValueError('이전 receipt에도 v8 접속 확인이 없습니다')
        for row in pipeline_states(prior):
            if row['model'].startswith('nutriomics-evidence-v8-run'):
                previous_observations.append({**row,
                    'observed_at_kst':previous_time.astimezone(timezone(timedelta(hours=9))).isoformat(),
                    'scope':'이전 접속 당시의 기록이며 현재 상태를 대체하지 않음'})
    paths = history_paths or {v: ROOT / "literature_service/runtime" / ("chatgpt-repair-" + v) / "comparison.json" for v in HISTORICAL}
    relations, gold_hash, cohort = historical_rows(paths, sources)
    relations += [new_evaluation("r2", r2_evaluation, sources, gold_hash, cohort),
                  new_evaluation("v8", v8_evaluation, sources, gold_hash, cohort)]
    counts, collection = collection_rows(progress)
    clinical, clinical_status = clinical_rows(clinical_summary, sources)
    return {"schema_version": "nutriomics-v8-submission-report-v1", "observed_at": progress["observed_at"],
            "observed_at_kst": observed.astimezone(timezone(timedelta(hours=9))).isoformat(),
            "generated_at": datetime.now(timezone.utc).isoformat(), "progress_sha256": progress_hash,
            "target": TARGET, "gold_sha256": gold_hash, "cohort_pmids_sha256": sha(canonical(sorted(cohort)).encode()),
            "counts": counts, "collection": collection, "relation_comparison": relations,
            "pipelines": pipeline_states(progress), "clinical": clinical, "clinical_status": clinical_status,
            "upstream_ner_training": training_ner_rows(progress),
            "previous_pipeline_observations": previous_observations,
            "default_engine": progress.get("default_engine", "미확인"),
            "additional_model_calls": count(progress.get("additional_model_calls")),
            "sealed_test_opened": progress.get("sealed_test_opened"),
            "completion_assessments": {
                "historical_development_comparison": "검증 완료",
                "new_relation_development_evaluations": {r['model']:r['status'] for r in relations[-2:]},
                "clinical_diagnostic": clinical_status,
                "training_internal_selection_gate": "본 보고서 입력으로 독립 검증하지 않음",
                "sealed_independent_test": "미검증·개발 비교 목표 충족과 별도 판정",
                "nutrition_domain_validation": "미측정",
                "seven_day_same_artifact_operation": "미검증",
                "full_system_completion": "판정 보류"},
            "claim_boundaries": [
                "100편·정답 관계 154건은 반복 사용한 개발 비교 자료이며 독립 최종 성능이 아니다.",
                "상류 NER 수치는 공식 학습 자료의 경계 탐지 OOF 진단이다. 다른 코호트의 과거 NER·ID·관계 성능과 직접 비교하지 않는다.",
                "정밀도 85%·정탐 65건·재현율 65/154는 내부 개발 목표다. 실제 협약 성과지표는 확인하지 못했다.",
                "문헌 수집, 모델 학습, 평가 완료, 목표 달성 및 행정 제출 요건을 각각 구분한다.",
                "새 모델의 개발 비교 목표 충족도 학습 내부 선택 기준·봉인 독립 시험·영양학 검증·7일 운영 통과를 대신하지 않는다.",
                "영양 문헌 실제 정확도와 연구자 검수 시간 절감은 미측정이다.",
                "임상 결과 구절 토큰 평가를 중재·비교군·용량·시점 자동 연결 정확도로 해석하지 않는다.",
                "실제 7일 연속 운영·봉인 독립 시험·최종 운영 전환은 이 보고서에서 검증 완료로 주장하지 않는다.",
                "라이브 DB와 고정 snapshot은 관측 범위와 시점이 다르므로 서로 더하거나 같은 분모로 계산하지 않는다.",
                "이전 접속 확인 기록은 해당 시각의 상태이며 최신 SSH 조회 결과나 현재 학습 상태를 대체하지 않는다.",
                "IO 재현 메모리는 저장·읽기 경로의 검사이며 전체 모델 학습 메모리나 예측 정확도 향상을 뜻하지 않는다.",
                "현재 원문 일치 추출 문헌은 원문 ID·해시와 일치하는 활성 결과가 최소 한 엔진에 있는 문헌의 합집합이며 v8 정확도나 성능을 뜻하지 않는다.",
                "엔진별 추출 문헌은 서로 중복될 수 있어 합산하지 않는다. 대기열의 실패 작업 행수를 실패 문헌 수로 바꾸지 않는다."],
            "administrative_submission": "협약서 지표와 기관 제출 요건 미확인으로 최종 제출 적합성 판정 보류",
            "sources": sources, "generator_sha256": sha(Path(__file__).read_bytes())}


def pct(value):
    return "미평가" if value is None else f"{100 * value:.2f}%"


def display(value):
    return "미확인" if value is None else f"{value:,}"


def tables(payload):
    relation = []
    for row in payload["relation_comparison"]:
        m = row["metrics"]
        relation.append([row["model"], row["status"], *([str(m[k]) for k in ("tp", "fp", "fn")] if m else ["미평가"] * 3),
                         str(row['predictions']) if m else "미평가",
                         *([pct(m[k]) for k in ("precision", "recall", "f1")] if m else ["미평가"] * 3),
                         "미평가" if m is None else ("충족" if row["target_passed"] else "미달")])
    counts = [[r["label"], display(r["value"]) + (r["unit"] if r["value"] is not None else ""), r["scope"], r["status"]] for r in payload["counts"]]
    clinical = []
    for row in payload["clinical"]:
        c = row["comparisons"]["v6"];ci = c["ci95"]
        clinical.append([row["corpus"], "200편", pct(row["metrics"]["f1"]), pct(c["baseline"]["f1"]),
                         f"{100*c['f1_difference']:+.2f}%p", f"[{100*ci['lower']:+.2f}, {100*ci['upper']:+.2f}]%p",
                         "충족" if row["target_plus5pp_vs_v6"] else "미달"])
    result = [("자료 규모와 집계 단위", ["지표", "건수", "범위", "상태"], counts),
            ("동일 100편·154건 관계 추출 비교", ["구성", "평가 상태", "TP", "FP", "FN", "예측 관계", "정밀도", "재현율", "F1", "내부 목표"], relation),
            ("임상 결과 구절 진단", ["자료", "범위", "현재 F1", "v6 F1", "변화", "문헌 bootstrap 2,000회 95% 구간", "+5%p 목표"], clinical),
            ("서버 작업 상태", ["작업", "단계", "상태", "세부 진행"], [[r["model"], r["stage"] or "미확인", r["status"], r.get('progress_detail') or '별도 확인'] for r in payload["pipelines"]])]
    if payload.get('upstream_ner_training'):
        result.append(('상류 NER 공식 학습 OOF 경계 진단·100편 관계 평가와 별도',
            ['실험','구성','문헌','TP','FP','FN','정밀도','재현율','F1'],
            [[r['engine'],r['arm'],str(r['papers'])+'편',*[str(r['metrics'][k]) for k in ['tp','fp','fn']],
              *[pct(r['metrics'][k]) for k in ['precision','recall','f1']]] for r in payload['upstream_ner_training']]))
    if payload.get('previous_pipeline_observations'):
        result.append(('이전 접속 확인 기록·현재 상태와 별도', ['확인 시각(KST)','작업','당시 단계','당시 진행'],
            [[r['observed_at_kst'],r['model'],r['stage'],r.get('progress_detail') or r['status']]
             for r in payload['previous_pipeline_observations']]))
    return result


def clinical_note(payload):
    if not payload['clinical']:
        return "저장된 임상 진단을 이 보고서에 포함하지 않았습니다. 임상 성능은 미평가로 표시합니다."
    passed=sum(row['target_plus5pp_vs_v6'] for row in payload['clinical'])
    inconclusive=sum(not row['comparisons']['v6']['difference_excludes_zero'] for row in payload['clinical'])
    return (f"임상 비교는 자료별 동일 200편의 결과 구절 토큰 평가입니다. v6 대비 +5%p 내부 목표 충족은 {passed}/{len(payload['clinical'])}개 자료입니다. "
            f"F1 차이의 95% 구간이 0을 포함하는 자료는 {inconclusive}개이며 개선을 단정하지 않습니다. "
            "이 결과는 진단용이며 중재·비교군·용량·시점 연결은 미평가입니다.")


def markdown(payload):
    lines = ["# NutriOmics 개발 성과 및 검증 현황", "", "관측 기준(KST): " + payload["observed_at_kst"], "",
             "확인된 과거 성능과 진행 중 작업을 구분한 보고서입니다. 새 관계 평가가 없으면 미평가로 표시합니다.", "",
             "내부 목표: TP ≥65, 재현율 ≥65/154(42.21%), 정밀도 ≥85%의 동시 충족.", ""]
    for title, headers, rows in tables(payload):
        lines += ["## " + title, "", "| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
        lines += ["| " + " | ".join(str(v).replace("|", "\\|") for v in row) + " |" for row in rows]
        lines += [""]
    lines += [clinical_note(payload), "",
              "## 보고 가능 성과와 추가 검증", "",
              "저장 원본과 해시로 확인된 수집 건수, 기존 관계 추출 비교와 임상 진단 결과를 보고할 수 있습니다. 진행 중 학습과 미평가 모델을 개선 성능으로 보고할 수 없습니다.", ""]
    lines += ["- " + value for value in payload["claim_boundaries"]]
    lines += ["", "행정 제출: " + payload["administrative_submission"], "", "## 출처와 재현", "",
              "각 JSON 원본의 SHA-256은 report.json의 sources에 보존했습니다. 모든 비율은 TP/FP/FN에서 재계산했으며 정답 라벨 파일을 열지 않았습니다.", "",
              "기본 엔진: " + payload["default_engine"], "추가 생성형 모델 호출: " + display(payload["additional_model_calls"]), ""]
    return "\n".join(lines)


def html_report(payload):
    e = lambda value: html.escape(str(value), quote=True)
    parts = ['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>NutriOmics 개발 성과 및 검증 현황</title><style>body{font-family:system-ui,sans-serif;color:#17324d;max-width:1200px;margin:32px auto;padding:0 20px;line-height:1.6}h1{font-size:28px}h2{margin-top:32px;font-size:20px}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #cbd5e1;padding:8px;text-align:left}th{background:#eaf2f8}small{color:#475569}.notice{background:#fff5db;padding:16px;border-radius:8px}@media print{body{margin:0;max-width:none}h2{break-after:avoid}tr{break-inside:avoid}table{font-size:10px}}</style>',
             '<body><h1>NutriOmics 개발 성과 및 검증 현황</h1><p>관측 기준(KST): ' + e(payload["observed_at_kst"]) + '</p>',
             '<p class="notice">내부 목표: TP ≥65 · 재현율 ≥42.21% · 정밀도 ≥85% 동시 충족. 새 평가가 없으면 미평가이며 0점이 아닙니다.</p>']
    for title, headers, rows in tables(payload):
        parts += ['<h2>' + e(title) + '</h2><div class="table-wrap"><table><thead><tr>',
                  ''.join('<th>' + e(v) + '</th>' for v in headers), '</tr></thead><tbody>']
        parts += ['<tr>' + ''.join('<td>' + e(v) + '</td>' for v in row) + '</tr>' for row in rows]
        parts += ['</tbody></table></div>']
    parts += ['<p>' + e(clinical_note(payload)) + '</p>',
              '<h2>보고 범위와 추가 검증</h2><ul>']
    parts += ['<li>' + e(v) + '</li>' for v in payload['claim_boundaries']]
    parts += ['</ul><p>행정 제출: ' + e(payload['administrative_submission']) + '</p>',
              '<h2>출처</h2><p>원본별 SHA-256과 집계 정의는 함께 제공한 report.json에 있습니다. TP/FP/FN으로 성능을 재계산했습니다.</p>',
              '<small>기본 엔진: ' + e(payload['default_engine']) + ' · 추가 생성형 모델 호출: ' + e(display(payload['additional_model_calls'])) + '</small></body></html>']
    return ''.join(parts)


def csv_report(payload):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["구분", "지표_구성", "상태", "값", "단위", "TP", "FP", "FN", "정밀도", "재현율", "F1", "범위"])
    for row in payload['counts']:
        writer.writerow(["규모", row['label'], row['status'], row['value'], row['unit'], '', '', '', '', '', '', row['scope']])
    for row in payload['relation_comparison']:
        m = row['metrics'] or {}
        writer.writerow(["관계", row['model'], row['status'], row['predictions'], '예측 관계', *[m.get(k) for k in ('tp','fp','fn','precision','recall','f1')], '반복 개발 100편·154건'])
    for row in payload['clinical']:
        writer.writerow(["임상", row['corpus'], row['status'], '', row['unit'], *[row['metrics'][k] for k in ('tp','fp','fn','precision','recall','f1')], '진단 200편'])
    for row in payload.get('upstream_ner_training', []):
        writer.writerow(['NER 학습 진단',row['engine']+'/'+row['arm'],'공식 학습 OOF',row['papers'],'편',
            *[row['metrics'][k] for k in ('tp','fp','fn','precision','recall','f1')],row['scope']])
    return stream.getvalue()


def write_report(payload, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    files = {'report.md': markdown(payload).encode('utf8'), 'report.html': html_report(payload).encode('utf8'),
             'report.csv': csv_report(payload).encode('utf-8-sig'), 'report.json': canonical(payload).encode('utf8')}
    for name, raw in files.items():
        with (output/name).open('xb') as stream:
            stream.write(raw)
    manifest = {'schema_version':1, 'files':{name:sha(raw) for name,raw in files.items()},
                'progress_sha256':payload['progress_sha256'], 'generator_sha256':payload['generator_sha256'],
                'source_files':payload['sources'], 'full_system_completion_claimed':False,
                'administrative_submission_ready':'unverified'}
    with (output/'report-manifest.json').open('x',encoding='utf8') as stream:
        stream.write(canonical(manifest))
    return manifest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--progress',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True,help='New immutable report directory')
    p.add_argument('--r2-evaluation',type=Path)
    p.add_argument('--v8-evaluation',type=Path)
    p.add_argument('--clinical-summary',type=Path)
    p.add_argument('--previous-verified-progress',type=Path,help='Optional older receipt; never replaces the current connection status')
    a = p.parse_args(argv)
    payload = build_payload(a.progress,r2_evaluation=a.r2_evaluation,v8_evaluation=a.v8_evaluation,
                            clinical_summary=a.clinical_summary,previous_verified_progress=a.previous_verified_progress)
    manifest = write_report(payload,a.output_dir)
    print(canonical({'output':str(a.output_dir.resolve()),'observed_at':payload['observed_at'],
                     'report_sha256':manifest['files']['report.json'],
                     'new_relation_evaluations':{r['model']:r['status'] for r in payload['relation_comparison'][-2:]}}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
