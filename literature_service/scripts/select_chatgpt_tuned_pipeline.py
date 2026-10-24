"""Select a pipeline family using train40 only, then assemble all held-fixed comparisons."""
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def select_training():
    from nutri_literature import chatgpt_tuning as base, chatgpt_joint_verifier as joint
    from nutri_literature.chatgpt_grounding import Catalog
    from nutri_literature.chatgpt_extraction import project
    from nutri_literature.chatgpt_id_ablation import restore_ids
    from nutri_literature.chatgpt_evaluation import _read, _metric
    from nutri_literature.contract_v7 import immutable, canonical, sha
    from nutri_literature.splits_v7 import grouped_folds
    while not (joint.HOME/'selection.json').exists():
        time.sleep(5)
    docs = _read(base.HOME/'inputs/calibration-gold.json')['documents']
    identity = _read(base.HOME/'identity.json')
    catalog = Catalog.load(ROOT/'runtime/chatgpt-inputs/official-id-dictionary.json.gz')
    families = {}
    for mode in ('fill_missing','revalidate_all'):
        predictions = {}
        for doc in docs:
            answer = _read(joint.HOME/'original-calibration/responses'/('paper-'+doc['pmid']+'.json'))
            original = project(doc['text'],answer['text'])['chatgpt_only']
            model = identity['crossfit_models'][str(identity['folds'][doc['pmid']])]
            predictions[doc['pmid']] = restore_ids(doc['text'],original,catalog,model,mode)
        rows = base.score_rows(docs,predictions)
        families['sol_'+mode] = {'configuration':mode,'per_paper':{p:r['relation'] for p,r in rows.items()}}
    for model,_ in base.MODELS:
        selection = _read(base.directory(model)/'selection.json')
        row = next(r for r in selection['grid'] if r['configuration'] == selection['selected'])
        families['sol_candidate' if model == 'gpt-6.1-sol' else 'luna_candidate'] = {
            'configuration':selection['selected'],'per_paper':{p:r['relation'] for p,r in row['per_paper'].items()}}
    selection = _read(joint.HOME/'selection.json')
    row = next(r for r in selection['grid'] if r['configuration'] == selection['selected'])
    families['joint'] = {'configuration':selection['selected'],'per_paper':{p:r['relation'] for p,r in row['per_paper'].items()}}
    def objective(metric):
        target = bool(metric['precision'] is not None and metric['precision'] >= .85 and metric['recall'] >= .30)
        return (target,metric['recall'] if target else metric['f1'],metric['f1'],metric['precision'] or 0.)
    for value in families.values():
        value['training_metrics'] = _metric(list(value['per_paper'].values()))
    winner = max(families,key=lambda k:objective(families[k]['training_metrics']))
    folds = grouped_folds(docs,salt='pipeline-family-selection')
    nested = []
    for fold in range(5):
        chosen = max(families,key=lambda k:objective(_metric([r for p,r in families[k]['per_paper'].items() if folds[p] != fold])))
        nested.extend(r for p,r in families[chosen]['per_paper'].items() if folds[p] == fold)
    record = {'schema':'chatgpt-training-only-family-selection-v1','selected_family':winner,'families':families,
              'selection_scope':'official train40 only; policy-level cross-validation, not independent final performance',
              'development_results_read_for_selection':False,'family_holdout_metrics':_metric(nested),
              'calibration_gold_sha256':sha((base.HOME/'inputs/calibration-gold.json').read_bytes()),
              'reviewer_selection_sha256':sha((joint.HOME/'selection.json').read_bytes()),
              'source_sha256':sha(Path(__file__).read_bytes()),'default_engine':'heuristic-v1'}
    immutable(base.HOME/'overall-selection.json',record)
    print(json.dumps({'selected_family':winner,'training_metrics':families[winner]['training_metrics'],
                      'family_holdout_metrics':record['family_holdout_metrics'],
                      'selection_sha256':sha(canonical(record))},ensure_ascii=False))


