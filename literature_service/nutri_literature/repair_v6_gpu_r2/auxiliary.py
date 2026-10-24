"""Native DrugProt and CG tasks; no conversion into BioRED correlations."""
import io,re,zipfile
from collections import Counter
from .common import *
from .text import pair_views

CG_COMMIT='b4a603dee25bd6ac5636017c2be41fd2edc52a3e'

def export_local():
 from ..corpora_v7 import read_drugprot,parse_standoff
 from .knowledge import download
 home=HOME/'auxiliary';receipt=home/'manifest.json'
 if receipt.exists():return read(receipt)
 protection=read(HOME/'data/protection.json');ids=set(protection['pmids']);texts=set(protection['text_hashes'])
 path=home/'raw/cg-official.zip';source=download('https://codeload.github.com/openbiocorpora/bionlp-st-2013-cg/zip/'+CG_COMMIT,path)
 cg=[];held_ids=set();held_text=set()
 with zipfile.ZipFile(path) as z:
  for name in z.namelist():
   if '/original-data/' not in name or not name.endswith('.txt'):continue
   match=re.fullmatch(r'PMID-(\d+)\.txt',Path(name).name)
   if not match:continue
   text=z.read(name).decode('utf8');pmid=match[1]
   if '/train/' not in name:held_ids.add(pmid);held_text.add(textkey(text));continue
   ann=parse_standoff(text,z.read(name[:-4]+'.a1').decode('utf8'),z.read(name[:-4]+'.a2').decode('utf8'))
   cg.append({'pmid':pmid,'text':text,**ann})
 # Do not silently let a late-discovered auxiliary test source contaminate NER/ID learning.
 external=read(HOME/'data/identity-train.json')['documents']
 overlap=[d['pmid'] for d in external if d['pmid'] in held_ids or textkey(d['text']) in held_text]
 if overlap:raise ValueError('CG 보호 원문과 독립 개체 학습 중복: '+canonical(overlap))
 ids|=held_ids;texts|=held_text
 drug=ROOT.parent/'final_2026/repos/nutriomics-evidence-engine/data/drugprot/drugprot-training-development-test-background.zip'
 rows=[];excluded=[];counts=Counter();vocab=set()
 def add(d,task,a,b,label):
  if a['start']<b['end'] and b['start']<a['end']:return
  views=pair_views(d['text'],{'mentions':[a]},{'mentions':[b]},1)
  if not views:return
  key=task+':'+label;vocab.add(key);rows.append({'pmid':d['pmid'],'task':task,'label':key,'text':views[0]['text'],'segments':views[0]['segments']});counts[task]+=1
 for task,docs in [('drugprot',read_drugprot(drug)),('cg',cg)]:
  for d in docs:
   if d['pmid'] in ids or textkey(d['text']) in texts:excluded.append({'task':task,'pmid':d['pmid']});continue
   if task=='drugprot':
    es={e['annotation_id']:e for e in d['entities']};labels={}
    for r in d['relations']:labels.setdefault((r['arg1'],r['arg2']),set()).add(r['type'])
    for a in d['entities']:
     if a['kind']!='component':continue
     for b in d['entities']:
      if b['kind']!='gene':continue
      a,b=dict(a),dict(b)
      if not any(s['start']<=min(a['start'],b['start']) and max(a['end'],b['end'])<=s['end'] for s in __import__(__package__+'.text',fromlist=['sentences']).sentences(d['text'])):continue
      for label in labels.get((a['annotation_id'],b['annotation_id']),{'NONE'}):add(d,task,a,b,label)
   else:
    spans=d['spans']
    for event in d['events'].values():
     a=spans[event['trigger']]
     if len(a['spans'])!=1:continue
     for arg in event['arguments']:
      target=d['events'][arg['target']]['trigger'] if arg['target'] in d['events'] else arg['target'];b=spans[target]
      if len(b['spans'])!=1:continue
      add(d,task,a['spans'][0],b['spans'][0],event['type']+'/'+re.sub(r'\d+$','',arg['role']))
 labels=sorted(vocab)
 if len(labels)>128:raise ValueError('보조 레이블 출력 차원 초과')
 for r in rows:r['target']=labels.index(r['label'])
 atomic(home/'train.json',{'rows':rows,'labels':labels,'heldout_labels_read':False},True)
 result={'rows':len(rows),'counts':dict(counts),'labels':labels,'excluded':excluded,'cg_source':source,'cg_held_source_papers':len(held_ids),'identity_overlap':overlap,
  'drugprot_sha256':filehash(drug),'data_sha256':filehash(home/'train.json'),'native_tasks_only':True,'relation_IDs_or_labels_from_protected_data':False}
 atomic(receipt,result,True);log('auxiliary-ready',rows=len(rows),counts=dict(counts));return result

def train():
 import torch
 from .engine import train_network
 from .neural import tokenizer
 value=read(HOME/'auxiliary/train.json');tok=tokenizer();rows=[]
 for r in value['rows']:
  enc=tok(r['task']+' native annotation: '+r['text'],max_length=512,truncation=True)
  rows.append({**enc,'target':r['target'],'task':r['task'],'pmid':r['pmid']})
 def loss(model,inputs,batch):
  scores=model(**inputs).float();targets=torch.tensor([r['target'] for r in batch],device=scores.device)
  # Each corpus sees only its own label space; NONE is not invented for unannotated CG roles.
  mask=torch.tensor([[label.startswith(r['task']+':') for label in value['labels']]+[False]*(128-len(value['labels'])) for r in batch],device=scores.device)
  return torch.nn.functional.cross_entropy(scores.masked_fill(~mask,-1e4),targets)
 return train_network('auxiliary',rows,HOME/'auxiliary/model',loss)
