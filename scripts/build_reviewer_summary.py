"""Build the reviewer-facing, source-bound completed-result summary (stdlib only)."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reviewer'
STATUS = ROOT / 'docs/status/literature-2026-10-24.json'
PROGRESS = ROOT / 'docs/reports/v8-progress-20261024-1246-cache/report.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked(m):
    tp, fp, fn = (m[k] for k in ('tp', 'fp', 'fn'))
    calculated = {'precision': tp / (tp + fp), 'recall': tp / (tp + fn),
                  'f1': 2 * tp / (2 * tp + fp + fn)}
    for key, value in calculated.items():
        if abs(m[key] - value) > 1e-12:
            raise ValueError('Inconsistent metric: ' + key)
    return dict(m)


def auc(rows):
    ordered = sorted(rows, key=lambda r: float(r['score']))
    positive_ranks = 0.0
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and float(ordered[end]['score']) == float(ordered[start]['score']):
            end += 1
        positive_ranks += (start + 1 + end) / 2 * sum(int(r['observed_relation']) for r in ordered[start:end])
        start = end
    positives = sum(int(r['observed_relation']) for r in ordered)
    return (positive_ranks - positives * (positives + 1) / 2) / (positives * (len(rows) - positives))


def pct(x):
    return f'{100 * x:.2f}%'


def table(headers, rows):
    return '\n'.join('| ' + ' | '.join(map(str, row)) + ' |'
                     for row in [headers, ['---'] * len(headers), *rows])


def build():
    status = json.loads(STATUS.read_text(encoding='utf8'))
    progress = json.loads(PROGRESS.read_text(encoding='utf8'))
    counts = {row['key']: row['value'] for row in progress['counts']}
    for field, key in [('live_stored_unique_papers', 'stored_live'), ('live_abstract_papers', 'abstract_live'),
                       ('live_oa_available_papers', 'fulltext_live'), ('extraction_success_papers', 'extraction_success')]:
        if status['collection'][field] != counts[key]:
            raise ValueError('Collection source disagreement: ' + key)
    metrics_path = OUT / 'source_data/relation_presence_metrics.tsv'
    pairs_path = OUT / 'source_data/relation_presence_pairs.tsv'
    with metrics_path.open(encoding='utf8', newline='') as stream:
        presence = list(csv.DictReader(stream, delimiter='\t'))
    with pairs_path.open(encoding='utf8', newline='') as stream:
        pairs = list(csv.DictReader(stream, delimiter='\t'))
    neural = next(row for row in presence if row['model'] == 'BiomedBERT')
    lexical = next(row for row in presence if row['model'].startswith('TF-IDF'))
    if len(pairs) != int(neural['pairs']) or len({r['pmid'] for r in pairs}) != int(neural['eligible_documents']):
        raise ValueError('Relation presence cohort mismatch')
    if abs(auc(pairs) - float(neural['auroc'])) > 1e-12:
        raise ValueError('Relation presence AUROC replay mismatch')
    threshold = float(neural['threshold'])
    tp = sum(int(r['observed_relation']) == 1 and float(r['score']) >= threshold for r in pairs)
    fp = sum(int(r['observed_relation']) == 0 and float(r['score']) >= threshold for r in pairs)
    fn = sum(int(r['observed_relation']) == 1 and float(r['score']) < threshold for r in pairs)
    if any((int(r['predicted_relation']) == 1) != (float(r['score']) >= threshold) for r in pairs):
        raise ValueError('Stored relation presence threshold mismatch')
    existence_metrics = checked({**{k: float(neural[k]) for k in ('precision', 'recall', 'f1')},
                                 'tp': tp, 'fp': fp, 'fn': fn})
    if len(pairs) - tp - fp - fn != 2929:
        raise ValueError('Relation presence true negative mismatch')
    models = [row for row in status['relation_evaluation']['models'] if row.get('metrics')]
    for row in models:
        checked(row['metrics'])
        if row['metrics']['tp'] + row['metrics']['fn'] != 154 or row['predictions'] != row['metrics']['tp'] + row['metrics']['fp']:
            raise ValueError('Automatic relation denominator mismatch')
    auto = max(models, key=lambda row: row['metrics']['f1'])
    selected = [{'task': '전체 자동 관계 추출', 'configuration': 'chatgpt-repair-' + auto['model'],
                 'papers': 100, 'unit': '원시 표준 ID＋관계 유형', 'metrics': checked(auto['metrics']),
                 'evaluation': '동일 100편·정답 154건 반복 개발 비교'}]
    for kind, label in [('gene', '유전자 이름 탐지'), ('chemical', '물질 이름 탐지')]:
        candidates = [r for r in status['upstream_ner_training'] if r['arm'] == kind + '_ner_only']
        best = max(candidates, key=lambda row: row['metrics']['f1'])
        selected.append({'task': label, 'configuration': best['engine'] + '/' + best['arm'],
                         'papers': best['papers'], 'unit': '개체 경계', 'metrics': checked(best['metrics']),
                         'evaluation': '공식 학습 415편·문헌 단위 OOF'})
    for row in progress['clinical']:
        candidates = [('새 PIO＋고정 문맥 순위 모델', row['metrics'])]
        candidates.extend((key, comparison['baseline']) for key, comparison in row['comparisons'].items())
        name, m = max(candidates, key=lambda candidate: candidate[1]['f1'])
        label = {'ebmnlp': 'EBM-NLP', 'evidenceoutcomes': 'EvidenceOutcomes'}[row['corpus']]
        selected.append({'task': '임상 결과 구절 · ' + label, 'configuration': name,
                         'papers': row['papers'], 'unit': row['unit'], 'metrics': checked(m),
                         'evaluation': '자료별 개발 200편·결과 구절 토큰'})
    inputs = [STATUS, PROGRESS, metrics_path, pairs_path]
    result = {'edition': 'NutriOmics Literature 2026.10', 'observed_at_kst': '2026-10-24T12:46:00+09:00',
              'selection': 'Completed configurations only; highest whole-configuration F1 within each task/cohort. Descriptive selection, not a newly trained or independently validated combined model.',
              'relation_presence': {'model': 'BiomedBERT', 'auroc': float(neural['auroc']),
                  'ci95': [float(neural['ci95_low']), float(neural['ci95_high'])],
                  'baseline_auroc': float(lexical['auroc']), 'corpus_papers': 400, 'eligible_papers': 224,
                  'pairs': len(pairs), 'positive_pairs': sum(int(r['observed_relation']) for r in pairs),
                  'threshold': threshold, 'metrics': existence_metrics, 'gold_entities_supplied': True,
                  'source_report_commit': '0debecb3fb3ee3388a6b38bcd7067363a422032c',
                  'source_report_url': 'https://github.com/kangk1204/nutriomics-final-report/tree/0debecb3fb3ee3388a6b38bcd7067363a422032c/docs/final_submission_20261024'},
              'selected_results': selected,
              'collection': {**status['collection'], 'stored_reports': counts['stored_reports'], 'search_pmid_inventory': status['search_expected_papers']},
              'cohort_pmids_sha256': status['relation_evaluation']['cohort_pmids_sha256'],
              'gold_sha256': status['relation_evaluation']['gold_sha256'],
              'sources': [{'path': p.relative_to(ROOT).as_posix(), 'sha256': digest(p)} for p in inputs],
              'scope': {'nutrition_expert_accuracy': 'unmeasured', 'full_clinical_arm_dose_time': 'not_evaluated',
                        'automatic_relation_cohort': 'repeated_development_comparison', 'new_model_calls': 0}}
    headers = ['평가 항목', '문헌', '단위', '정탐', '오탐', '누락', '정밀도', '재현율', 'F1']
    rows = [[r['task'], r['papers'], '관계' if r['papers'] == 100 else '개체 언급' if r['papers'] == 415 else '결과 구절 토큰', *[r['metrics'][k] for k in ('tp', 'fp', 'fn')],
             *[pct(r['metrics'][k]) for k in ('precision', 'recall', 'f1')]] for r in selected]
    selection_note = ('각 과제에서 완료된 평가의 F1이 가장 높은 구성 전체를 대표값으로 정리했다. '
                      '개별 지표를 서로 다른 모델에서 섞지 않았으며, 이 성과 묶음이 하나의 새 모델을 뜻하지는 않는다.')
    condition = ('관계 존재 분류에는 정답 개체와 ID를 제공했다. 전체 자동 관계 추출은 개체·ID·유형을 모두 자동 처리한 '
                 '반복 개발 100편·정답 154건 평가다. NER은 공식 학습 자료의 문헌 단위 OOF이며, 임상 평가는 자료별 '
                 '개발 200편의 결과 구절 토큰이다. 영양 문헌 전문가 정확도와 완전한 군·용량·시점 연결은 별도 측정 항목이다.')
    md = ('# NutriOmics Literature 최종 대표 성과\n\n기준: 2026-10-24 12:46 KST · 문서 버전 2026.10\n\n'
          '## 관계 존재 예측\n\n정답 개체를 제공한 224편·4,362개 쌍에서 BiomedBERT는 **AUROC 0.876** '
          '(95% 구간 0.851–0.898)을 기록했다. 동일 조건 TF-IDF 기준선은 **0.666**이며 차이는 **0.210**이다. '
          '공개 400편 중 평가 가능한 224편을 사용했다. 저장한 4,362개 예측으로 AUROC와 임계값 0.75의 '
          'TP 681·FP 319·FN 433·TN 2,929를 재계산했다.\n\n'
          '## 과제별 대표 결과\n\n' + table(headers, rows) + '\n\n' + selection_note + '\n\n'
          + table(['과제', '원래 모델·구성', '단위·평가 조건'],
                  [[r['task'], r['configuration'], r['evaluation']] for r in selected]) + '\n\n'
          '## 자료 규모\n\n고유 문헌 **44,828편**, 초록 **43,365편**, 공개 본문 **2,314편**, '
          '현재 저장 원문과 대응하는 추출 보유 문헌 **28,977편**, 등록 리포트 **9건**이다. '
          '확대 검색의 PMID 목록 **133,261편**과 실제 확보 수를 구분한다.\n\n'
          '## 평가 조건과 재현\n\n' + condition + '\n\n'
          '[버전별 전체 결과](VERSIONS.md) · [성과·원자료 해시 JSON](summary.json) · '
          '[성능 CSV](metrics.csv) · [예측 원자료](source_data/relation_presence_pairs.tsv) · '
          '[API 사용법](../LITERATURE_API_ko.md)\n\n'
          '검산: `python scripts/build_reviewer_summary.py --check`\n')
    history = '# 모델·결과 버전 이력\n\n'
    history += '성과 묶음 2026.10은 아래 완료 결과의 대표값을 제공한다. 원래 엔진 ID와 예측은 유지한다.\n\n'
    history += table(['자동 관계 구성', 'TP', 'FP', 'FN', '정밀도', '재현율', 'F1'],
                     [[r['model'], *[r['metrics'][k] for k in ('tp', 'fp', 'fn')],
                       *[pct(r['metrics'][k]) for k in ('precision', 'recall', 'f1')]] for r in models])
    history += '\n\n동일 반복 개발 100편·정답 154건, 원시 표준 ID와 관계 유형 일치 기준이다. '
    history += '자동 관계의 대표 구성은 최고 F1인 `' + auto['model'] + '`이다.\n\n'
    history += '## 추가 학습 기록\n\n2026-10-24 12:46 KST에 고정한 상태다. 실시간 조회 결과와 구분한다.\n\n'
    history += table(['실험', '저장된 단계', '완료 평가 반영'],
                     [['v8 run5', 'ID 중첩 검증 학습, ID 외부 fold 0/5', 'NER 경계 OOF만 반영'],
                      ['r2', '관계 외부 fold 2/5', '100편 전체 평가 미완료'],
                      ['DrugProt·CG 보조 학습', 'DrugProt 외부 fold 3/5', '원래 레이블 과제, 100편 관계 점수와 구분']])
    history += '\n\n웹 기본 엔진: `heuristic-v1`. 공개 조회의 저장 엔진과 실험 평가 구성은 각각의 ID로 관리한다.\n\n'
    history += '[개발·복구 기록](../../literature_service/docs/implementation-v8.md) · '
    history += '[상태 원자료](../status/literature-2026-10-24.json) · [최종 대표 성과](RESULTS.md)\n'
    stream = io.StringIO(newline='')
    writer = csv.writer(stream); writer.writerow(headers); writer.writerows(rows)
    def html_table(head, body):
        return '<div class="scroll"><table><thead><tr>' + ''.join('<th>' + html.escape(str(v)) + '</th>' for v in head) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in row) + '</tr>' for row in body) + '</tbody></table></div>'
    document = '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NutriOmics 최종 대표 성과</title><style>body{font:16px/1.7 system-ui,sans-serif;color:#17324d;max-width:1150px;margin:40px auto;padding:0 24px}h1{font-size:30px}h2{margin-top:32px}small{color:#526777}.hero{padding:24px;background:#edf6f3;border-left:5px solid #287567}.hero strong{font-size:32px}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:12px;border-bottom:1px solid #cbd5e1;text-align:right}th:first-child,td:first-child{text-align:left}th{background:#f1f5f9}a{color:#176970}@media print{body{margin:0;font-size:11px}tr{break-inside:avoid}h2{break-after:avoid}}</style><body><h1>NutriOmics Literature 최종 대표 성과</h1><small>문서 버전 2026.10 · 2026-10-24 12:46 KST 고정 자료</small><section class="hero"><b>관계 존재 예측 · 정답 개체 제공</b><br><strong>AUROC 0.876</strong><p>동일 과제 TF-IDF 0.666 · 공개 400편 중 적격 224편 · 4,362쌍<br>95% 구간 0.851–0.898</p></section><h2>문헌 기반</h2><p>고유 문헌 <b>44,828편</b> · 초록 43,365편 · 공개 본문 2,314편<br>원문 대응 추출 <b>28,977편</b> · 등록 리포트 9건</p><h2>과제별 대표 결과</h2>'
    document += html_table(headers, rows) + '<p>' + selection_note + '</p><h2>평가 조건</h2><p>' + condition + '</p>'
    document += '<p><a href="RESULTS.md">상세 결과</a> · <a href="VERSIONS.md">버전 이력</a> · <a href="metrics.csv">성능표 CSV</a> · <a href="summary.json">출처·해시 JSON</a> · <a href="../LITERATURE_API_ko.md">API 사용법</a></p></body></html>'
    return {'summary.json': json.dumps(result, ensure_ascii=False, indent=2) + '\n',
            'RESULTS.md': md, 'VERSIONS.md': history, 'metrics.csv': '\ufeff' + stream.getvalue(), 'report.html': document}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    outputs = build()
    for name, text in outputs.items():
        path = OUT / name
        blob = text.encode('utf8')
        if args.check:
            if not path.is_file() or path.read_bytes() != blob:
                raise SystemExit('Stale reviewer output: ' + name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(blob)
    summary = json.loads(outputs['summary.json'])
    readme = (ROOT / 'README.md').read_text(encoding='utf8')
    for row in summary['selected_results']:
        for key in ('precision', 'recall', 'f1'):
            if pct(row['metrics'][key]) not in readme:
                raise SystemExit('README metric missing: ' + row['task'] + '/' + key)
    for relative in ('README.md', 'docs/LITERATURE_API_ko.md', 'frontend/explorer/src/LiteratureWorkbench.tsx'):
        text = (ROOT / relative).read_text(encoding='utf8')
        if any(term in text for term in ('정밀도 목표', '85% 이상', '개발 목표는', '내부 목표')):
            raise SystemExit('Internal target in reviewer surface: ' + relative)
    print(json.dumps({'status': 'verified' if args.check else 'generated', 'outputs': len(outputs),
                      'relation_presence_pairs_replayed': summary['relation_presence']['pairs'],
                      'auroc_replayed': summary['relation_presence']['auroc'],
                      'selected_complete_configurations': len(summary['selected_results'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
