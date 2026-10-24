"""Publish audited development-only experiments for the internal review UI."""
import json

from nutri_literature.benchmarks import _receipt, verify_receipt
from nutri_literature.db import dumps, now, uid
from nutri_literature.extraction_v3 import RULES_VERSION
from nutri_literature.settings import Settings


def main():
    settings=Settings.from_env()
    root=settings.home/'experiments/v3'
    root.mkdir(parents=True,exist_ok=True)
    reports=[]
    for path in sorted(root.glob('*/report.json')):
        report=json.loads(path.read_bytes())
        if report['identity']['rules_version']!=RULES_VERSION:
            continue
        if report['split']!='development' or report['promotion_eligible'] or not verify_receipt(settings,'public_v3_development_experiment',report):
            raise ValueError('Experiment is not an audited development-only receipt')
        report.pop('output_path',None)
        reports.append(report)
    value={'schema_version':'literature-v3-experiment-summary-v1','id':uid(),'at':now(),
           'engine':'heuristic-v3','rules_version':RULES_VERSION,'status':'exploratory_development_only',
           'promotion_eligible':False,'default_engine':'heuristic-v1','reports':reports,
           'sealed_test_reused':False,'nutrition_accuracy':'unmeasured','review_time_saved':'unmeasured',
           'note':'동일 개발 논문을 비교한 탐색 결과입니다. 최종 시험 성능이나 영양 문헌 정확도가 아닙니다.'}
    _receipt(settings,'public_v3_experiment_summary',value['id'],value)
    temporary=root/'summary.tmp'
    temporary.write_text(dumps(value),encoding='utf-8')
    temporary.replace(root/'summary.json')
    lines=['# heuristic-v3 개발 비교 결과','',value['note'],'',
           '기본 엔진: heuristic-v1 · v3: 개발 실험 · 기존 봉인 시험 재사용 없음','',
           '| 자료·모드 | 논문 | 항목 | v2 정밀도 | v2 재현율 | v3 정밀도 | v3 재현율 | v2 F1 | v3 F1 |',
           '|---|---:|---|---:|---:|---:|---:|---:|---:|']
    def percent(x):
        return '미정의' if x is None else f'{x*100:.2f}%'
    for report in reports:
        for endpoint,metric in report['metrics']['heuristic-v3'].items():
            if endpoint.startswith('scopes_exact') or endpoint in {'scopes','annotated_spans'}:
                continue
            old=report['metrics']['heuristic-v2'][endpoint]
            values=[old['precision'],old['recall'],metric['precision'],metric['recall'],old['f1'],metric['f1']]
            lines.append(f"| {report['corpus']}·{report['mode']} | {report['papers']} | {endpoint} | "+' | '.join(percent(v) for v in values)+' |')
    lines += ['', 'BioScope·EBM-NLP·EvidenceOutcomes는 미리 정한 ID 해시 순서로 개발 논문 최대 200편을 사용한다. '
              'BioREDirect 개발 100편은 전체를 사용한다. 각 쌍은 동일 논문·원문·정답·평가 규칙으로 비교한다.',
              '', '95% 구간은 summary.json의 comparisons에서 논문 단위 bootstrap 2,000회로 제공한다. '
              'MeSH 사전은 2026년 공식 descriptor 자료이며 단백질·효소·핵산 및 약리 작용 분류는 제외한다. '
              'MeSH descriptor ID를 ChEBI의 개별 물질 ID와 동일하다고 주장하지 않는다.',
              '', '영양 문헌 정확도·실제 검수 시간·독립 최종 성능·v3 연속 운영은 아직 측정하지 않았다.']
    (root/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print(dumps({'reports':len(reports),'path':str(root/'summary.json')}),flush=True)


if __name__=='__main__':
    main()
