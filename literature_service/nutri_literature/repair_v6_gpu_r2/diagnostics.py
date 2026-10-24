"""Stage losses use training annotations only; no development labels are loaded."""
from collections import Counter
from .common import *

def training_candidates():
 from .relations import native_truth
 docs=read(HOME/'data/relations-train.json')['documents'];counts=Counter();entities={k:Counter() for k in ('chemical','gene')};details=[]
 for d in docs:
  mentions=read(HOME/'prepared/train'/('paper-'+d['pmid']+'.json'))['mentions'];pairs=native_truth(d)
  for kind in entities:
   gold={(e['start'],e['end']) for e in d['entities'] if e['kind']==kind};pred={(m['start'],m['end']) for m in mentions if m['kind']==kind}
   entities[kind].update({'tp':len(gold&pred),'fp':len(pred-gold),'fn':len(gold-pred)})
  for a,b,t in sorted(pairs):
   ea=[e for e in d['entities'] if e['kind']=='chemical' and a in e.get('native_ids',[])];eb=[e for e in d['entities'] if e['kind']=='gene' and b in e.get('native_ids',[])]
   aa=[m for m in mentions if m['kind']=='chemical' and any(m['start']==e['start'] and m['end']==e['end'] for e in ea)];bb=[m for m in mentions if m['kind']=='gene' and any(m['start']==e['start'] and m['end']==e['end'] for e in eb)]
   stage='mention_missing' if not aa or not bb else 'retrieval_200_missing' if not any(a in m['candidate_ids'] for m in aa) or not any(b in m['candidate_ids'] for m in bb) else 'candidate_available'
   counts[stage]+=1;details.append({'pmid':d['pmid'],'gold':[a,b,t],'first_failure':stage})
 result={'gold_relations':sum(counts.values()),'first_failure':dict(counts),'candidate_recall':counts['candidate_available']/max(1,sum(counts.values())),
  'entity_counts':{k:dict(v) for k,v in entities.items()},'details':details,'development_labels_read':False,'all_relation_gold_denominator_retained':True}
 atomic(HOME/'diagnostics/training-candidates.json',result,True);log('candidate-training-diagnostic',**{k:result[k] for k in ('gold_relations','first_failure','candidate_recall')});return result

def ner_oof():
 docs=read(HOME/'data/identity-train.json')['documents'];pred=read(HOME/'identity/ner-oof.json')['predictions'];counts={k:Counter() for k in ('gene','chemical')}
 for d in docs:
  scope='chemical' if d.get('gene_annotation_complete') is False else 'gene';a={(e['start'],e['end']) for e in d['entities'] if e['kind']==scope};b={(e['start'],e['end']) for e in pred[d['pmid']] if e['kind']==scope}
  counts[scope].update({'tp':len(a&b),'fp':len(b-a),'fn':len(a-b)})
 result={}
 for kind,c in counts.items():
  p=c['tp']/max(1,c['tp']+c['fp']);r=c['tp']/max(1,c['tp']+c['fn']);result[kind]={**dict(c),'precision':p,'recall':r,'f1':2*p*r/max(1e-12,p+r)}
 atomic(HOME/'diagnostics/ner-oof.json',{'metrics':result,'papers':len(docs),'scope':'only the officially annotated entity type is scored','development_comparable':False,'source_sha256':filehash(HOME/'identity/ner-oof.json')},True);return result

def dictionary_pilot():
 from .dictionary_mentions import extract
 from .knowledge import Knowledge
 from .relations import native_truth
 docs=read(HOME/'data/relations-train.json')['documents'];kb=Knowledge();counts=Counter();new_mentions=0
 try:
  for number,d in enumerate(docs):
   paper=read(HOME/'prepared-ner-only/train'/('paper-'+d['pmid']+'.json'));mentions=list(paper['mentions']);seen={(m['kind'],m['start'],m['end']) for m in mentions}
   for m in extract(d['text'],kb):
    if (m['kind'],m['start'],m['end']) in seen:continue
    m['candidate_ids']=[v[0] for v in kb.lexical(m['kind'],m['text'],())];mentions.append(m);new_mentions+=1
   for a,b,t in native_truth(d):
    es=[[e for e in d['entities'] if e['kind']==kind and identifier in e.get('native_ids',[])] for kind,identifier in [('chemical',a),('gene',b)]]
    matches=[[m for m in mentions if any(m['kind']==e['kind'] and m['start']==e['start'] and m['end']==e['end'] for e in group)] for group in es]
    stage='mention_missing' if any(not v for v in matches) else 'retrieval_200_missing' if any(not any(identifier in m['candidate_ids'] for m in group) for identifier,group in zip((a,b),matches)) else 'candidate_available'
    counts[stage]+=1
   if number%50==0:log('dictionary-restoration-pilot',papers=number+1,total=len(docs))
  result={'papers':len(docs),'gold_relations':sum(counts.values()),'first_failure':dict(counts),'candidate_recall':counts['candidate_available']/sum(counts.values()),
   'added_mention_candidates':new_mentions,'pilot_only':True,'new_mentions_ID_search':'whole official lexical aliases, not the final dense+contextual reranker',
   'development_labels_read':False,'gold_used_to_construct_candidates':False,'source_sha256':filehash(Path(__file__).with_name('dictionary_mentions.py'))}
  atomic(HOME/'diagnostics/dictionary-abbreviation-pilot.json',result,True);return result
 finally:kb.close()
