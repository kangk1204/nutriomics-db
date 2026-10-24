"""Audited batch stages. Scoring is opened only after every prediction is durable."""
from collections import Counter,defaultdict
import csv
import io
import time
from .common import *
from . import representation as rep,learning
from .. import benchmarks,evaluation_v6
from ..chatgpt_evaluation import _bootstrap
from ..chatgpt_extraction import exact_span

ARMS=('v2','v3','sol_only','luna_only','identity_only','events_locked_ids','combined')

def integrity():
    from .. import chatgpt_tuning as old
    from ..splits_v7 import text_key
    audit=read(old.HOME/'inputs/training-audit.json')
    official_path=old.HOME/'inputs/official-bioredirect-train.json'
    earlier=read(PRIOR/'data-audit.json')
    if sha(official_path.read_bytes())!=earlier['official_train_sha256']:
        raise ValueError('공식 학습 원본 변경')
    protected_ids=set(audit['protected_pmids'])|{d['pmid'] for d in data('development')}
    protected_text=set(audit['protected_text_hashes'])|{text_key(d['text']) for d in data('development')}
    protected_pmc=set(audit['protected_pmcids'])
    official=read(official_path)
    expected=[d for d in official['documents'] if d['pmid'] not in protected_ids
              and text_key(d['text']) not in protected_text
              and not (d.get('pmcid') and d['pmcid'] in protected_pmc)]
    if canonical(expected)!=canonical(data('train')):
        raise ValueError('감사된 공식 학습 분할과 입력 불일치')
    overlaps=[];seen=set();duplicates=[]
    for d in data('train'):
        if d['pmid'] in protected_ids or text_key(d['text']) in protected_text or d.get('pmcid') and d['pmcid'] in protected_pmc:
            overlaps.append(d['pmid'])
        token=text_key(d['text'])
        if token in seen:duplicates.append(d['pmid'])
        seen.add(token)
    if overlaps:raise ValueError('학습·보호 자료 중복')
    for name,digest in read(HOME/'audit.json')['preserved_sha256'].items():
        if sha((ROOT/name).read_bytes())!=digest:raise ValueError('이전 버전 변경: '+name)
    for name,digest in read(V3/'audit.json')['preserved_v2_sha256'].items():
        if sha((ROOT/name).read_bytes())!=digest:raise ValueError('v2 소스·결과 변경: '+name)
    return {'protected_overlap':overlaps,'internal_normalized_text_duplicates':duplicates,
            'training_source_sha256':sha((PRIOR/'inputs/train.json').read_bytes()),
            'development_source_sha256':sha((PRIOR/'inputs/development-sources.json').read_bytes()),
            'pmcid_missing':sum(not d.get('pmcid') for d in data('train')),
            'pretraining_overlap':'unknown','sealed_labels_opened':False}

def load(split):
    cohort=read(HOME/'cohort.json')
    ids=cohort['pmids'] if split=='train' else [d['pmid'] for d in data('development')]
    documents={d['pmid']:d for d in data(split)}
    result=[]
    for pmid in ids:
        path=HOME/'rebuilt'/split/('paper-'+pmid+'.json')
        if path.exists():
            p=read(path);p['document']=documents[pmid]
            for m in p['mentions']:m['_source']=p['document']['text']
        else:
            p=rep.build(pmid,split)
            stored={k:v for k,v in p.items() if k not in ('document',)}
            stored['mentions']=[{k:v for k,v in m.items() if k!='_source'} for m in p['mentions']]
            write('rebuilt/'+split+'/paper-'+pmid+'.json',stored)
        p['sample_weight']=cohort['weights'][pmid] if split=='train' else 1.
        result.append(p)
    return result

def train():
    if not (HOME/'train-inference-complete.json').exists():
        raise ValueError('학습 400개 유효 응답 완료 후 학습 가능')
    write('data-integrity.json',integrity())
    started=time.monotonic();papers=load('train')
    log('training-start',papers=len(papers),gold_relations=sum(len(evaluation_v6.relation_sets(p['document'],{'relations':[]})[0]) for p in papers))
    result=learning.train_nested(papers)
    result['elapsed_seconds']=time.monotonic()-started
    write('selection.json',result)
    log('training-complete',outer=result['outer_metrics'],weighted=result['outer_weighted_estimate'],selected=result['selected'])

