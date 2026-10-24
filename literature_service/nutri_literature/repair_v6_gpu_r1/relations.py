"""ID-independent semantic supervision and explicitly typed document hypotheses."""
import gc,math
from collections import defaultdict
from .common import *
from .text import clusters,pair_views

HYPOTHESES={
 'exists':'The chemical and the gene or protein have a relationship supported by this document.',
 'Association':'The chemical and the gene or protein have an association whose specific relation is not established as another named relation.',
 'Bind':'The chemical binds to the gene product or protein.',
 'Negative_Correlation':'The chemical and the gene or protein have a negative correlation under the same stated conditions.',
 'Positive_Correlation':'The chemical and the gene or protein have a positive correlation under the same stated conditions.',
 'Comparison':'The chemical and the gene or protein are compared in the reported study.',
 'Conversion':'A conversion involving the chemical and the gene product is reported.',
 'Cotreatment':'The chemical and the gene product are jointly used as treatments.',
 'Drug_Interaction':'An interaction involving the chemical and the gene product as therapeutic agents is reported.'}

def native_truth(d):
 ids={}
 for e in d.get('entities',[]):
  for identifier in e.get('native_ids',[]):ids.setdefault(identifier,set()).add(e['kind'])
 out=set()
 for r in d.get('relations',[]):
  a,b=r['entity1'],r['entity2'];typ=r['predicate']
  if typ not in TYPES:continue
  if ('chemical' in ids.get(a,set()) and 'gene' in ids.get(b,set())):out.add((a,b,typ))
  elif ('gene' in ids.get(a,set()) and 'chemical' in ids.get(b,set())):out.add((b,a,typ))
 return out
def aligned(d,node):
 result=set();ambiguous=False
 for m in node['mentions']:
  exact=[e for e in d.get('entities',[]) if e['kind']==node['kind'] and e['start']==m['start'] and e['end']==m['end']]
  if exact:result.update(v for e in exact for v in e.get('native_ids',[]));continue
  overlap=[e for e in d.get('entities',[]) if e['kind']==node['kind'] and e['start']<m['end'] and m['start']<e['end']]
  candidates=[e for e in overlap if min(e['end'],m['end'])-max(e['start'],m['start'])>=.8*max(e['end']-e['start'],m['end']-m['start']) and min(e['end']-e['start'],m['end']-m['start'])>=4]
  identities={tuple(sorted(e.get('native_ids',[]))) for e in candidates}
  if len(identities)==1:result.update(next(iter(identities)))
  elif overlap:ambiguous=True
 return result,ambiguous and not result

def semantic_types(d,c,g):
 (a,unclear_a),(b,unclear_b)=aligned(d,c),aligned(d,g)
 if unclear_a or unclear_b:return None
 return {t for x,y,t in native_truth(d) if x in a and y in b}

def direction_target(d,c,g):
 a,b=aligned(d,c)[0],aligned(d,g)[0];directions=set()
 for r in d.get('relations',[]):
  if 'subject' not in r:continue
  x,y=str(r.get('entity1','')),str(r.get('entity2',''))
  if not ((x in a and y in b) or (x in b and y in a)):continue
  s=str(r.get('subject',''))
  if s in a:directions.add(0)
  elif s in b:directions.add(1)
  elif s in ('None','null','','undirected'):directions.add(2)
 return next(iter(directions)) if len(directions)==1 else -100

