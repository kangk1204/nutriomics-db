"""v5-r1: carry ALL fold-local existing ID assignments into the editing policy.

The original frozen v5 experiment is immutable. No prompts, calls, thresholds or
benchmark mappings change. Reference IDs are inputs, never learned ID-number features.
"""
import copy,time
from pathlib import Path
from collections import Counter
from .repair_v5 import common as common,representation as rep,learning as editor
from .repair_v4 import learning as ids,representation as oldrep
from .splits_v7 import grouped_folds
from .evaluation_v6 import relation_sets
from .chatgpt_evaluation import _bootstrap
from . import benchmarks

ROOT=common.ROOT;HOME=ROOT/'runtime/chatgpt-repair-v5-r1';V5=common.HOME;SEED=common.SEED;FLOOR=common.FLOOR
read=common.read;write_file=common.immutable;sha=common.sha;canonical=common.canonical;filehash=common.filehash
_BASE={};_POLICIES={}

def write(name,value):write_file(HOME/name,value);return value
def log(stage,**values):print(canonical({'stage':stage,**values}),flush=True)
def source_files():
    files=[Path(__file__),ROOT/'scripts/chatgpt_repair_v5_revision.py',ROOT/'tests/test_repair_v5_revision.py']
    return {str(p.relative_to(ROOT)):filehash(p) for p in files}

def reference_choices(p,policy):
    return {oldrep.mention_key(m):ids.choose(m,policy)['selected_id'] for m in p['_legacy']['mentions']}

def bind_reference(p,reference):
    """A fresh view; do not mutate cached sources or confuse missing relations with NIL."""
    view={**p};mentions=[]
    for m in p['mentions']:
        identifier=reference.get(oldrep.mention_key(m))
        previous=[identifier] if identifier is not None else list(m['old_ids'])
        mentions.append({**m,'old_ids':previous,'reference_id':identifier})
    view['mentions']=mentions
    return view

def base_policy(papers):
    token=tuple(sorted(p['pmid'] for p in papers))
    if token not in _BASE:
        _BASE[token]=ids.calibrate_identity([p['_legacy'] for p in papers],1.)
        if len(_BASE)>64:_BASE.pop(next(iter(_BASE)))
    return _BASE[token]

def views(papers,base):return [bind_reference(p,reference_choices(p,base)) for p in papers]

def rank_fit(bound,c=1.):
    return ids.fit_ranker(bound,c)

def calibrate(papers):
    token=tuple(sorted(p['pmid'] for p in papers))
    if token in _POLICIES:return _POLICIES[token]
    folds=grouped_folds([p['document'] for p in papers],folds=3,salt='repair-v4-id-policy')
    records={'new':[],'change':[]}
    for f in range(3):
        train=[p for p in papers if folds[p['pmid']]!=f];held=[p for p in papers if folds[p['pmid']]==f]
        baseline=base_policy(train);training=views(train,baseline);testing=views(held,baseline)
        rank=rank_fit(training);validity=ids.fit_validity(training,1.)
        for original,bound in zip(train,training):
            if '_rank_examples' in bound:original['_rank_examples']=bound['_rank_examples']
        for p in testing:
            for m in p['mentions']:
                selected,priority,gap,_=ids.proposal(m,rank,validity,p['document']['text'])
                if selected is None or selected in m['old_ids']:continue
                kind='change' if m['old_ids'] else 'new'
                records[kind].append((priority,gap,int(selected in oldrep.strict_ids(m,p['document'])),p['sample_weight']))
    choices={}
    for category,items in records.items():
        best=None
        for margin in (0.,.25,.5,1.,2.):
            rows=sorted((r for r in items if r[1]>=margin),key=lambda r:-r[0]);correct=wrong=wc=ww=0.
            for i,r in enumerate(rows):
                correct+=r[2];wrong+=1-r[2];wc+=r[2]*r[3];ww+=(1-r[2])*r[3]
                if i+1<len(rows) and rows[i+1][0]==r[0]:continue
                precision=wc/(wc+ww)
                row={'threshold':r[0],'margin':margin,'correct':int(correct),'wrong':int(wrong),'weighted_precision':precision,
                    'enabled':precision>=.9 and correct>0,'full_strict_denominator_including_unmatched':True}
                if best is None or (row['enabled'],row['correct'] if row['enabled'] else 0,row['weighted_precision'])>(best['enabled'],best['correct'] if best['enabled'] else 0,best['weighted_precision']):best=row
        choices[category]=best if best and best['enabled'] else {'enabled':False,'threshold':1.000001,'margin':0.,'correct':0,'wrong':0,'reason':'no 90 percent OOF policy'}
    baseline=base_policy(papers);bound=views(papers,baseline)
    identity={**choices,'ranker':rank_fit(bound),'validity':ids.fit_validity(bound,1.),'calibration_papers':[p['pmid'] for p in papers],
        'reference_assignments_fitted_inside_each_calibration_fold':True,'existing_id_state_includes_nonrelation_mentions':True}
    for original,p in zip(papers,bound):
        if '_rank_examples' in p:original['_rank_examples']=p['_rank_examples']
    _POLICIES[token]=(baseline,identity)
    if len(_POLICIES)>20:_POLICIES.pop(next(iter(_POLICIES)))
    return _POLICIES[token]

