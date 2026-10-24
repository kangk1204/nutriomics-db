import csv,io,time
from collections import Counter
from .common import *
from .preparation import prepare
from . import learning,representation
from .. import benchmarks,evaluation_v6
from ..chatgpt_evaluation import _bootstrap

ARMS=('v2','v3','v4','identity_baseline','retrieval_only','events_only','combined')

def infer_train():
    from .review import request
    request('train')

def train():
    assert (HOME/'train-inference-complete.json').exists()
    started=time.monotonic();papers=representation.load('train')
    log('training-start',papers=len(papers),gold_relations=sum(len(evaluation_v6.relation_sets(p['document'],{'relations':[]})[0]) for p in papers))
    result=learning.train_nested(papers);result['elapsed_seconds']=time.monotonic()-started
    write('selection.json',result);log('training-complete',outer=result['outer_actual'],selected=result['selected'],gate=result['training_gate_passed'])

def freeze():
    import importlib.metadata,platform
    from ..repair_v4.pipeline import verify_freeze as prior_verify
    prior_verify();selection=read(HOME/'selection.json')
    files=[HOME/'prepared.json',HOME/'audit.json',HOME/'selection.json',HOME/'targeted-cohort.json',HOME/'train-inference-complete.json',
        *sorted((HOME/'rebuilt').glob('*/*.json')),*sorted((HOME/'payloads').glob('*/*.json')),
        *sorted((HOME/'inference/train').glob('*/*/*.json'))]
    write('freeze.json',{'schema':'repair-v5-frozen','source_sha256':sources(),
        'artifacts':{str(p.relative_to(HOME)):filehash(p) for p in files},
        'index':read(HOME/'knowledge/index.json'),'v4_freeze_sha256':filehash(V4/'freeze.json'),
        'evaluator_sha256':benchmarks.evaluator_source_hashes(),'selected':selection['selected'],
        'training_gate_passed':selection['training_gate_passed'],'sealed_test_allowed':False,
        'development_gold_used_for_selection':False,'new_development_inference_allowed':selection['training_gate_passed'],
        'budget':budget_status(),'default_engine':'heuristic-v1',
        'environment':{'python':platform.python_version(),'library_threads':1,'memory_budget_gib':8,
            'packages':{name:importlib.metadata.version(name) for name in ('numpy','scipy','scikit-learn','ijson','rapidfuzz','requests')}}})
    log('frozen',gate=selection['training_gate_passed'])

def verify_freeze():
    from ..repair_v4.pipeline import verify_freeze as prior_verify
    prior_verify();frozen=read(HOME/'freeze.json')
    assert frozen['source_sha256']==sources(),'v5 동결 소스 변경'
    assert frozen['evaluator_sha256']==benchmarks.evaluator_source_hashes()
    for name,h in frozen['artifacts'].items():assert filehash(HOME/name)==h,name
    assert filehash(HOME/'knowledge/aliases.sqlite3')==frozen['index']['index_sha256']
    return frozen

def infer_development():
    frozen=verify_freeze()
    if not frozen['new_development_inference_allowed']:
        write('development-inference-skipped.json',{'reason':'training gate failed; preserve six development calls','new_calls':0,'all100_still_evaluated_from_saved_responses':True})
        log('development-inference-skipped',remaining=budget_status()['remaining']);return
    from .review import request
    request('development')

def predict():
    verify_freeze();s=read(HOME/'selection.json');model=s['model'];policy=s['selected']
    if not ((HOME/'development-inference-complete.json').exists() or (HOME/'development-inference-skipped.json').exists()):raise ValueError('개발 추가 판독 상태 미확정')
    from ..chatgpt_extraction import exact_span
    for i,p in enumerate(representation.load('development')):
        pmid=p['pmid'];values={
            'v2':read(V4/'predictions/v2'/('paper-'+pmid+'.json')),
            'v3':read(V4/'predictions/v3'/('paper-'+pmid+'.json')),
            'v4':read(V4/'predictions/combined'/('paper-'+pmid+'.json')),
            'identity_baseline':read(V4/'predictions/identity_only'/('paper-'+pmid+'.json')),
            'retrieval_only':learning.project(p,model,policy,'retrieval_only'),
            'events_only':learning.project(p,model,policy,'events_only'),
            'combined':learning.project(p,model,policy)}
        for arm,v in values.items():
            for r in v['relations']:
                assert all(exact_span(p['document']['text'],span) for span in r['evidence']+r['endpoints'])
            write('predictions/'+arm+'/paper-'+pmid+'.json',{**v,'source_sha256':sha(p['document']['text']),'review_status':'unreviewed'})
        if i%20==0:log('prediction-progress',papers=i+1)
    write('predictions-complete.json',{'papers':100,'arms':list(ARMS),'all700_saved_before_gold':True,'freeze_sha256':filehash(HOME/'freeze.json')})

