"""Unchanged native evaluator; exposed development labels are read only after 100 predictions exist."""
import copy,random
from collections import Counter
from .common import *

def verify_predictions():
 m=read(HOME/'predictions/manifest.json')
 if not m['complete_before_scoring'] or m['papers']!=100:raise ValueError('100편 예측 완료 전 평가 금지')
 docs=read(HOME/'data/development-sources.json')['documents'];source={d['pmid']:d for d in docs}
 expected={'predictions/final/paper-'+pmid+'.json' for pmid in source}
 if len(m['files'])!=100 or {v['path'].replace('\\','/') for v in m['files']}!=expected:raise ValueError('개발 예측 누락/중복')
 if filehash(HOME/'freeze.json')!=m['freeze_sha256']:raise ValueError('예측과 동결 버전 불일치')
 for v in m['files']:
  if filehash(HOME/v['path'])!=v['sha256']:raise ValueError('예측 파일 변경')
  p=read(HOME/v['path']);d=source[p['pmid']]
  if p['source_sha256']!=sha(d['text']):raise ValueError('예측과 개발 원문 불일치')
  for r in p['relations']:
   if not all(exact(d['text'],s) for s in r['endpoints']+r['evidence']):raise ValueError('수락 인용 위치 불일치')
 return m

def evaluate_local():
 from ..repair_v3.runner import gold
 from ..repair_v3.common import native_metrics,budget_status
 from .. import evaluation_v6,benchmarks
 from ..chatgpt_evaluation import _bootstrap
 verify_predictions()
 from ..repair_v5.pipeline import verify_freeze as verify_legacy
 verify_legacy();docs,goldhash=gold();paths={
 'v2':'runtime/chatgpt-repair-v2/predictions/combined','v3':'runtime/chatgpt-repair-v3/predictions/combined',
 'v4':'runtime/chatgpt-repair-v4/predictions/combined','identity_baseline':'runtime/chatgpt-repair-v5-r1/predictions/identity_baseline',
 'v5_r1':'runtime/chatgpt-repair-v5-r1/predictions/combined'}
 predictions={name:{d['pmid']:read(ROOT/path/('paper-'+d['pmid']+'.json')) for d in docs} for name,path in paths.items()}
 predictions['v6_gpu_r2']={d['pmid']:read(HOME/'predictions/final'/('paper-'+d['pmid']+'.json')) for d in docs}
 from ..repair_v4.learning import choose as old_choose
 old_policy=read(ROOT/'runtime/chatgpt-repair-v4/selection.json')['model']['identity']
 # Ablation only. Historical predictions do not enter any new training or final selection.
 onlyid={};onlyrelation={};candidate=[];ids_changed=Counter()
 allrows=read(HOME/'relation-data/development.json')['rows']
 for d in docs:
  pmid=d['pmid'];base=copy.deepcopy(predictions['identity_baseline'][pmid]);dec=read(HOME/'normalized-final/development'/('paper-'+pmid+'.json'))['decisions'];lookup={(r['kind'],r['start'],r['end']):r for r in dec};old_ids={}
  archived=read(ROOT/'runtime/chatgpt-repair-v4/rebuilt/development'/('paper-'+pmid+'.json'))
  for m in archived['mentions']:
   m['_source']=d['text'];selected=old_choose(m,old_policy)['selected_id']
   if selected:old_ids.setdefault((m['kind'],m['start'],m['end']),set()).add(selected)
  for r in base['relations']:
   for i,e in enumerate(r.get('endpoints',[])):
    key=(e['kind'].replace('component','chemical'),e['start'],e['end'])
    n=lookup.get(key)
    if n and n['reason'] in ('new','changed') and n['selected_id']:
     k='entity1' if i==0 else 'entity2';ids_changed['changed_endpoints']+=int(r[k]!=n['selected_id'])
     if r.get('subject')==r[k]:r['subject']=n['selected_id']
     r[k]=n['selected_id']
  onlyid[pmid]=base
  rr=copy.deepcopy(predictions['v6_gpu_r2'][pmid])
  for r in rr['relations']:
   for i,e in enumerate(r['endpoints']):
    values=old_ids.get((e['kind'],e['start'],e['end']),set())
    k='entity1' if i==0 else 'entity2';new=next(iter(values)) if len(values)==1 else None
    if r.get('subject')==r[k]:r['subject']=new
    r[k]=new
  rr['relations']=[r for r in rr['relations'] if r['entity1'] and r['entity2']];onlyrelation[pmid]=rr
  rows=[r for r in allrows if r['pmid']==pmid];expected=evaluation_v6.relation_sets(d,{'relations':[]})[0]
  strict_pairs={(r['ids'][0],r['ids'][1]) for r in rows if all(r['ids'])};c=read(HOME/'prepared/development'/('paper-'+pmid+'.json'));mentions=c['mentions']
  states=Counter()
  for _,pair,t in expected:
   a=next((v for v in pair if any(e['kind'] in ('component','chemical','nutrient') and v in e.get('native_ids',[]) for e in d['entities'])),pair[0]);b=next(v for v in pair if v!=a)
   if (a,b) in strict_pairs:states['selected_ID_pair_available']+=1;continue
   ea=[e for e in d['entities'] if a in e.get('native_ids',[])];eb=[e for e in d['entities'] if b in e.get('native_ids',[])]
   aa=[m for m in mentions if any(m['kind']==e['kind'].replace('component','chemical') and m['start']==e['start'] and m['end']==e['end'] for e in ea)];bb=[m for m in mentions if any(m['kind']==e['kind'].replace('component','chemical') and m['start']==e['start'] and m['end']==e['end'] for e in eb)]
   if not aa or not bb:states['mention_boundary_missing']+=1
   elif not any(a in m['candidate_ids'][:200] for m in aa) or not any(b in m['candidate_ids'][:200] for m in bb):states['ID_retrieval_missing_200']+=1
   else:states['ID_selection_or_pair_construction']+=1
  candidate.append({'pmid':pmid,'gold':len(expected),'first_failure':dict(states),'candidate_pairs':len(rows)})
 predictions.update({'new_identity_only':onlyid,'new_relation_existing_ID_only':onlyrelation})
 scores={name:native_metrics(docs,p) for name,p in predictions.items()}
 if tuple(scores['identity_baseline']['metrics'][k] for k in ('tp','fp','fn'))!=(65,63,89):raise ValueError('기준선 65/63/89 불일치')
 transitions={};baseline=predictions['identity_baseline'];outcomes=[]
 for name,pred in predictions.items():
  c=Counter()
  for d in docs:
   expected,old,_,_=evaluation_v6.relation_sets(d,baseline[d['pmid']]);_,new,_,_=evaluation_v6.relation_sets(d,pred[d['pmid']]);a,b=expected&old,expected&new
   c.update({'retained':len(a&b),'lost':len(a-b),'new':len(b-a)})
   for key in sorted(old-expected):
    outcomes.append({'arm':name,'pmid':d['pmid'],'old_error':list(key),'retained_error':key in new,'type':key[2]})
  transitions[name]=dict(c)
 intervals={name:_bootstrap({p:v['relation'] for p,v in scores['identity_baseline']['per_paper'].items()},
  {p:v['relation'] for p,v in score['per_paper'].items()},SEED) for name,score in scores.items()}
 final=scores['v6_gpu_r2']['metrics'];passed=final['tp']>=65 and final['precision']>=.85 and final['recall']>=65/154
 result={'papers':100,'gold_relations':154,'gold_sha256':goldhash,'scores':scores,'transitions':transitions,'candidate_diagnostics':candidate,'baseline_error_destinations':outcomes,
  'id_ablation_changes':dict(ids_changed),'paired_bootstrap_2000_delta_95':intervals,'performance_target_passed':passed,'budget':budget_status(),
  'evaluator_sha256':benchmarks.evaluator_source_hashes(),'independent_final_performance':False,'default_engine':'heuristic-v1','nutrition_accuracy':'unmeasured','sealed_test_opened':False}
 atomic(HOME/'evaluation.json',result,True)
 lines=['# relation-repair-v6-gpu-r2 개발 비교','', '| 구성 | TP | FP | FN | 정밀도 | 재현율 | F1 |','|---|---:|---:|---:|---:|---:|---:|']
 for name,v in scores.items():
  m=v['metrics'];lines.append('| '+name+' | '+' | '.join(str(m[k]) for k in ('tp','fp','fn'))+' | '+' | '.join('미정의' if m[k] is None else f'{100*m[k]:.2f}%' for k in ('precision','recall','f1'))+' |')
 lines+=['','목표 충족: '+str(passed),'','같은 100편을 반복 사용한 개발 비교이며 독립 최종 성능이 아닙니다. 기본 엔진은 heuristic-v1입니다.']
 (HOME/'comparison.md').write_text('\n'.join(lines)+'\n',encoding='utf8');return {k:v['metrics'] for k,v in scores.items()}