def candidate_rows(p,identity,baseline,lock=False):
    bound=bind_reference(p,reference_choices(p,baseline))
    return rep.candidates(bound,identity,baseline,lock_ids=lock)

def fit(papers,c):
    baseline,identity=calibrate(papers);x=[];y=[];weights=[];sx=[];sy=[];sw=[];ix=[];iy=[];iw=[]
    for p in papers:
        for r in candidate_rows(p,identity,baseline):
            x.append(r['features']);y.append(r['strict_target']);weights.append(p['sample_weight'])
            if r['semantic_target'] is not None:
                sx.append({k:v for k,v in r['features'].items() if not k.startswith(('chemical_','gene_'))});sy.append(r['semantic_target']);sw.append(p['sample_weight'])
            if r['id_target'] is not None:ix.append(r['features']);iy.append(r['id_target']);iw.append(p['sample_weight'])
    return {'C':c,'identity':identity,'legacy_identity':baseline,'acceptance':ids.fit_head(x,y,c,weights),
        'semantics':ids.fit_head(sx,sy,c,sw),'id_correctness':ids.fit_head(ix,iy,c,iw),
        'existing_state_corrected':True,'semantic_ID_targets_separate':True}

def scored(papers,model,lock=False):
    rows={};base={}
    for p in papers:
        values=candidate_rows(p,model['identity'],model['legacy_identity'],lock)
        for r in values:
            r['priority']=editor.predict_binary(model['acceptance'],r['features'])
            r['semantic_priority']=editor.predict_binary(model['semantics'],r['features'])
            r['id_priority']=editor.predict_binary(model['id_correctness'],r['features'])
        rows[p['pmid']]=values;base[p['pmid']]=rep.anchor_rows(p,model['legacy_identity'])
    return rows,base