def report():
    import csv
    from nutri_literature import chatgpt_tuning as base, chatgpt_joint_verifier as joint
    from nutri_literature.chatgpt_evaluation import _read, _bootstrap
    from nutri_literature.contract_v7 import immutable, sha
    choice = _read(base.HOME/'overall-selection.json')
    initial = _read(base.PRIOR/'five-configuration-comparison.json')
    tuned = _read(base.HOME/'comparison.json')
    reviewed = _read(joint.HOME/'comparison.json')
    diagnostic = _read(base.HOME/'cached-id-ablations/comparison.json')
    if any(r['gold_relations'] != 154 or r['papers'] != 100 for r in (tuned,reviewed,diagnostic)):
        raise ValueError('Incomplete same-cohort comparisons')
    if any(r['gold_sha256'] != initial['gold_sha256'] for r in (tuned,reviewed,diagnostic)):
        raise ValueError('Native gold differs')
    rows = []
    for p in initial['configurations']:
        rows.append({'name':p['configuration'],'family':None,'metrics':p['metrics']['relation'],'per_paper':p['per_paper']})
    for model in ('gpt-6.1-sol','gpt-6-luna'):
        for mode in ('fill_missing','revalidate_all'):
            r = diagnostic['arms'][model+':'+mode]
            family = 'sol_'+mode if model == 'gpt-6.1-sol' else None
            rows.append({'name':model+' · '+('미확정 ID 복원' if mode == 'fill_missing' else '전체 ID 재검증'),
                         'family':family,'metrics':r['metrics'],'per_paper':r['per_paper']})
    for model,r in tuned['models'].items():
        rows.append({'name':model+' · 후보 제공·학습 선택','family':'sol_candidate' if model == 'gpt-6.1-sol' else 'luna_candidate',
                     'metrics':r['metrics'],'per_paper':{p:v['relation'] for p,v in r['per_paper'].items()}})
    rows.append({'name':'세 후보 스트림 결합·원문/ID 재검증','family':'joint','metrics':reviewed['metrics'],
                 'per_paper':{p:v['relation'] for p,v in reviewed['per_paper'].items()}})
    selected = next(r for r in rows if r['family'] == choice['selected_family'])
    differences = {r['name']:{'delta':{k:None if r['metrics'][k] is None or rows[0]['metrics'][k] is None else r['metrics'][k]-rows[0]['metrics'][k]
                            for k in ('tp','fp','fn','precision','recall','f1')},
                            'paired_paper_bootstrap2000_ci95':_bootstrap(rows[0]['per_paper'],r['per_paper'],20261017)} for r in rows[5:]}
    result = {'schema':'chatgpt-optimized-pipeline-comparison-v1','papers':100,'gold_relations':154,
              'input_sha256':initial['input_sha256'],'gold_sha256':initial['gold_sha256'],
              'training_selected_family':choice['selected_family'],'training_selected_result':selected,
              'family_holdout_metrics':choice['family_holdout_metrics'],'configurations':rows,'differences_vs_v6':differences,
              'training_selection_sha256':sha((base.HOME/'overall-selection.json').read_bytes()),
              'new_completed_model_responses':460,'original_responses_reused':200,
              'evaluation_scope':'repeated exposed development, not independent final',
              'default_engine':'heuristic-v1','nutrition_accuracy':'unmeasured','sealed_test_opened':False}
    immutable(base.HOME/'optimized-comparison.json',result)
    lines = ['# ChatGPT 파이프라인 튜닝 후 동일100편 재평가','',
             '기존과 동일한 원문100편·정답154관계, 변경하지 않은 v6 유형·표준 ID 일치 평가 기준입니다. 방향성은 별도 평가입니다.', '',
             '학습 자료에서 선택한 최종 계열: **'+selected['name']+'**. 아래 개발 성능으로 계열을 바꾸거나 운영 엔진을 전환하지 않았습니다.','',
             '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |','|---|---:|---:|---:|---:|---:|---:|']
    csv_rows = [['구성','정탐','오탐','누락','정밀도','재현율','F1']]
    for row in rows:
        m = row['metrics']
        values = [row['name'],m['tp'],m['fp'],m['fn'],*['미정의' if m[k] is None else f'{100*m[k]:.2f}%' for k in ('precision','recall','f1')]]
        lines.append('| '+' | '.join(map(str,values))+' |')
        csv_rows.append(values)
    lines.extend(['','ID 복원은 원래 모델 응답의 관계 판단을 바꾸지 않는 진단입니다. 전체 ID 재검증은 올바른 기존 연결을 훼손할 수 있어 별도 집계했습니다.', '',
                  '후보 제공 방식은 공식 사전에서 자동 언급·ID를 생성해 모델에 제공했습니다. 결합 재검증은 원래 Sol 추출과 두 모델의 후보 제공 추출을 합쳐, Sol medium이 원문과 공식 ID 명칭을 다시 대조합니다. 제안에 없던 관계도 추가할 수 있고 ID·유형도 교정할 수 있습니다.', '',
                  'ID 모델은 공식 학습465편의 교차 예측으로 선택했습니다. 검증 강도와 계열 선택은 별도의 공식 관계 학습40편에서 수행했습니다. ID 모델 정밀도91.26%·재현율50.11%는 유전자와 물질을 합친 학습 내부 수치이며 영양 문헌의 유전자 정확도가 아닙니다.', '',
                  '제한된 검색 범위에서 고른 구성입니다. 학습 내부 정책 선택을 다른 fold에서 검증한 결과와 모든 후보 계열을 JSON에 보존했습니다. 원문·ID·관계 정답은 모델 입력에 넣지 않았습니다. 기존 개발100편은 반복 노출된 자료이며 공개 모델의 사전학습 중복은 불명입니다.', '',
                  '새 완료 응답460건과 이전 응답200건을 사용했습니다. 초기 통신 점검, 실패한 요청, 계정 전체 사용량은 이 집계에 포함하지 않습니다. 재현 감사와 원문 위치 검사 뒤에만 완료로 판정합니다. 기본 엔진v1과 봉인 시험은 유지했습니다. 영양학 정확도와 실제 검수 시간 절감은 미측정입니다.',''])
    (base.HOME/'optimized-comparison.md').write_text('\n'.join(lines),encoding='utf-8')
    with (base.HOME/'optimized-comparison.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        csv.writer(stream).writerows(csv_rows)
    print(json.dumps({'selected_family':choice['selected_family'],'selected_metrics':selected['metrics'],
                      'configurations':[{'name':r['name'],'metrics':r['metrics']} for r in rows]},ensure_ascii=False))


if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='report':
        report()
    else:
        select_training()