def build_rows(split):
 path=HOME/'relation-data'/(split+'.json')
 if path.exists():return read(path)['rows']
 name='relations-train' if split=='train' else 'development-sources';documents=read(HOME/'data'/(name+'.json'))['documents'];out=[]
 for d in documents:
  paper=read(HOME/'prepared'/split/('paper-'+d['pmid']+'.json'));norm=read(HOME/'normalized'/split/('paper-'+d['pmid']+'.json'));decisions={r['key']:r for r in norm['decisions']}
  nodes=clusters(paper['mentions'],d['text']);cs=[v for v in nodes if v['kind']=='chemical'];gs=[v for v in nodes if v['kind']=='gene']
  for c in cs:
   for g in gs:
    views=pair_views(d['text'],c,g)
    if not views:continue
    ranked=[]
    for v in views:
     chosen=[decisions.get(m['key'],{}) for m in v['endpoints']];ids=[r.get('selected_id') for r in chosen]
     ranked.append((not all(ids),abs(v['endpoints'][0]['start']-v['endpoints'][1]['start']),ids,chosen,v))
    lead=min(ranked,key=lambda r:r[:2]);ids=lead[2];chosen=lead[3];row={
     'pmid':d['pmid'],'pair_key':sha(canonical([d['pmid'],c['key'],g['key']]))[:24],'ids':ids,
     'views':views,'source_sha256':sha(d['text']),'endpoints':lead[4]['endpoints'],'evidence':lead[4]['segments'],
     'id_features':[float(r.get('priority',0.)) for r in chosen]+[float(r.get('reason') in ('unchanged','retained')) for r in chosen]+[float(m.get('ner_score',0.)) for m in lead[4]['endpoints']]+[float(r.get('selected_chemical_category_confirmed',False)) for r in chosen]+[float(r.get('nil_priority',0.)) for r in chosen]+[float(r.get('proposed_id') is None) for r in chosen],
     'identity_unresolved':not all(ids),'role_direction':'unknown'}
    if split=='train':
     types=semantic_types(d,c,g);row['semantic_supervised']=types is not None;row['semantic_types']=sorted(types or []);row['exists']=bool(types)
     row['direction_target']=direction_target(d,c,g)
     # Position-aligned semantics remain positive even when automatic native IDs are wrong.
     row['strict_types']=sorted(t for a,b,t in native_truth(d) if a==ids[0] and b==ids[1]) if all(ids) else []
     row['id_correct']=all(any(identifier in e.get('native_ids',[]) and e['kind']==node['kind'] for e in d['entities']) for node,identifier in ((c,ids[0]),(g,ids[1]))) if all(ids) else False
    out.append(row)
 log('relation-pairs',split=split,pairs=len(out),papers=len(documents));atomic(path,{'rows':out,'training_labels':split=='train','native_IDs_are_features':False},True);return out

def encode(rows,mode):
 from .neural import tokenizer,encode_view
 tok=tokenizer()
 for r in rows:
  hypotheses=['exists',*TYPES] if mode=='nli' else [None]
  if mode=='nli' and 'semantic_types' in r:
   # Native positive types plus a fixed, source-keyed negative sample. No label remapping.
   negatives=sorted([t for t in TYPES if t not in r['semantic_types']],key=lambda t:sha(r['pair_key']+t))[:3]
   hypotheses=['exists',*r['semantic_types'],*negatives]
  for typ in hypotheses:
   views=[]
   for v in r['views']:
    views.append(encode_view(tok,v['text'],HYPOTHESES[typ] if typ else None))
   row={**views[0],'view_encodings':views,'pair_key':r['pair_key'],'pmid':r['pmid']}
   if 'semantic_types' in r:
    row['targets']=[float(r['exists']),*[float(t in r['semantic_types']) for t in TYPES]]
    row['direction_target']=r.get('direction_target',-100)
    if typ:row['target']=float(r['exists'] if typ=='exists' else typ in r['semantic_types'])
   row['hypothesis']=typ;yield row

def document_loss(network,inputs,batch):
 import torch
 from .neural import tokenizer
 from .engine import collate
 global _TOK
 if '_TOK' not in globals():_TOK=tokenizer()
 flat=[v for r in batch for v in r['view_encodings']];enc=collate(flat,_TOK,'document',inputs['input_ids'].device);outputs=network(**enc);logits=outputs['logits'].float();offset=0;losses=[]
 for r in batch:
  v=logits[offset:offset+len(r['view_encodings'])];direction=outputs['direction'][offset:offset+len(r['view_encodings'])].float().mean(0);offset+=len(r['view_encodings'])
  pooled=torch.logsumexp(v,0)-math.log(len(v))
  target=torch.tensor(r['targets'],device=v.device);value=torch.nn.functional.binary_cross_entropy_with_logits(pooled,target,pos_weight=network.class_weights)
  if r.get('direction_target',-100)!=-100:value=value+.2*torch.nn.functional.cross_entropy(direction.unsqueeze(0),torch.tensor([r['direction_target']],device=v.device))
  losses.append(value)
 return torch.stack(losses).mean()