def select(papers,salt):
    folds=grouped_folds([p['document'] for p in papers],folds=3,salt=salt);choices=[]
    for c in (.1,1.,10.):
        rows={};base={}
        for f in range(3):
            train=[p for p in papers if folds[p['pmid']]!=f];held=[p for p in papers if folds[p['pmid']]==f]
            r,b=scored(held,fit(train,c));rows.update(r);base.update(b)
        metric=editor.counts(papers,base,{'mode':'baseline'});floor=max(FLOOR,metric['recall'])
        if c==.1:choices.append({'mode':'baseline','C':1.,'weighted':metric,'actual':editor.counts(papers,base,{'mode':'baseline'},False),'recall_floor':floor})
        for a in (0.,.05,.15,.3,.5,.7,.85,.95,.99):
            for n in (.5,.7,.85,.95,.99,1.000001):
                for margin in (0.,.1,.2):
                    rule={'mode':'editor','C':c,'anchor_threshold':a,'new_threshold':n,'relabel_margin':margin}
                    choices.append({**rule,'weighted':editor.counts(papers,rows,rule),'actual':editor.counts(papers,rows,rule,False),'recall_floor':floor})
        log('inner-selection',C=c,papers=len(papers),best=max([r for r in choices if r['C']==c],key=lambda r:editor.objective(r['weighted'],r['recall_floor'])))
    chosen=max(choices,key=lambda r:(editor.objective(r['weighted'],r['recall_floor']),r['mode']=='baseline',-r['C']))
    return chosen,choices

def train():
    from .repair_v5.pipeline import verify_freeze
    verify_freeze();papers=rep.load('train');outer=[];predictions={};baseline_all={};start=time.monotonic()
    folds=grouped_folds([p['document'] for p in papers],folds=5,salt='repair-v5-outer')
    for f in range(5):
        training=[p for p in papers if folds[p['pmid']]!=f];held=[p for p in papers if folds[p['pmid']]==f]
        rule,_=select(training,'repair-v5-inner-'+str(f));model=fit(training,rule['C']);rows,base=scored(held,model)
        out=base if rule['mode']=='baseline' else {k:[{'entity1':r['ids'][0],'entity2':r['ids'][1],'predicate':r['native']} for r in editor.choose_rows(v,rule)] for k,v in rows.items()}
        predictions.update(out);baseline_all.update(base);m=editor.counts(held,out,{'mode':'baseline'})
        outer.append({'fold':f,'selected':rule,'weighted':m,'held_pmids':[p['pmid'] for p in held]});log('outer-complete',fold=f,metrics=m)
    selected,grid=select(papers,'repair-v5-final');m=editor.counts(papers,predictions,{'mode':'baseline'});bm=editor.counts(papers,baseline_all,{'mode':'baseline'})
    write('selection.json',{'selected':selected,'grid':grid,'model':fit(papers,selected['C']),'outer_folds':outer,
        'outer_actual':editor.counts(papers,predictions,{'mode':'baseline'},False),'outer_weighted':m,'baseline_weighted':bm,
        'training_gate_passed':m['precision']>=.85 and m['recall']>=max(FLOOR,bm['recall']),
        'elapsed_seconds':time.monotonic()-start,'thresholds_unchanged_from_v5':True,'new_model_calls':0,
        'development_driven_bug_repair':True,'threshold_selection_uses_training_only':True})
    log('training-complete',selected=selected,outer=m)

def freeze():
    from .repair_v5.pipeline import verify_freeze
    verify_freeze();s=read(HOME/'selection.json')
    write('freeze.json',{'source_sha256':source_files(),'original_v5_freeze_sha256':filehash(V5/'freeze.json'),
        'original_v5_selection_sha256':filehash(V5/'selection.json'),'selection_sha256':filehash(HOME/'selection.json'),
        'old_comparison_sha256':filehash(V5/'comparison.json'),'evaluator_sha256':benchmarks.evaluator_source_hashes(),
        'selected':s['selected'],'new_model_calls':0,'same_saved16_extra_responses':True,
        'no_development_scores_used_to_choose_thresholds':True,'default_engine':'heuristic-v1'})

def verify():
    from .repair_v5.pipeline import verify_freeze
    verify_freeze();f=read(HOME/'freeze.json');assert source_files()==f['source_sha256']
    assert filehash(HOME/'selection.json')==f['selection_sha256'];assert filehash(V5/'comparison.json')==f['old_comparison_sha256']
    assert benchmarks.evaluator_source_hashes()==f['evaluator_sha256']