def evaluate():
    verify_freeze();assert (HOME/'predictions-complete.json').exists()
    from ..repair_v3.runner import gold
    docs,gold_sha=gold();assert len(docs)==100
    results={};correct={};allgold=set();errors={};idcounts=Counter();gaps=Counter();idchanges=Counter();old45_records=[]
    for d in docs:allgold|=evaluation_v6.relation_sets(d,{'relations':[]})[0]
    assert len(allgold)==154
    for arm in ARMS:
        preds={d['pmid']:read(HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')) for d in docs}
        results[arm]=native_metrics(docs,preds)
        observed=set().union(*(evaluation_v6.relation_sets(d,preds[d['pmid']])[1] for d in docs));correct[arm]=observed&allgold
        from scripts.analyze_chatgpt_repair_v4 import error_class
        errors[arm]=dict(Counter(error_class(next(d for d in docs if d['pmid']==k[0]),k) for k in observed-allgold))
    for arm,counts in [('v2',(64,64,90)),('v3',(63,57,91)),('v4',(50,54,104)),('identity_baseline',(65,63,89))]:
        assert tuple(results[arm]['metrics'][k] for k in ('tp','fp','fn'))==counts
    transitions={}
    for arm in ARMS:
        transitions[arm]={'retained65':len(correct[arm]&correct['identity_baseline']),
            'lost65':len(correct['identity_baseline']-correct[arm]),'new':len(correct[arm]-correct['identity_baseline']),
            'paired_bootstrap2000_ci95_vs_id_baseline':_bootstrap({p:v['relation'] for p,v in results['identity_baseline']['per_paper'].items()},
                {p:v['relation'] for p,v in results[arm]['per_paper'].items()},SEED)}
    # Candidate coverage is measured over ALL annotated mentions, not an error whitelist.
    byid={d['pmid']:d for d in docs}
    from ..repair_v4 import learning as old_learning,representation as old_rep
    current_model=read(HOME/'selection.json')['model'];old_policy=read(V4/'selection.json')['model']['identity']
    for p in representation.load('development'):
        d=byid[p['pmid']]
        previous_mentions={old_rep.mention_key(m):old_learning.choose(m,old_policy)['selected_id'] for m in p['_legacy']['mentions']}
        for m in p['mentions']:
            truth=old_rep.strict_ids(m,d)
            decision=old_learning.choose(m,current_model['identity']);old_id=previous_mentions.get(old_rep.mention_key(m));new_id=decision['selected_id']
            prefix=m['kind']+'_';idchanges[prefix+'mentions']+=1;idchanges[prefix+'selected']+=int(new_id is not None);idchanges[prefix+'strict_correct']+=int(new_id is not None and new_id in truth)
            idchanges[prefix+'nil']+=int(new_id is None)
            if new_id is not None and new_id!=old_id:
                idchanges[prefix+'new_or_changed']+=1;idchanges[prefix+'new_or_changed_strict_correct']+=int(new_id in truth)
            if old_id and truth:
                idchanges[prefix+'wrong_existing_corrected']+=int(old_id not in truth and new_id in truth)
                idchanges[prefix+'correct_existing_damaged']+=int(old_id in truth and new_id not in truth)
            if not truth:continue
            idcounts[prefix+'exact_mentions']+=1
            menu=[r['native_id'] for r in m['candidates']]
            for k in (10,50,200):idcounts[prefix+'recall'+str(k)+'_hits']+=int(bool(truth&set((menu if k<200 else m.get('candidate_pool200',menu))[:k])))
            idcounts[prefix+'normalization_rank10_hits']+=int(bool(truth&set(decision['top10'])))
        for oldgap in read(ROOT/'runtime/chatgpt-repair-v4-postscore/diagnostics.json')['relation_records']:
            if oldgap['key'][0]!=p['pmid'] or not oldgap['first_observed_gap'].endswith('_id_not_retrieved'):continue
            kind=oldgap['first_observed_gap'].split('_id_')[0];wanted=next(x for x in oldgap['key'][1] if x.startswith(('C','D'))==(kind=='chemical'))
            hit=any(m['kind']==kind and wanted in m.get('candidate_pool200',[]) for m in p['mentions'])
            gaps[kind+('_now_in200' if hit else '_still_absent200')]+=1
            key=(oldgap['key'][0],tuple(oldgap['key'][1]),oldgap['key'][2])
            hit50=any(m['kind']==kind and wanted in {r['native_id'] for r in m['candidates']} for m in p['mentions'])
            stage='matched' if key in correct['combined'] else 'in50_not_final' if hit50 else 'rank51_to200_not_ranked' if hit else 'absent200'
            old45_records.append({'key':oldgap['key'],'kind':kind,'now_in50':hit50,'now_in200':hit,'final_stage':stage})
    additional=budget_status()['attempts']-read(HOME/'audit.json')['budget_at_start']['attempts'];assert additional<=25
    m=results['combined']['metrics'];goal=m['tp']>=65 and m['recall']>=FLOOR and m['precision']>=.85
    report={'papers':100,'gold_relations':154,'gold_sha256':gold_sha,'arms':results,'transitions':transitions,
        'goal_met':goal,'additional_calls':additional,'budget':budget_status(),'error_classes':errors,
        'candidate_coverage':dict(idcounts),'previous45_first_gap_retrieval':dict(gaps),'previous45_records':old45_records,
        'id_decision_counts':dict(idchanges),'id_decision_reference':'frozen v4 mention selections; relation-anchor NIL fallback is separate',
        'training_gate_passed':read(HOME/'selection.json')['training_gate_passed'],
        'configuration_selected_from_development':False,'sealed_test_opened':False,'nutrition_accuracy':'unmeasured','default_engine':'heuristic-v1'}
    write('comparison.json',report)
    labels={'v2':'v2','v3':'v3','v4':'v4 결합','identity_baseline':'ID 전용 기준선','retrieval_only':'v5 ID 검색·교정','events_only':'v5 이벤트·기존 ID 고정','combined':'v5 최종'}
    lines=['# 관계 추출 v5 실제 추가 추론·개발 평가','',
        '같은 100편·정답 154건의 반복 노출 개발 비교다. 원시 ID·유형 평가기를 유지했다.','',
        '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |','|---|---:|---:|---:|---:|---:|---:|']
    for arm in ARMS:
        v=results[arm]['metrics'];pct=lambda x:'정의 불가' if x is None else format(x,'.2%')
        lines.append(f"| {labels[arm]} | {v['tp']} | {v['fp']} | {v['fn']} | {pct(v['precision'])} | {pct(v['recall'])} | {pct(v['f1'])} |")
    t=transitions['combined'];lines+=['',f"판정: {'합격' if goal else '미달'}. ID 전용 정탐 65건 중 유지 {t['retained65']}·상실 {t['lost65']}·신규 {t['new']}건.",
        f"추가 요청 {additional}/25회, 남은 원래 예산 {report['budget']['remaining']}회. 학습 추가 응답은 {len(list((HOME/'inference/train').glob('*/valid/*.json')))}개, 개발 추가 응답은 {len(list((HOME/'inference/development').glob('*/valid/*.json')))}개다.",
        f"학습 기준 통과 여부: {report['training_gate_passed']}. 미달 시 개발 추가 6회는 보존하고 저장 응답으로 100편 전체를 처리했다.",
        '기존 응답을 새 입력 응답으로 재표시하지 않았다. 기본 엔진 v1과 봉인 시험은 유지했다.','',
        '후보 검색 지표와 기존 45건의 후보 복원은 comparison.json에 기록했다. 후보 포함은 관계 정탐 복원을 뜻하지 않는다.',
        '유형·ID 교정은 원문 근거를 보존하며 이전 엔진의 사람 승인을 복사하지 않았다.']
    text='\n'.join(lines)+'\n';path=HOME/'comparison.md'
    if path.exists() and path.read_text(encoding='utf8')!=text:raise ValueError('보고서 변경')
    path.write_text(text,encoding='utf8')
    for arm in ARMS:
        preds={d['pmid']:read(HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')) for d in docs}
        assert native_metrics(docs,preds)==results[arm]
    write('completion-audit.json',{'offline_rescore_identical':True,'previous_versions_preserved':True,'source_offsets_exact':True,
        'calls':additional,'budget':budget_status(),'production_modified':False,'goal_met':goal})
    log('evaluation-complete',goal_met=goal,metrics={a:v['metrics'] for a,v in results.items()})

def resume():
    steps=[('prepared.json',prepare),('train-inference-complete.json',infer_train),('selection.json',train),('freeze.json',freeze)]
    for receipt,action in steps:
        if not (HOME/receipt).exists():log('stage-start',receipt=receipt);action()
    if not ((HOME/'development-inference-complete.json').exists() or (HOME/'development-inference-skipped.json').exists()):infer_development()
    if not (HOME/'predictions-complete.json').exists():predict()
    if not (HOME/'completion-audit.json').exists():evaluate()
