"""Batch-only state machine. Production APIs and protected labels are never imported."""
from .common import *

def status():
 state={'engine':POLICY['engine'],'default_engine':'heuristic-v1','stages':{},'protected_labels_opened':False}
 for name,path in [('audit','audit.json'),('ner','identity/ner-ready.json'),('knowledge','knowledge/manifest.json'),('alias','identity/alias-dual/complete.json'),
  ('dense','knowledge/dense-manifest.json'),('prepared','prepared/manifest.json'),('identity','identity/selection.json'),('normalized','normalized/manifest.json'),
  ('auxiliary','auxiliary/model/complete.json'),('relations','calibration/selection.json'),('freeze','freeze.json'),('predictions','predictions/manifest.json'),('evaluation','evaluation.json')]:
  p=HOME/path;state['stages'][name]={'complete':p.exists(),'path':str(p)}
  if p.exists() and name in ('identity','relations'):state['stages'][name]['summary']={k:v for k,v in read(p).items() if k in ('policy','outer_metrics','training_gate_passed')}
 if (HOME/'gpu-budget.json').exists():state['gpu_budget']=read(HOME/'gpu-budget.json')
 return state

def train_id():
 from .identity import train,apply_all
 from .datasets import audit
 audit()
 for name in ('prepared/manifest.json','knowledge/manifest.json','identity/ner-ready.json'):
  if not (HOME/name).exists():raise FileNotFoundError('검증된 이전 원문·검색 자산이 필요함: '+name)
 train();apply_all()

def train_relation():
 from .auxiliary import train
 from .calibration import train as calibrate
 train();return calibrate()

def freeze():
 from .relations import build_rows
 build_rows('development') # Source-only candidate table is frozen BEFORE any neural prediction/scoring.
 selection=read(HOME/'calibration/selection.json');paths=['data/relations-train.json','data/identity-train.json','data/development-sources.json','data/protection.json',
 'knowledge/manifest.json','knowledge/dense-manifest.json','prepared/manifest.json','normalized/manifest.json','identity/selection.json','calibration/selection.json','auxiliary/manifest.json','auxiliary/model/complete.json',
 'aliases.sqlite3','knowledge/overlay.sqlite3','knowledge/dense.npy','knowledge/dense-rows.sqlite3','knowledge/chemical-categories.json','knowledge/base-source/manifest.json',
 'identity/ner-full/model.safetensors','identity/alias-dual/model.safetensors','relation-data/train.json','relation-data/development.json','auxiliary/cross-source-audit.json']
 for manifest in ('prepared/manifest.json','normalized/manifest.json'):paths.extend(v['path'] for v in read(HOME/manifest)['files'])
 identity_model=read(HOME/'identity/selection.json')['model']
 paths.extend([identity_model,str(Path(identity_model).parent/'complete.json')])
 paths.extend(str(p.relative_to(HOME)) for p in (HOME/'identity/auto-profiles').glob('*/manifest.json'))
 paths.extend(str(p.relative_to(HOME)) for p in (HOME/'identity/policies').glob('*.json'))
 for model in selection['models'].values():paths.extend([model+'/model.safetensors',model+'/complete.json'])
 if (HOME/'teacher/manifest.json').exists():paths.append('teacher/manifest.json')
 result={'source_sha256':source_hashes(),'artifacts_sha256':{p:filehash(HOME/p) for p in sorted(set(paths))},'training_gate_passed':selection['training_gate_passed'],
  'protected_test_opened':False,'final_test_allowed':selection['training_gate_passed'],'reason':'Sealed test remains gated on training criteria; this receipt precedes development scoring.',
  'development_selection':False,'policy':POLICY,'frozen_unix':time.time()}
 if (HOME/'freeze.json').exists():
  old=verify_freeze();return old
 atomic(HOME/'freeze.json',result,True);return result

def verify_freeze():
 value=read(HOME/'freeze.json')
 if value['source_sha256']!=source_hashes():raise ValueError('동결 후 소스 변경; 새 실험 버전 필요')
 for path,digest in value['artifacts_sha256'].items():
  if filehash(HOME/path)!=digest:raise ValueError('동결 후 산출물 변경: '+path)
 return value

def project(row,value):
 from .semantics import context
 labels=('chemical_to_gene','gene_to_chemical','undirected');prob=value['document'].get('direction',[1/3]*3);direction=labels[max(range(3),key=lambda i:prob[i])] if max(prob)>=.7 else 'unknown'
 return {'entity1':row['ids'][0],'entity2':row['ids'][1],'predicate':value['predicate'],'role_direction':direction,
  'subject':row['ids'][0] if direction=='chemical_to_gene' else row['ids'][1] if direction=='gene_to_chemical' else None,
  'endpoints':[{k:m[k] for k in ('kind','start','end','text')} for m in row['endpoints']],
  'evidence':row['evidence'],'evidence_kind':'model-selected source context; direct event not human-verified',
  'priority':value['priority'],'priority_is_accuracy_probability':False,'review_status':'unreviewed','engine':POLICY['engine'],
  'document_relation_hypothesis':True,'context':context(row['views']),
  'decision_trace':{'semantic_document':value['document'],'semantic_definition':value['nli'],'id_features':row['id_features'],'type_priorities':value['type_priorities']}}

def predict():
 from .relations import build_rows,score
 from .calibration import decisions
 frozen=verify_freeze();selection=read(HOME/'calibration/selection.json');rows=build_rows('development');values={r['pair_key']:{} for r in rows}
 for mode in ('document','nli'):
  p=HOME/'predictions'/(mode+'-scores.json')
  if p.exists():v=read(p)
  else:v=score(rows,HOME/selection['models'][mode],mode);atomic(p,v,True)
  for key,val in v.items():values[key][mode]=val
 chosen=decisions(rows,values,selection['final']['model']);threshold=selection['final']['selected']['threshold'];byid={};files=[]
 for v in chosen:
  row=v['row']
  if not all(row['ids']) or v['priority']<threshold:continue
  byid.setdefault(row['pmid'],[]).append(project(row,v))
 docs=read(HOME/'data/development-sources.json')['documents']
 for d in docs:
  found={}
  for r in byid.get(d['pmid'],[]):
   if not all(exact(d['text'],s) for s in r['evidence']+r['endpoints']):raise ValueError('출처 위치 불일치')
   key=(r['entity1'],r['entity2']);old=found.get(key)
   if old is None or r['priority']>old['priority']:found[key]=r
  p=HOME/'predictions/final'/('paper-'+d['pmid']+'.json');atomic(p,{'pmid':d['pmid'],'relations':list(found.values()),'source_sha256':sha(d['text']),'freeze_sha256':filehash(HOME/'freeze.json'),'source_only':True},True)
  files.append({'path':str(p.relative_to(HOME)),'sha256':filehash(p)})
 result={'papers':len(docs),'files':files,'freeze_sha256':filehash(HOME/'freeze.json'),'gold_read':False,'complete_before_scoring':True};atomic(HOME/'predictions/manifest.json',result,True);return result

def resume():
 train_id();train_relation();freeze();predict();return status()

def main(action):
 if action=='status':return status()
 if action in ('resume','train-id','train-relation','calibrate','freeze','predict'):
  return {'resume':resume,'train-id':train_id,'train-relation':train_relation,'calibrate':train_relation,'freeze':freeze,'predict':predict}[action]()
 if action=='build-kb':
  from .knowledge import build
  return build()
 if action in ('evaluate','compare'):
  from .evaluation import evaluate_local
  return evaluate_local()
 raise ValueError('알 수 없는 단계: '+action)
