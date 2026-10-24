"""Listwise contextual ranking and held-out automatic selection gates."""
import gc,math
from collections import Counter
from .common import *
from .knowledge import Knowledge

def query(text,m):
 return text[max(0,m['start']-180):m['start']]+' [MENTION] '+m['text']+' [/MENTION] '+text[m['end']:m['end']+180]
def gold_ids(d,m):return set(v for e in d.get('entities',[]) if e['kind']==m['kind'] and e['start']==m['start'] and e['end']==m['end'] for v in e.get('native_ids',[]))
def annotation_scope(d,kind):
 if 'entities' not in d:return None
 if d.get('relation_annotation_complete') is False:
  return kind==('chemical' if d.get('gene_annotation_complete') is False else 'gene')
 return True
def candidate_texts(kb,m,limit=50,inject=()):
 ids=list(m['candidate_ids'][:limit])
 for identifier in sorted(inject):
  if identifier not in ids and kb.by_id(m['kind'],identifier):ids.append(identifier)
 texts=['NIL; No valid matching entity in the candidate list.'];identifiers=[None]
 for identifier in ids:
  r=kb.by_id(m['kind'],identifier)
  if r:identifiers.append(identifier);texts.append(kb.candidate_text(r,m['text']))
 return identifiers,texts
def training_rows(documents):
 from .neural import tokenizer
 tok=tokenizer();kb=Knowledge();out=[]
 try:
  for d in documents:
   p=read(HOME/'prepared/id-supervision'/('paper-'+d['pmid']+'.json'))
   for m in p['mentions']:
    truth=gold_ids(d,m);identifiers,texts=candidate_texts(kb,m,16,truth)
    positives=[i for i,v in enumerate(identifiers) if v in truth] if truth else [0]
    if not positives:continue # True out-of-KB mentions do not become NIL training labels.
    q=query(d['text'],m);encoded=tok(q,max_length=128,truncation=True)
    out.append({**encoded,'query':q,'candidate_texts':texts,'target_positions':positives,'pmid':d['pmid']})
  return out
 finally:kb.close()

def loss(network,inputs,batch):
 import torch
 from .neural import tokenizer
 global _TOK
 if '_TOK' not in globals():_TOK=tokenizer()
 texts=[(r['query'],s) for r in batch for s in r['candidate_texts']]
 enc=_TOK([v[0] for v in texts],[v[1] for v in texts],max_length=256,truncation=True,padding=True,return_tensors='pt').to(inputs['input_ids'].device)
 scores=network(**enc).float();offset=0;losses=[]
 for r in batch:
  s=scores[offset:offset+len(r['candidate_texts'])];offset+=len(r['candidate_texts'])
  # Multi-ID annotations must not reward an arbitrarily confident single winner.
  # Uniform positive supervision keeps genuinely ambiguous alternatives competitive.
  losses.append(torch.logsumexp(s,0)-s[r['target_positions']].mean())
 return torch.stack(losses).mean()

def fit(documents):
 from .engine import train_network
 path=HOME/'identity/rankers'/sha(canonical(sorted(d['pmid'] for d in documents)))[:18]
 train_network('identity',training_rows(documents),path,loss);return path

