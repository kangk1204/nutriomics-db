"""Render the measured public summary, with native task coverage and uncertainty."""
import csv
import html
import json

from nutri_literature.settings import Settings


def percent(value):
    return '미정의' if value is None else f'{100*value:.2f}%'


def percentage_points(value):
    return '미정의' if value is None else f'{100*value:+.2f}%p'


def estimate(metric,key):
    value=percent(metric.get(key))
    interval=metric.get(key+'_ci95')
    if interval:
        value+=f" [{percent(interval['lower'])}, {percent(interval['upper'])}]"
    return value


def markdown_html(lines):
    """Render the report's own headings, paragraphs and simple tables safely."""
    output=[]
    table=False
    for line in lines:
        if line.startswith('|'):
            if set(line.replace('|','').replace(' ','').replace(':','')) <= {'-'}:
                continue
            cells=[cell.strip() for cell in line.strip('|').split('|')]
            if not table:
                output.append('<table>')
            tag='td' if table else 'th'
            output.append('<tr>'+''.join(f'<{tag}>'+html.escape(cell)+f'</{tag}>' for cell in cells)+'</tr>')
            table=True
            continue
        if table:
            output.append('</table>')
            table=False
        if not line:
            continue
        if line.startswith('#'):
            level=min(len(line)-len(line.lstrip('#')),6)
            output.append(f'<h{level}>'+html.escape(line.lstrip('#').strip())+f'</h{level}>')
        else:
            output.append('<p>'+html.escape(line)+'</p>')
    if table:
        output.append('</table>')
    return ''.join(output)