def nli_loss(network,inputs,batch):
 import torch
 from .neural import tokenizer
 from .engine import collate
 global _TOK
 if '_TOK' not in globals():_TOK=tokenizer()
 flat=[v for r in batch for v in r['view_encodings']];enc=collate(flat,_TOK,'nli',inputs['input_ids'].device);scores=network(**enc).float();offset=0;losses=[]
 for r in batch:
  v=scores[offset:offset+len(r['view_encodings'])];offset+=len(r['view_encodings']);pooled=torch.logsumexp(v,0)-math.log(len(v))
  i=0 if r.get('hypothesis','exists')=='exists' else TYPES.index(r['hypothesis'])+1
  losses.append(torch.nn.functional.binary_cross_entropy_with_logits(pooled,torch.tensor(r['target'],device=v.device),pos_weight=network.class_weights[i]))
 return torch.stack(losses).mean()

def fit(rows,mode):
 from .engine import train_network
 key=sha(canonical(sorted({r['pmid'] for r in rows})))[:18];path=HOME/'relation-models'/mode/key
 # Keep every positive bag. Mine difficult negatives plus a fixed random sample per TRAIN paper.
 # Held-out inference still scores every generated pair, with the complete gold denominator.
 supervised=[r for r in rows if r.get('semantic_supervised',True)];selected=[r for r in supervised if r['exists']];negative=defaultdict(list)
 for r in supervised:
  if not r['exists']:negative[r['pmid']].append(r)
 for pmid,group in sorted(negative.items()):
  hard=sorted(group,key=lambda r:(-int(any(v['same_sentence'] for v in r['views'])),-sum(r['id_features']),r['pair_key']))[:32];used={r['pair_key'] for r in hard}
  randoms=sorted([r for r in group if r['pair_key'] not in used],key=lambda r:sha(str(SEED)+r['pair_key']))[:32];selected.extend(hard+randoms)
 atomic(path/'sampling.json',{'all_candidate_pairs':len(rows),'semantically_supervised':len(supervised),'training_bags':len(selected),'positive_bags':sum(r['exists'] for r in selected),
  'negative_budget_per_paper':64,'hard':32,'random':32,'all_evaluation_candidates_retained':True,'source_definition_sha256':filehash(Path(__file__))},True)
 train_network(mode,list(encode(selected,mode)),path,document_loss if mode=='document' else nli_loss);return path

def score(rows,path,mode):
 import torch
 from .neural import tokenizer,Network
 from .engine import load_weights,setup,collate
 tok=tokenizer();dev=setup();model=load_weights(Network(mode,len(tok)),Path(path)/'model.safetensors').to(dev).eval();values={r['pair_key']:r for r in rows};result={};budget=RuntimeBudget();budget.start(mode+'-score')
 try:
  with torch.inference_mode():
   for encoded in encode([{k:v for k,v in r.items() if k not in ('semantic_types','exists','strict_types','direction_target','id_correct')} for r in rows],mode):
    budget.check()
    key=encoded['pair_key'];enc=collate(encoded['view_encodings'],tok,mode,dev)
    with torch.autocast('cuda',dtype=torch.bfloat16):v=model(**enc)
    logits=v['logits'].float() if mode=='document' else v.float();pooled=(torch.logsumexp(logits,0)-math.log(len(encoded['view_encodings']))).sigmoid().cpu().tolist()
    if mode=='document':result[key]={'exists':pooled[0],'types':pooled[1:],'direction':v['direction'].float().mean(0).softmax(-1).cpu().tolist()}
    else:
     entry=result.setdefault(key,{'exists':0.,'types':[0.]*len(TYPES)})
     if encoded['hypothesis']=='exists':entry['exists']=pooled
     else:entry['types'][TYPES.index(encoded['hypothesis'])]=pooled
  return result
 finally:budget.stop();del model;gc.collect();torch.cuda.empty_cache()