def score(documents,path,profile='id-supervision'):
 import torch
 from .neural import tokenizer,Network
 from .engine import setup,load_weights
 tok=tokenizer();dev=setup();model=load_weights(Network('identity',len(tok)),Path(path)/'model.safetensors').to(dev).eval();kb=Knowledge();records=[];budget=RuntimeBudget();budget.start('identity-score')
 try:
  with torch.inference_mode():
   for number,d in enumerate(documents):
    p=read(HOME/'prepared'/profile/('paper-'+d['pmid']+'.json'))
    cache=HOME/'identity/score-cache'/sha(str(Path(path).relative_to(HOME))+profile)/(d['pmid']+'-'+sha(canonical(d.get('entities',[])))[:12]+'.json')
    inputs_hash=sha(canonical(p));model_hash=filehash(Path(path)/'complete.json')
    if cache.exists():
     saved=read(cache)
     if saved['inputs_sha256']!=inputs_hash or saved['model_receipt_sha256']!=model_hash:raise ValueError('ID 문헌별 캐시 입력 변경')
     records.extend(saved['records']);continue
    begin=len(records)
    for m in p['mentions']:
     budget.check()
     ids,texts=candidate_texts(kb,m);q=query(d['text'],m);scores=[]
     for a in range(0,len(texts),16):
      subset=texts[a:a+16];enc=tok([q]*len(subset),subset,max_length=256,truncation=True,padding=True,return_tensors='pt').to(dev)
      with torch.autocast('cuda',dtype=torch.bfloat16):scores.extend(model(**enc).float().cpu().tolist())
     order=sorted(range(len(ids)),key=lambda i:(-scores[i],str(ids[i])));top=order[0];chosen=ids[top];old=m.get('previous_id');truth=gold_ids(d,m)
     probability=float(torch.tensor(scores).softmax(0)[top]);margin=scores[top]-scores[order[1]] if len(order)>1 else 0.
     record=kb.by_id(m['kind'],chosen) if chosen else None
     records.append({'pmid':d['pmid'],'key':m['key'],'kind':m['kind'],'start':m['start'],'end':m['end'],
      'previous_id':old,'proposed_id':chosen,'taxon':record.get('taxon') if record else None,'priority':probability,'margin':margin,
      'species_evidence_compatible':m['kind']=='chemical' or (len(m.get('supported_taxa',[]))==1 and record is not None and record.get('taxon') in m['supported_taxa']),
      'annotation_scope_complete':annotation_scope(d,m['kind']),
      'strict_correct':bool(chosen and chosen in truth) if annotation_scope(d,m['kind']) else None,'previous_correct':bool(old and old in truth) if annotation_scope(d,m['kind']) else None,
      'in10':bool(truth&set(ids[order[i]] for i in range(min(10,len(order))))),'in50':bool(truth&set(ids)),
      'retrieval_in10':bool(truth&set(m['candidate_ids'][:10])),'retrieval_in50':bool(truth&set(m['candidate_ids'][:50])),'retrieval_in200':bool(truth&set(m['candidate_ids'][:200])),
      'has_gold':bool(truth),'normalization_scores_are_probabilities_of_accuracy':False,
      'top10':[{'native_id':ids[i],'score':scores[i]} for i in order[:10]],'candidate_count':len(ids)})
    atomic(cache,{'records':records[begin:],'inputs_sha256':inputs_hash,'model_receipt_sha256':model_hash},True)
    if number%10==0:log('identity-score-paper',profile=profile,papers=number+1,total=len(documents),mentions=len(records))
  return records
 finally:budget.stop();kb.close();del model;gc.collect();torch.cuda.empty_cache()

def choose_policy(records):
 result={}
 for category in ('new','change'):
  options=[]
  for threshold in POLICY['thresholds']:
   for margin in (0.,.25,.5,1.,2.):
    selected=[r for r in records if r.get('annotation_scope_complete',True) and r.get('species_evidence_compatible',True) and r['proposed_id'] and r['proposed_id']!=r['previous_id'] and bool(r['previous_id'])==(category=='change') and r['priority']>=threshold and r['margin']>=margin]
    n=len(selected);ok=sum(r['strict_correct'] for r in selected);lower=wilson(ok,n)
    if n>=50 and lower>=.9:options.append({'enabled':True,'threshold':threshold,'margin':margin,'n':n,'correct':ok,'wilson_lower':lower})
  result[category]=max(options,key=lambda r:(r['correct'],r['wilson_lower'],r['threshold'])) if options else {'enabled':False,'threshold':1.000001,'margin':0.,'n':0,'correct':0,'wilson_lower':0.}
 return result
def select(r,policy):
 old=r['previous_id'];proposed=r['proposed_id']
 if proposed==old:return proposed,'unchanged'
 rule=policy['change' if old else 'new']
 if proposed and r.get('species_evidence_compatible',True) and rule['enabled'] and r['priority']>=rule['threshold'] and r['margin']>=rule['margin']:return proposed,'changed' if old else 'new'
 return old,'retained' if old else 'NIL'

