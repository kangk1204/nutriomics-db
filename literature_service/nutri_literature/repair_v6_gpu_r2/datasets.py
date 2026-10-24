"""Source-only deployment inputs; relation/normalization supervision never mixed."""
import copy
from collections import Counter
from .common import *

def normalize(d):
 d=copy.deepcopy(d);d['pmid']=str(d['pmid']);d['source_sha256']=sha(d['text'])
 for e in d.get('entities',[]):
  if not exact(d['text'],e):raise ValueError('공식 개체 위치 불일치: '+d['pmid'])
  if e['kind']=='component':e['kind']='chemical'
  e['native_ids']=[str(v) for v in e.get('native_ids',[]) if v and v not in ('-','None','null')]
 return d

def export_local():
 from ..repair_v3.common import data,budget_status,native_metrics
 from ..repair_v3.runner import gold
 from ..repair_v5.pipeline import verify_freeze
 verify_freeze();train=[normalize(d) for d in data('train')];development=data('development')
 assert len(train)==384 and len(development)==100
 if any('entities' in d or 'relations' in d for d in development):raise ValueError('개발 입력에 정답 포함')
 prior=ROOT/'runtime/v6/training-data/audited-complete-v6-e00e690ae91d2648/audit.json';audit=read(prior)
 protected_ids=set(map(str,audit['protected_pmids']))|{str(d['pmid']) for d in development}
 protected_pmcids=set(audit.get('protected_pmcids',[]));protected_text=set()
 protected_sources=[]
 for src in audit['protected_sources']:
  p=Path(src['path'])
  if not p.exists():raise FileNotFoundError('보호 입력 경로 없음: '+str(p))
  if filehash(p)!=src['sha256']:raise ValueError('보호 원문 변경')
  ds=read(p)['documents'];protected_text.update(textkey(d['text']) for d in ds)
  protected_sources.append({'path':str(p.relative_to(ROOT)),'sha256':filehash(p),'labels_read':False})
 protected_text.update(textkey(d['text']) for d in development)
 # The relation cohort is fixed; upstream ID/NER/auxiliary training must exclude ALL its documents.
 relation_ids={d['pmid'] for d in train};relation_text={textkey(d['text']) for d in train}
 merged_path=ROOT/'runtime/v6/training-data/official-merged-train-v6.json';merged=read(merged_path)
 ids=[];excluded=Counter();seen=set()
 for d in merged['documents']:
  if d.get('relation_annotation_complete') is not False:continue
  reason=('pmid' if str(d['pmid']) in protected_ids|relation_ids else 'pmcid' if d.get('pmcid') in protected_pmcids else
          'text' if textkey(d['text']) in protected_text|relation_text else None)
  if reason:excluded[reason]+=1;continue
  if str(d['pmid']) in seen:excluded['duplicate']+=1;continue
  seen.add(str(d['pmid']));ids.append(normalize(d))
 assert not relation_ids&{d['pmid'] for d in ids}
 for d in train:
  if d['pmid'] in protected_ids or textkey(d['text']) in protected_text:raise ValueError('관계 학습·보호 자료 중복')
 docs,_=gold();base={d['pmid']:read(ROOT/'runtime/chatgpt-repair-v5-r1/predictions/identity_baseline'/('paper-'+d['pmid']+'.json')) for d in docs}
 scores=native_metrics(docs,base);assert tuple(scores['metrics'][k] for k in ('tp','fp','fn'))==(65,63,89)
 for name,value in [('relations-train',train),('identity-train',ids),('development-sources',development)]:
  atomic(HOME/'data'/(name+'.json'),{'documents':value,'split':'development_source_only' if name.startswith('development') else 'train'},True)
 atomic(HOME/'data/protection.json',{'pmids':sorted(protected_ids|relation_ids),'text_hashes':sorted(protected_text|relation_text),
  'pmcids':sorted(protected_pmcids),'sources':protected_sources,'labels_read':False},True)
 old_files=[*sorted((ROOT/'runtime/chatgpt-repair-v5-r1').glob('predictions/*/*.json')),
  ROOT/'runtime/chatgpt-repair-v5-r1/comparison.json',ROOT/'runtime/chatgpt-repair-v5/freeze.json']
 snapshot={'relation_train':len(train),'identity_train':len(ids),'development':len(development),'excluded_identity':dict(excluded),
  'relation_chemical_gene_gold':549,'baseline':scores,'budget_at_start':budget_status(),
  'prior_audit_sha256':filehash(prior),'raw_merged_sha256':filehash(merged_path),'protected_labels_read':False,
  'baseline_preservation':{str(p.relative_to(ROOT)):filehash(p) for p in old_files},'source':source_hashes(),
  'pretraining_text_overlap':'unknown; no pretrained BioRED task weights','legacy_corrected_predictions_are_training_features':False}
 atomic(HOME/'audit.json',snapshot,True);atomic(HOME/'policy.json',POLICY,True)
 log('audit-complete',relations=len(train),normalization=len(ids),development=100,budget=snapshot['budget_at_start'])
 return snapshot

def transfer_contract():
 paths=['audit.json','policy.json',*['data/'+p.name for p in sorted((HOME/'data').glob('*.json'))],
  'auxiliary/train.json','auxiliary/manifest.json']
 value={'files':{p:filehash(HOME/p) for p in paths},'source_computer':'local authorized workspace','credentials_included':False,
  'source_only_development':True,'protection_labels_exported':False,'gold_development_exported':False}
 atomic(HOME/'transfer-contract.json',value,True);return value

def audit():
 if not (HOME/'audit.json').exists():return export_local()
 if not (HOME/'transfer-contract.json').exists():transfer_contract()
 contract=read(HOME/'transfer-contract.json')
 for path,digest in contract['files'].items():
  if filehash(HOME/path)!=digest:raise ValueError('전송 입력 계약 불일치: '+path)
 train=read(HOME/'data/relations-train.json')['documents'];external=read(HOME/'data/identity-train.json')['documents'];dev=read(HOME/'data/development-sources.json')['documents']
 if len(train)!=384 or len(dev)!=100 or any('entities' in d or 'relations' in d for d in dev):raise ValueError('코호트/개발 입력 계약 불일치')
 if {d['pmid'] for d in train}&{d['pmid'] for d in external}:raise ValueError('상류 개체 학습과 관계 학습 중복')
 return {'passed':True,'training':len(train),'normalization':len(external),'development_source_only':len(dev),'contract_sha256':filehash(HOME/'transfer-contract.json')}