def freeze():
    import platform,importlib.metadata
    selected=read(HOME/'selection.json')
    paths=[HOME/'cohort.json',HOME/'audit.json',HOME/'canonical-inputs.json',HOME/'data-integrity.json',HOME/'selection.json',HOME/'train-inference-complete.json',
           *sorted((PAYLOAD_HOME/'train').glob('*.json')),
           *sorted((HOME/'canonical-mentions/train').glob('*.json')),
           *sorted((HOME/'rebuilt/train').glob('*.json')),
           *sorted((INFERENCE_HOME/'train').glob('*/*/*.json'))]
    write('freeze.json',{'schema':'repair-v4-freeze','source_sha256':source_files(),
        'artifact_sha256':{str(p.relative_to(HOME)):sha(p.read_bytes()) for p in paths},
        'evaluator_sha256':benchmarks.evaluator_source_hashes(),'selected':selected['selected'],
        'official_dictionary_sha256':sha((ROOT/'runtime/chatgpt-inputs/official-id-dictionary.json.gz').read_bytes()),
        'training_gate_passed':selected['training_gate_passed'],
        'diagnostic_development_evaluation_allowed':True,'sealed_test_allowed':False,
        'selection_before_development_requests':not (HOME/'development-inference-complete.json').exists(),
        'environment':{'python':platform.python_version(),
            'packages':{p:importlib.metadata.version(p) for p in ['numpy','scipy','scikit-learn','requests']},'library_threads':1},
        'heads_trained_only_on_reviewed200':'avoids review-availability leakage; all549 CG gold relations retained',
        'default_engine':'heuristic-v1','development_labels_used_for_selection':False})
    log('frozen',gate_passed=selected['training_gate_passed'],selected=selected['selected'])

def verify_freeze():
    frozen=read(HOME/'freeze.json')
    if source_files()!=frozen['source_sha256']:raise ValueError('동결 v4 소스 변경')
    if benchmarks.evaluator_source_hashes()!=frozen['evaluator_sha256']:raise ValueError('평가기 변경')
    if sha((ROOT/'runtime/chatgpt-inputs/official-id-dictionary.json.gz').read_bytes())!=frozen['official_dictionary_sha256']:
        raise ValueError('공식 ID 사전 변경')
    for name,digest in frozen['artifact_sha256'].items():
        if sha((HOME/name).read_bytes())!=digest:raise ValueError('동결 파일 변경: '+name)
    return frozen

def raw_model(p,model):
    bykey={m['key']:m for m in p['mentions']};relations={}
    for e in p['events']:
        if e['origin']!=model or not e['supported_types']:continue
        endpoints=[bykey[e[k]] for k in ('chemical','gene')]
        ids=[]
        for m in endpoints:
            review=m['reviews'].get(model,{})
            selected=review.get('selected_id')
            ids.append(selected if review.get('eligible') and review.get('valid_id_evidence') and selected in {r['native_id'] for r in m['candidates']} else None)
        if not all(ids):continue
        native=e['native_type']
        r={'entity1':ids[0],'entity2':ids[1],'predicate':native,
           'endpoints':[{k:m[k] for k in ('kind','text','start','end')} for m in endpoints],
           'evidence':e['evidence'],'evidence_event_key':e['key'],'review_status':'unreviewed',
           'priority_is_accuracy_probability':False,**{k:e[k] for k in ('observation','role_direction','negated','speculative','non_significant')}}
        if e['role_direction'] in ('chemical_to_gene','gene_to_chemical'):r['subject']=ids[int(e['role_direction']=='gene_to_chemical')]
        relations[(*ids,native)]=r
    return {'relations':[relations[k] for k in sorted(relations)]}

def identity_arm(p,policy):
    current=baseline(p['pmid'],'development')
    chosen={rep.mention_key(m):learning.choose(m,policy) for m in p['mentions']}
    out=[]
    for r in current['relations']:
        ids=[chosen.get(rep.mention_key(m),{}).get('selected_id') or r['entity1' if i==0 else 'entity2'] for i,m in enumerate(r['endpoints'])]
        new={**r,'entity1':ids[0],'entity2':ids[1]}
        if r.get('subject'):new['subject']=ids[int(r['subject']==r['entity2'])]
        out.append(new)
    return {'relations':out}