def calibrate(documents):
 key=sha(canonical(sorted(d['pmid'] for d in documents)))[:18];receipt=HOME/'identity/policies'/(key+'.json')
 if receipt.exists():return read(receipt)
 folds=foldmap(documents,3,'identity-inner');records=[]
 for f in range(3):
  train=[d for d in documents if folds[d['pmid']]!=f];held=[d for d in documents if folds[d['pmid']]==f]
  records.extend(score(held,fit(train)))
 result={'policy':choose_policy(records),'training_pmids':[d['pmid'] for d in documents],'held_scores_sha256':sha(canonical(records))}
 atomic(receipt,result,True);return result

def train():
 documents=read(HOME/'data/identity-train.json')['documents'];folds=foldmap(documents,5,'independent-ner-id');outer=[];automatic=[]
 for f in range(5):
  receipt=HOME/'identity'/('outer-'+str(f)+'.json')
  if receipt.exists():row=read(receipt)
  else:
   training=[d for d in documents if folds[d['pmid']]!=f];held=[d for d in documents if folds[d['pmid']]==f]
   policy=calibrate(training)['policy'];values=score(held,fit(training),'normalization');decisions=[]
   for r in values:
    chosen,reason=select(r,policy);decisions.append({**r,'selected_id':chosen,'reason':reason,
     'selected_correct':r['strict_correct'] if chosen==r['proposed_id'] else r['previous_correct']})
   row={'fold':f,'policy':policy,'decisions':decisions,'held_pmids':[d['pmid'] for d in held]};atomic(receipt,row,True)
  outer.append(row);automatic.extend(row['decisions']);log('identity-outer-complete',fold=f,decisions=len(row['decisions']))
 policy=calibrate(documents)['policy']
 for category in ('new','change'):
  accepted=[r for r in automatic if r.get('annotation_scope_complete',True) and r['reason']==('new' if category=='new' else 'changed')];ok=sum(r['selected_correct'] for r in accepted)
  if len(accepted)<50 or wilson(ok,len(accepted))<.9:policy[category]={**policy[category],'enabled':False,'blocked_by_automatic_outer_gate':True}
 path=fit(documents);atomic(HOME/'identity/selection.json',{'policy':policy,'model':str(path.relative_to(HOME)),
  'outer_folds':outer,'automatic_outer_decisions':len(automatic),'selection_uses_development':False,'reference_is_dictionary_only':True},True)
 return policy

def apply_all():
 model=read(HOME/'identity/selection.json');path=HOME/model['model'];policy=model['policy'];files=[]
 categories=set(read(HOME/'knowledge/chemical-categories.json')['chemical_ids'])
 for split in ('train','development'):
  source=HOME/'data'/('relations-train.json' if split=='train' else 'development-sources.json');documents=read(source)['documents']
  # Source-only documents are mandatory at the actual model boundary.
  docs=[{'pmid':d['pmid'],'text':d['text']} for d in documents];scored=score(docs,path,split);byid={}
  for r in scored:
   chosen,reason=select(r,policy);byid.setdefault(r['pmid'],[]).append({**{k:v for k,v in r.items() if k not in ('strict_correct','previous_correct','has_gold','in10','in50','retrieval_in10','retrieval_in50','retrieval_in200','annotation_scope_complete')},'selected_id':chosen,'reason':reason,
    'selected_chemical_category_confirmed':r['kind']=='gene' or chosen in categories,'category_is_not_an_identity_or_benchmark_type_rule':True})
  for d in documents:
   p=HOME/'normalized'/split/('paper-'+d['pmid']+'.json');atomic(p,{'pmid':d['pmid'],'source_sha256':sha(d['text']),'decisions':byid.get(d['pmid'],[]),'source_only':True},True);files.append({'path':str(p.relative_to(HOME)),'sha256':filehash(p)})
 atomic(HOME/'normalized/manifest.json',{'files':files,'selection_sha256':filehash(HOME/'identity/selection.json')},True)