def main():
    settings=Settings.from_env()
    summary=json.loads((settings.home/'validation'/'public-v2-summary.json').read_bytes())
    directory=settings.home/'validation'/'public-v2-report'
    directory.mkdir(parents=True,exist_ok=True)
    rows=[]
    for measurement in summary['measurements']:
        for endpoint,metric in sorted(measurement['metrics'].items()):
            if endpoint in {'scopes','scopes_exact'} or endpoint.startswith('scopes_exact:'):
                continue
            applicable=measurement.get('annotation_applicability') or {}
            papers=applicable.get('annotated_documents_by_endpoint',{}).get(endpoint,measurement['documents'])
            rows.append({'분할':measurement['split'],'자료':measurement['corpus'],
                         '수준':'fulltext' if ':fulltext:' in measurement['key'] else 'abstract',
                         '엔진':measurement.get('ablation_stage') or measurement['engine'],'모드':measurement['mode'],
                         '항목':endpoint,'주석 논문':papers,'TP':metric['tp'],'FP':metric['fp'],'FN':metric['fn'],
                         '정밀도·95%구간':estimate(metric,'precision'),'재현율·95%구간':estimate(metric,'recall'),
                         'F1·95%구간':estimate(metric,'f1'),'평가 ID':measurement['id']})
    fields=list(rows[0]) if rows else []
    with (directory/'metrics.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    lines=['# heuristic-v2 공개 평가 결과','',f"기록 시각: {summary['measured_at']}",'',
           '공개 과제의 측정 결과이며 영양학 정확도와 검수 시간 절감은 미측정이다. '
           '주석된 항목과 논문만 평가한다. 해당 항목의 정답이 비어 있는 주석 논문도 분모에 포함한다. '
           '정답 개체 제공(gold_entities)과 자동 탐지(end_to_end)는 별도 평가이다. '
           '지원하지 못한 관계 유형과 문장 간 관계도 전체 관계 분모에서 제외하지 않는다.','',
           '대괄호는 논문 bootstrap 2,000회의 percentile 95% 구간이다. '
           'BioScope·EvidenceOutcomes는 공식 시험 분할이 없는 자료로, 사전 등록한 ID 기반 논문 분할을 사용했다. '
           'BioScope 범위 점수는 cue-linked whitespace token F1이며 공식 Java scorer 점수와 구분한다. '
           'BioScope 본문 최종 분할은 2편으로 일반화와 구간 해석이 제한된다. '
           'PICO와 결과 항목의 annotated_spans 점수도 주석 토큰 기준이며 구절 전체 일치 정확도와 구분한다.','']
    for split in ['test','development']:
        selected=[row for row in rows if row['분할']==split]
        if not selected:
            continue
        lines.extend(['## '+('동결 최종 평가' if split=='test' else '개발 평가·단계별 비교'),'',
                      '| 자료·수준 | 엔진·모드 | 항목 | 주석 논문 | 정밀도 | 재현율 | F1 |',
                      '|---|---|---|---:|---|---|---|'])
        for row in selected:
            lines.append(f"| {row['자료']}·{row['수준']} | {row['엔진']}·{row['모드']} | {row['항목']} | "
                         f"{row['주석 논문']} | {row['정밀도·95%구간']} | {row['재현율·95%구간']} | {row['F1·95%구간']} |")
        lines.append('')
    promotion=summary.get('promotion',{})
    paired=[]
    for comparison in summary.get('comparisons',[]):
        for endpoint,result in sorted(comparison.get('bootstrap',{}).items()):
            for name,value in result['metrics'].items():
                paired.append({'분할':comparison['split'],'자료':comparison['corpus'],
                               '수준':'fulltext' if ':fulltext:' in comparison['key'] else 'abstract',
                               '모드':comparison['mode'],'항목':endpoint,'지표':name,
                               'v1':percent(value['baseline']),'v2':percent(value['candidate']),
                               '차이·%p':percentage_points(value['delta']),
                               '95%구간·%p':f"[{percentage_points(value['lower_ci95'])}, {percentage_points(value['upper_ci95'])}]",
                               '논문':result['papers'],'유효draws':value['valid_draws'],
                               '비교ID':comparison['id']})
    # Keep the complete paired comparisons in a separate machine-readable table.
    if paired:
        paired_fields=list(paired[0])
        with (directory/'comparisons.csv').open('w',encoding='utf-8-sig',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=paired_fields)
            writer.writeheader()
            writer.writerows(paired)
        lines.extend(['## 최종 v1/v2 차이','',
                      '동일 논문을 짝지어 bootstrap 2,000회로 계산했다. 차이와 구간은 퍼센트포인트(%p)다. '
                      '예측이 없는 경우 정밀도와 그 차이는 미정의다. Oracle 개체 탐지 점수는 모델 성능 해석에서 제외한다.','',
                      '| 자료·모드 | 항목·지표 | v1 | v2 | 차이(%p) | 95% 구간(%p) | 논문 |',
                      '|---|---|---|---|---|---|---:|'])
        for row in paired:
            if row['분할']!='test' or (row['모드']=='gold_entities' and row['항목']=='gene_detection'):
                continue
            lines.append(f"| {row['자료']}·{row['수준']}·{row['모드']} | {row['항목']}·{row['지표']} | "
                         f"{row['v1']} | {row['v2']} | {row['차이·%p']} | {row['95%구간·%p']} | {row['논문']} |")
        lines.append('')
    normalized=[m for m in summary['measurements'] if m['split']=='test' and m['mode']=='end_to_end'
                and isinstance(m.get('normalization'),dict)]
    if normalized:
        lines.extend(['## ID 정규화와 교차표 범위','',
                      '정확도는 정규화된 예측 중 정확한 경계·ID의 비율이다. '
                      '재현율은 전체 정답 개체 기준이며 공식 교차표 coverage를 함께 표시한다. '
                      '화합물 교차표를 제공하지 않았으므로 해당 범위의 성능을 과장하지 않는다.','',
                      '| 자료 | 엔진 | 개체 | 정확도 | 재현율 | 미정규화율 | 교차표 coverage |',
                      '|---|---|---|---|---|---|---|'])
        for measurement in normalized:
            for kind,value in measurement['normalization'].get('by_kind',{}).items():
                lines.append(f"| {measurement['corpus']} | {measurement['engine']} | {kind} | "
                             f"{percent(value.get('accuracy'))} | {percent(value.get('recall'))} | "
                             f"{percent(value.get('unnormalized_fraction'))} | {percent(value.get('crosswalk_coverage'))} |")
        lines.append('')
    if summary.get('test_unavailable'):
        lines.extend(['## 완료하지 못한 공개 시험','',
                      'EBM-NLP는 동결한 어댑터가 test/gold 중첩 경로를 지원하지 않아 최종 점수를 산출하지 않았다. '
                      '191편의 전문가 시험 논문 중 3편이 다른 자료의 개발 분할과 겹친다. '
                      '원래 프로토콜에는 개발 PICO 점수만 있으며 공개 시험 전체 완료로 표시하지 않는다. '
                      '별도 보완 프로토콜이 있는 경우 아래에 구분해 표시한다.',''])
    supplement_path=settings.home/'validation'/'ebm-expert-supplement-summary.json'
    if supplement_path.exists():
        supplement=json.loads(supplement_path.read_bytes())
        protocol=supplement['protocol']
        supplement_rows=[]
        lines.extend(['## EBM-NLP 별도 전문가 보완 시험','',
                      '기존 프로토콜과 구분한 '+protocol['version']+'의 결과다. '
                      '공식 전문가 시험 191편에서 다른 개발 자료의 3편과 기존 시험 예측 1편을 제외한 187편이다. '
                      '원본·제외 ID·어댑터·기존 규칙·모델·평가 코드를 먼저 감사 기록에 고정하고 주석을 바이트 그대로 변환했다. '
                      '이미 수행한 봉인 예측과 알고리즘을 바꾸지 않았다. 원래 전체 시험의 완료 플래그는 false로 유지한다.','',
                      '보완 측정 기록: '+supplement['measured_at']+' · 프로토콜 ID: '+protocol['id'],'',
                      '| 엔진 | 주석 항목 | 주석 논문 | 정밀도·95%구간 | 재현율·95%구간 | F1·95%구간 |',
                      '|---|---|---:|---|---|---|'])
        for engine,measurement in supplement['measurements'].items():
            applicable=measurement['annotation_applicability']['annotated_documents_by_endpoint']
            for endpoint,metric in sorted(measurement['metrics'].items()):
                supplement_rows.append({'엔진':engine,'항목':endpoint,'주석 논문':applicable[endpoint],
                                        'TP':metric['tp'],'FP':metric['fp'],'FN':metric['fn'],
                                        '정밀도·95%구간':estimate(metric,'precision'),
                                        '재현율·95%구간':estimate(metric,'recall'),
                                        'F1·95%구간':estimate(metric,'f1'),'평가 ID':measurement['id']})
                lines.append(f"| {engine} | {endpoint} | {applicable[endpoint]} | "
                             f"{estimate(metric,'precision')} | {estimate(metric,'recall')} | {estimate(metric,'f1')} |")
        if supplement_rows:
            with (directory/'ebm-supplement-metrics.csv').open('w',encoding='utf-8-sig',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=list(supplement_rows[0]))
                writer.writeheader()
                writer.writerows(supplement_rows)
        supplement_comparisons=[]
        for endpoint,result in supplement['comparison']['bootstrap'].items():
            for name,value in result['metrics'].items():
                supplement_comparisons.append({'항목':endpoint,'지표':name,'주석 논문':result['papers'],
                                               'v1':percent(value['baseline']),'v2':percent(value['candidate']),
                                               '차이·%p':percentage_points(value['delta']),
                                               '95%하한·%p':percentage_points(value['lower_ci95']),
                                               '95%상한·%p':percentage_points(value['upper_ci95']),
                                               '유효draws':value['valid_draws']})
        if supplement_comparisons:
            with (directory/'ebm-supplement-comparisons.csv').open('w',encoding='utf-8-sig',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=list(supplement_comparisons[0]))
                writer.writeheader()
                writer.writerows(supplement_comparisons)
        lines.extend(['',
                      '주석 파일이 없는 항목은 해당 항목의 분모에서 제외하고 주석 파일의 모든 값이 0인 경우는 음성 정답으로 유지한다. '
                      'v1은 이 어댑터에서 PICO 주석 token을 출력하지 않아 예측 0건이다. '
                      '선정·제외 사유·원본별 해시·짝지은 bootstrap 차이는 ebm-expert-supplement-summary.json에 보존했다.',''])
    lines.extend(['## 기본 엔진 판정','',
                  '판정: '+promotion.get('status','최종 평가 미완료'),'',
                  '기본 엔진: '+promotion.get('default_engine','heuristic-v1'),'',
                  '미충족 기준: '+', '.join(promotion.get('reasons',[])),'',
                  '원본·분할·예측·동결·평가·짝지은 비교는 runtime/public_benchmarks/와 DB 감사 기록에 보존했다. '
                  'ID 정규화·교차표 coverage·유형별 관계·출처 검사는 public-v2-summary.json과 개별 평가 보고서를 따른다.'])
    (directory/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    document='<html lang="ko"><meta charset="utf-8"><title>heuristic-v2 공개 평가</title><style>body{font-family:system-ui;margin:32px}table{border-collapse:collapse;font-size:13px;margin-bottom:24px}td,th{border:1px solid #ccc;padding:8px}th{background:#eef2f7}</style>'+markdown_html(lines)+'</html>'
    (directory/'report.html').write_text(document,encoding='utf-8')
    print(json.dumps({'directory':str(directory),'endpoint_rows':len(rows),'test_complete':summary['test_complete']}))


if __name__=='__main__':
    main()