def project(p,model,rule,lock=False,retrieval_only=False):
    rows,base=scored([p],model,lock)
    if rule['mode']=='baseline' and not retrieval_only:return {'relations':base[p['pmid']],'final_mode':'baseline'}
    chosen=[r for r in rows[p['pmid']] if r['anchor']] if retrieval_only else editor.choose_rows(rows[p['pmid']],rule)
    relations=[]
    for r in chosen:
        v={'entity1':r['ids'][0],'entity2':r['ids'][1],'predicate':r['native'],'endpoints':r['endpoints'],'evidence':r['evidence'],
            'scientific_events':r['events'],'priority':r['priority'],'priority_is_accuracy_probability':False,'review_status':'unreviewed',
            'role_direction':r.get('role_direction','unknown'),'identity_trace':r.get('identity_trace')}
        if r.get('subject'):v['subject']=r['subject']
        relations.append(v)
    return {'relations':relations,'all_reference_IDs_carried':True}

ARMS=('v2','v3','v4','identity_baseline','v5_initial','retrieval_only','events_only','combined')

def predict():
    verify();s=read(HOME/'selection.json');model=s['model'];rule=s['selected']
    from .chatgpt_extraction import exact_span
    for i,p in enumerate(rep.load('development')):
        pmid=p['pmid'];out={a:read(V5/'predictions'/a/('paper-'+pmid+'.json')) for a in ('v2','v3','v4','identity_baseline')}
        out['v5_initial']=read(V5/'predictions/combined'/('paper-'+pmid+'.json'))
        out.update(retrieval_only=project(p,model,rule,retrieval_only=True),events_only=project(p,model,rule,lock=True),combined=project(p,model,rule))
        for arm,value in out.items():
            for r in value['relations']:assert all(exact_span(p['document']['text'],q) for q in r['endpoints']+r['evidence'])
            write('predictions/'+arm+'/paper-'+pmid+'.json',value)
        if i%20==0:log('prediction-progress',papers=i+1)
    write('predictions-complete.json',{'papers':100,'predictions':800,'all_saved_before_scoring':True})

