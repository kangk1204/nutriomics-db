"""Automatic mention and ID candidates receive source text only."""
from .common import *
from .knowledge import Knowledge,namekey
from .text import definitions,species,normal


def contextual_candidates(document,m,retriever):
 text=document['text'];start=max(0,m['start']-140);end=min(len(text),m['end']+140)
 context=m['text']+'; context: '+text[start:end]
 values=retriever.query(m['kind'],context)
 lexical={v:i for i,v in enumerate(m.get('lexical_ids',[]))};dense={v:i for i,v in enumerate(m.get('dense_ids',[]))}
 contextual={identifier:i for i,(identifier,score) in enumerate(values)}
 exact=set(m.get('exact_ids',[]));old=m.get('previous_id')
 def rank(identifier):
  score=sum(1/(60+ranks[identifier]) for ranks in (lexical,dense,contextual) if identifier in ranks)
  return (identifier not in exact,-score,identifier)
 ids=sorted(set(m['candidate_ids'])|set(contextual),key=rank)[:200]
 if old and old not in ids:ids=ids[:199]+[old]
 return {**m,'candidate_ids':ids,'contextual_ids':[v[0] for v in values],
         'contextual_scores':{v:float(score) for v,score in values},'context_source_sha256':sha(text[start:end])}

def candidate_mentions(document,mentions,kb,retriever):
 text=document['text'];aliases=definitions(text);cells=kb.cell_species(text);out=[]
 for m in mentions:
  if not exact(text,m):raise ValueError('원문과 자동 개체 위치가 다름')
  expansions=tuple(sorted(aliases.get(normal(m['text']),set())))
  lex=list(kb.lexical(m['kind'],m['text'],expansions));dense=retriever.query(m['kind'],m['text'])
  lr={r[0]:i for i,r in enumerate(lex)};dr={r[0]:i for i,r in enumerate(dense)};exact_ids={r[0] for r in lex if r[1]}
  local=species(text[max(0,m['start']-180):m['end']+180]);doc_tax=species(text)
  cell_evidence=[c for c in cells if abs(c['start']-m['start'])<=500]
  supported=local or sorted({c['taxon'] for c in cell_evidence}) or doc_tax
  all_ids=set(lr)|set(dr)
  def rank(identifier):
   r=kb.by_id(m['kind'],identifier);tax=(r or {}).get('taxon')
   score=(1/(60+lr[identifier]) if identifier in lr else 0)+(1/(60+dr[identifier]) if identifier in dr else 0)
   return (identifier not in exact_ids,int(m['kind']=='gene' and len(supported)==1 and tax not in supported),-score,identifier)
  ids=sorted(all_ids,key=rank)[:200];eligible=[]
  for identifier in exact_ids:
   record=kb.by_id(m['kind'],identifier)
   if m['kind']=='chemical' or (len(supported)==1 and record and record['taxon'] in supported):eligible.append(identifier)
  previous=eligible[0] if len(eligible)==1 else None
  out.append({**m,'key':sha(str((m['kind'],m['start'],m['end'])))[:20],'candidate_ids':ids,
   'lexical_ids':[v[0] for v in lex],'dense_ids':[v[0] for v in dense],'exact_ids':sorted(exact_ids),
   'previous_id':previous,'local_taxa':local,'document_taxa':doc_tax,'cell_evidence':cell_evidence,'supported_taxa':supported,
   'source_sha256':sha(text),'retrieval_uses_gold':False})
 return [contextual_candidates(document,m,retriever) for m in out]

def rebuild():
 from .entities import predict
 from .dense import Retriever
 root=HOME/'prepared';receipt=root/'manifest.json'
 if receipt.exists():return read(receipt)
 external=read(HOME/'data/identity-train.json')['documents'];ner_oof=read(HOME/'identity/ner-oof.json')['predictions']
 documents={'train':read(HOME/'data/relations-train.json')['documents'],'development':read(HOME/'data/development-sources.json')['documents']}
 ner={}
 for split,docs in documents.items():
  path=HOME/'prepared'/('ner-'+split+'.json')
  if not path.exists():atomic(path,predict([{'pmid':d['pmid'],'text':d['text']} for d in docs],HOME/'identity/ner-full'),True)
  ner[split]=read(path)
 kb=Knowledge();retriever=Retriever();files=[]
 try:
  for split,docs in [('train',documents['train']),('development',documents['development']),('normalization',external)]:
   for n,d in enumerate(docs):
    path=root/split/('paper-'+d['pmid']+'.json')
    if not path.exists():
     raw=ner_oof[d['pmid']] if split=='normalization' else ner[split][d['pmid']]
     from .dictionary_mentions import extract
     seen={(m['kind'],m['start'],m['end']) for m in raw}
     raw=raw+[m for m in extract(d['text'],kb) if (m['kind'],m['start'],m['end']) not in seen]
     source={'pmid':d['pmid'],'text':d['text']};values=candidate_mentions(source,raw,kb,retriever)
     atomic(path,{'pmid':d['pmid'],'text':d['text'],'source_sha256':sha(d['text']),'mentions':values,'source_only':True},True)
    files.append({'path':str(path.relative_to(HOME)),'sha256':filehash(path)})
    if n%25==0:log('automatic-candidates',split=split,papers=n+1,total=len(docs))
  result={'files':files,'normalization':len(external),'relation_train':len(documents['train']),'development':100,
   'ner_full_sha256':filehash(HOME/'identity/ner-full/model.safetensors'),'ner_oof_sha256':filehash(HOME/'identity/ner-oof.json'),
   'dense_sha256':filehash(HOME/'knowledge/dense-manifest.json'),'knowledge_sha256':filehash(HOME/'knowledge/manifest.json'),
   'chemical_categories_sha256':filehash(HOME/'knowledge/chemical-categories.json'),'mention_strategy':'NER plus whole official literal aliases; no gold whitelist',
   'legacy_predictions_used':False,'development_has_gold':False};atomic(receipt,result,True);return result
 finally:retriever.close();kb.close()