def predict():
    verify_freeze()
    if not (HOME/'development-inference-complete.json').exists():raise ValueError('개발 200개 유효 응답 필요')
    selection=read(HOME/'selection.json');model=selection['model'];threshold=selection['selected']['threshold']
    for p in load('development'):
        values={'v2':baseline(p['pmid'],'development'),
                'v3':read(V3/'predictions/combined'/('paper-'+p['pmid']+'.json')),
                'sol_only':raw_model(p,'gpt-6.1-sol'),'luna_only':raw_model(p,'gpt-6-luna'),
                'identity_only':identity_arm(p,model['identity']),
                'events_locked_ids':learning.infer([p],model,threshold,lock=True)[p['pmid']],
                'combined':learning.infer([p],model,threshold)[p['pmid']]}
        for arm,value in values.items():
            for r in value['relations']:
                if any(not exact_span(p['document']['text'],s) for s in r['evidence']+r['endpoints']):
                    raise ValueError('원문 위치 불일치')
            value.update(source_sha256=p['source_sha256'],review_response_sha256=p['review_response_sha256'])
            write('predictions/'+arm+'/paper-'+p['pmid']+'.json',value)
    write('prediction-complete.json',{'papers':100,'arms':list(ARMS),'freeze_sha256':sha((HOME/'freeze.json').read_bytes()),
        'gold_read_in_predict':False,'valid_model_responses':600,'all_source_offsets_exact':True})
    log('prediction-complete',papers=100,arms=list(ARMS))

def identity_report(papers,policy):
    counts=defaultdict(Counter);records=[]
    for p in papers:
        for m in p['mentions']:
            k=m['kind'];c=counts[k];truth=rep.strict_ids(m,p['document'])
            aligned=rep.aligned_ids(m,p['document']);choice=learning.choose(m,policy);new=choice['selected_id'];old=set(m['old_ids'])
            c['automatic_mentions']+=1;c['selected']+=int(new is not None);c['nil']+=int(new is None)
            c['source_verified_kind_hypotheses']+=int(m.get('source')=='source_verified_kind_hypothesis')
            c['strict_selected_correct']+=int(new is not None and new in truth)
            c['unmatched']+=int(not aligned);c['exact_annotated']+=int(bool(truth))
            if truth:
                c['recall10_hits']+=int(bool(truth&set(choice['top10'])));c['recall50_hits']+=int(bool(truth&set(choice['top50'])))
                c['wrong_old_corrected']+=int(bool(old) and not old&truth and new in truth)
                c['correct_old_damaged']+=int(bool(old&truth) and new not in truth)
            changed=new is not None and new not in old
            if changed:
                c['new_or_changed']+=1;c['new_or_changed_strict_correct']+=int(new in truth)
                c['changed_existing' if old else 'new_link']+=1
            if changed or old and new not in old:
                records.append({'pmid':p['pmid'],'mention':{k:m[k] for k in ('kind','start','end','text')},
                    'old_ids':sorted(old),'selected_id':new,'strict_gold_ids':sorted(truth),
                    'selection_reason':choice['selection_reason'],'used_for_reselection':False})
    for c in counts.values():
        c['new_or_changed_strict_precision']=c['new_or_changed_strict_correct']/c['new_or_changed'] if c['new_or_changed'] else None
        c['selected_strict_precision']=c['strict_selected_correct']/c['selected'] if c['selected'] else None
    return {'counts':{k:dict(v) for k,v in counts.items()},'records':records,
            'full_strict_denominator_including_unmatched':True,'zero_changes_is_not_success':True}