def evaluate():
    verify();assert (HOME/'predictions-complete.json').exists()
    from .repair_v3.runner import gold
    docs,h=gold();truth=set().union(*(relation_sets(d,{'relations':[]})[0] for d in docs));assert len(truth)==154
    result={};correct={};transitions={};id_counts=Counter()
    for arm in ARMS:
        pred={d['pmid']:read(HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')) for d in docs}
        result[arm]=common.native_metrics(docs,pred);correct[arm]=set().union(*(relation_sets(d,pred[d['pmid']])[1] for d in docs))&truth
    for arm in ARMS:
        transitions[arm]={'retained65':len(correct[arm]&correct['identity_baseline']),'lost65':len(correct['identity_baseline']-correct[arm]),'new':len(correct[arm]-correct['identity_baseline']),
            'paired_bootstrap2000_ci95':_bootstrap({k:v['relation'] for k,v in result['identity_baseline']['per_paper'].items()},{k:v['relation'] for k,v in result[arm]['per_paper'].items()},SEED)}
    s=read(HOME/'selection.json');model=s['model'];byid={d['pmid']:d for d in docs}
    for p in rep.load('development'):
        reference=reference_choices(p,model['legacy_identity']);bound=bind_reference(p,reference)
        for m in bound['mentions']:
            old=m['reference_id'];new=ids.choose(m,model['identity'])['selected_id'];gold_ids=oldrep.strict_ids(m,byid[p['pmid']]);prefix=m['kind']+'_'
            id_counts[prefix+'selected']+=int(new is not None);id_counts[prefix+'strict_correct']+=int(new in gold_ids if new else False)
            if old and gold_ids:id_counts[prefix+'correct_existing_damaged']+=int(old in gold_ids and new not in gold_ids)
            if old and gold_ids:id_counts[prefix+'wrong_existing_corrected']+=int(old not in gold_ids and new in gold_ids)
            if new is not None and new!=old:id_counts[prefix+'new_or_changed']+=1;id_counts[prefix+'new_or_changed_strict_correct']+=int(new in gold_ids)
    m=result['combined']['metrics'];goal=m['tp']>=65 and m['recall']>=FLOOR and m['precision']>=.85
    calls=common.budget_status()['attempts']-read(V5/'audit.json')['budget_at_start']['attempts'];assert calls<=25
    report={'papers':100,'gold_relations':154,'gold_sha256':h,'arms':result,'transitions':transitions,'id_decisions':dict(id_counts),
        'goal_met':goal,'total_v5_calls':calls,'revision_calls':0,'budget':common.budget_status(),'selected':s['selected'],
        'development_driven_bug_repair':True,'training_only_policy_selection':True,'default_engine':'heuristic-v1','sealed_test_opened':False}
    write('comparison.json',report)
    labels={'v2':'v2','v3':'v3','v4':'v4 결합','identity_baseline':'ID 전용 기준선','v5_initial':'v5 최초 동결 구성','retrieval_only':'v5-r1 ID 상태 수정','events_only':'v5-r1 이벤트·ID 고정','combined':'v5-r1 최종'}
    lines=['# v5-r1 기존 ID 상태 전달 오류 수정과 재평가','',
        '최초 동결 v5는 보존했다. 기존 자동 정규화 ID를 신규로 취급한 오류를 일반 규칙으로 수정했다. 개발 진단에서 발견한 구현 수정이며 독립 시험으로 주장하지 않는다.',
        '추가 모델 호출은 0회다. 저장된 추가 학습 응답 16개와 같은 학습·개발 자료를 사용하고, 수치 임계값·C·평가기·정답 분모는 바꾸지 않았다.','',
        '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |','|---|---:|---:|---:|---:|---:|---:|']
    for arm in ARMS:
        v=result[arm]['metrics'];pct=lambda x:'정의 불가' if x is None else format(x,'.2%')
        lines.append(f"| {labels[arm]} | {v['tp']} | {v['fp']} | {v['fn']} | {pct(v['precision'])} | {pct(v['recall'])} | {pct(v['f1'])} |")
    t=transitions['combined'];lines+=['',f"판정: {'합격' if goal else '미달'}. 기존 정탐 유지 {t['retained65']}·상실 {t['lost65']}·신규 {t['new']}건.",
        f"v5 전체 실제 추가 요청 {calls}/25회. 수정본 추가 요청 0회. 잔여 {report['budget']['remaining']}회.",
        '기준선 유지 모드가 선택되면 신규 편집기를 채택한 개선으로 해석하지 않는다. 모델·선택 정책은 훈련 내부에서 동결한 후 100편 전부를 처리했다.',
        '원문 위치, 이전 예측 보존, 저장 결과 재채점 동일을 확인했다. 기본 엔진 v1과 봉인 시험은 유지했다.','']
    path=HOME/'comparison.md';text='\n'.join(lines)
    if path.exists() and path.read_text(encoding='utf8')!=text:raise ValueError('수정 보고서 변경')
    path.write_text(text,encoding='utf8')
    for arm in ARMS:
        pred={d['pmid']:read(HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')) for d in docs};assert common.native_metrics(docs,pred)==result[arm]
    write('completion-audit.json',{'offline_rescore_identical':True,'old_v5_preserved':True,'new_model_calls':0,'total_v5_calls':calls,'goal_met':goal})
    log('evaluation-complete',goal=goal,metrics={a:v['metrics'] for a,v in result.items()})

def resume():
    HOME.mkdir(parents=True,exist_ok=True)
    for receipt,action in [('selection.json',train),('freeze.json',freeze),('predictions-complete.json',predict),('completion-audit.json',evaluate)]:
        if not (HOME/receipt).exists():log('stage-start',receipt=receipt);action()