def evaluate():
    verify_freeze()
    assert (HOME/'prediction-complete.json').exists()
    from ..repair_v3.runner import gold
    docs,digest=gold();all_gold=set();observed={};correct={};results={};transitions={}
    for d in docs:all_gold.update(evaluation_v6.relation_sets(d,{'relations':[]})[0])
    assert len(all_gold)==154 and len(docs)==100
    sources={d['pmid']:d['text'] for d in data('development')}
    assert all(d['text']==sources[d['pmid']] for d in docs)
    for arm in ARMS:
        predictions={d['pmid']:read(HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')) for d in docs}
        results[arm]=native_metrics(docs,predictions)
        observed[arm]=set().union(*(evaluation_v6.relation_sets(d,predictions[d['pmid']])[1] for d in docs))
        correct[arm]=observed[arm]&all_gold
    for arm,expected in [('v2',(64,64,90)),('v3',(63,57,91))]:
        assert tuple(results[arm]['metrics'][k] for k in ('tp','fp','fn'))==expected
    for arm in ARMS:
        transitions[arm]={'retained_v2_tp':len(correct[arm]&correct['v2']),'lost_v2_tp':len(correct['v2']-correct[arm]),
            'new_tp':len(correct[arm]-correct['v2']),
            'paired_bootstrap2000_ci95_vs_v2':_bootstrap({p:v['relation'] for p,v in results['v2']['per_paper'].items()},
                                                       {p:v['relation'] for p,v in results[arm]['per_paper'].items()},SEED),
            'paired_bootstrap2000_ci95_vs_v3':_bootstrap({p:v['relation'] for p,v in results['v3']['per_paper'].items()},
                                                       {p:v['relation'] for p,v in results[arm]['per_paper'].items()},SEED)}
    fates=[]
    for k in sorted(observed['v3']-all_gold):
        restored=[v for v in correct['combined'] if v[:2]==k[:2]]
        fates.append({'key':k,'v4_status':'retained_error' if k in observed['combined'] else 'type_corrected' if restored else 'removed_or_identity_changed',
                      'corrected_same_pair':restored,'expert_adjudication':False})
    byid={d['pmid']:d for d in docs}
    labeled=[{**p,'document':byid[p['pmid']]} for p in load('development')]
    write('analysis/id-report.json',identity_report(labeled,read(HOME/'selection.json')['model']['identity']))
    pertype={arm:{t:benchmarks._metrics({k for k in all_gold if k[2]==t},{k for k in observed[arm] if k[2]==t}) for t in rep.TYPES} for arm in ARMS}
    write('analysis/error-fates.json',{'records':fates,'association41':dict(Counter(r['v4_status'] for r in fates if r['key'][2]=='Association')),
                                      'other16':dict(Counter(r['v4_status'] for r in fates if r['key'][2]!='Association')),
                                      'used_for_reselection':False,'per_type':pertype})
    m=results['combined']['metrics'];goal=m['tp']>=64 and m['precision']>=.85 and m['recall']>=FLOOR
    write('comparison.json',{'schema':'repair-v4-comparison','papers':100,'gold_relations':154,'gold_sha256':digest,
          'arms':results,'transitions':transitions,'goal_met':goal,'per_type':pertype,
          'budget':budget_status(),'additional_calls':budget_status()['attempts']-read(HOME/'audit.json')['budget_at_start']['attempts'],
          'criteria':{'tp_minimum':64,'precision_minimum':.85,'recall_minimum_exact':FLOOR},
          'configuration_chosen_from_development':False,'default_engine':'heuristic-v1',
          'scope':'repeated exposed development, not independent final','nutrition_accuracy':'unmeasured','sealed_test_opened':False})
    log('evaluation-complete',goal_met=goal,arms={a:v['metrics'] for a,v in results.items()})

def compare():
    verify_freeze()
    integrity()
    from ..repair_v3.runner import gold
    docs,_=gold();result=read(HOME/'comparison.json')
    for arm in ARMS:
        predictions={d['pmid']:read(HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')) for d in docs}
        assert native_metrics(docs,predictions)==result['arms'][arm]
    for name,digest in read(HOME/'audit.json')['preserved_sha256'].items():
        if sha((ROOT/name).read_bytes())!=digest:raise ValueError('이전 버전 보존 실패: '+name)
    actual_valid=sum(1 for _ in INFERENCE_HOME.glob('*/*/valid/*.json'))
    assert actual_valid==600
    assert result['additional_calls']<=653
    labels={'v2':'v2 기준선','v3':'v3 기준선','sol_only':'새 Sol 단독','luna_only':'새 Luna 단독',
            'identity_only':'ID만 교정','events_locked_ids':'이벤트 개선·기존 ID 고정','combined':'v4 결합'}
    lines=['# 관계 추출 v4 실제 추가 추론 및 개발 재평가','',
        '같은 100편·정답 154건의 반복 노출 개발 비교다. 독립 최종 성능과 영양학 정확도를 뜻하지 않는다.','',
        '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |','|---|---:|---:|---:|---:|---:|---:|']
    out=io.StringIO(newline='');writer=csv.DictWriter(out,fieldnames=['arm','tp','fp','fn','precision','recall','f1']);writer.writeheader()
    for arm in ARMS:
        m=result['arms'][arm]['metrics']
        percentage=lambda value: '정의 불가' if value is None else format(value,'.2%')
        lines.append(f"| {labels[arm]} | {m['tp']} | {m['fp']} | {m['fn']} | {percentage(m['precision'])} | {percentage(m['recall'])} | {percentage(m['f1'])} |")
        writer.writerow({'arm':arm,**m})
    t=result['transitions']['combined']
    lines+=['',f"판정: {'목표 충족' if result['goal_met'] else '목표 미달'}. 기본 엔진 v1 유지.",
        f"v2 정탐 유지 {t['retained_v2_tp']}건, 상실 {t['lost_v2_tp']}건, 신규 {t['new_tp']}건.",
        '',f"실제 추가 요청 {result['additional_calls']}회. 유효 문헌·모델 응답 {actual_valid}개. 잔여 예산 {result['budget']['remaining']}회.",
        '학습 200편·400응답과 개발 100편·200응답은 서로 구분해 저장했고 새 요청·응답 해시를 보존했다.',
        '', '새 특징을 사용하는 학습은 추가 검증을 받은 200편에 한정했다. 관계 양성 142편·정답 549건은 모두 포함한다.',
        '음성 층화 표집의 실제 수치와 가중 추정치는 selection.json에서 구분하며 전체 384편의 실제 정밀도로 표현하지 않는다.',
        '', '모든 출력 인용의 문자 위치를 확인했고, 저장 예측만으로 오프라인 재채점한 수치는 동일하다.',
        '학습 목표 미달 구성을 개발 진단으로 평가할 수 있지만 봉인 시험과 기본 엔진 전환은 허용하지 않는다.']
    report='\n'.join(lines)+'\n'
    for name,text in [('comparison.md',report),('comparison.csv',out.getvalue())]:
        path=HOME/name
        if path.exists() and path.read_text(encoding='utf8')!=text:raise ValueError('보고서 변경')
        path.write_text(text,encoding='utf8',newline='')
    write('completion-audit.json',{'offline_rescore_identical':True,'previous_versions_preserved':True,
          'source_offsets_exact':True,'valid_responses':600,'budget':budget_status(),'goal_met':result['goal_met'],
          'default_engine':'heuristic-v1','production_modified':False})
    files=[*HOME.glob('*.json'),*HOME.glob('*.md'),*HOME.glob('*.csv'),*(HOME/'analysis').glob('*.json')]
    write('delivery-manifest.json',{'schema':'repair-v4-delivery','source_sha256':source_files(),
          'artifacts_sha256':{str(p.relative_to(HOME)):sha(p.read_bytes()) for p in files if p.name not in ('delivery-manifest.json','run-state.json')},
          'mutable_controller_state_excluded':True})

def resume():
    from .review import request
    from .aliases import prepare_canonical
    for receipt,action in [('audit.json',prepare),('canonical-inputs.json',prepare_canonical),('pilot-canonical.json',lambda:request('train',True)),
        ('train-inference-complete.json',lambda:request('train')),('selection.json',train),('freeze.json',freeze),
        ('development-inference-complete.json',lambda:request('development')),('prediction-complete.json',predict),
        ('comparison.json',evaluate),('completion-audit.json',compare)]:
        if not (HOME/receipt).exists():
            log('stage-start',receipt=receipt);action()
