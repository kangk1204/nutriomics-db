"""Two BIO heads prevent incomplete chemical/gene annotation becoming negatives."""
import gc
from .common import *
from .text import definitions

def rows(documents):
 from .neural import tokenizer
 tok=tokenizer();out=[]
 for d in documents:
  encoded=tok(d['text'],max_length=512,stride=64,truncation=True,return_overflowing_tokens=True,return_offsets_mapping=True)
  scope=[d.get('gene_annotation_complete') is not False,d.get('gene_annotation_complete') is False]
  for n,offsets in enumerate(encoded['offset_mapping']):
   labels=[[-100,-100] if a==b else [0 if scope[k] else -100 for k in range(2)] for a,b in offsets]
   for e in d['entities']:
    kind=0 if e['kind']=='gene' else 1 if e['kind']=='chemical' else None
    if kind is None or not scope[kind]:continue
    first=True
    for i,(a,b) in enumerate(offsets):
     if a<b and a>=e['start'] and b<=e['end']:
      labels[i][kind]=1 if first else 2;first=False
   value={k:encoded[k][n] for k in ('input_ids','attention_mask','token_type_ids') if k in encoded}
   out.append({**value,'labels':labels,'pmid':d['pmid']})
 return out

def loss(network,inputs,batch):
 import torch
 labels=inputs.pop('labels');output=network(**inputs)
 return torch.nn.functional.cross_entropy(output.float().reshape(-1,3),labels.reshape(-1),ignore_index=-100)

def train(documents,home):
 from .engine import train_network
 return train_network('ner',rows(documents),home,loss)

def predict(documents,path):
 import torch
 from .neural import Network,tokenizer
 from .engine import load_weights,setup
 tok=tokenizer();dev=setup();network=load_weights(Network('ner',len(tok)),Path(path)/'model.safetensors').to(dev).eval();result={};budget=RuntimeBudget();budget.start('ner-predict')
 try:
  with torch.inference_mode():
   for d in documents:
    budget.check()
    enc=tok(d['text'],max_length=512,stride=64,truncation=True,return_overflowing_tokens=True,return_offsets_mapping=True);found={}
    for n,offsets in enumerate(enc['offset_mapping']):
     inputs={k:torch.tensor([enc[k][n]],device=dev) for k in ('input_ids','attention_mask','token_type_ids') if k in enc}
     with torch.autocast('cuda',dtype=torch.bfloat16):prob=network(**inputs).float().softmax(-1)[0].cpu()
     for kind,head in [('gene',0),('chemical',1)]:
      current=None
      def emit():
       if current is not None:
        a,b,values=current;key=(kind,a,b);v={'kind':kind,'start':a,'end':b,'text':d['text'][a:b],'ner_score':sum(values)/len(values),'source':'independent-ner'}
        if key not in found or v['ner_score']>found[key]['ner_score']:found[key]=v
      for i,(a,b) in enumerate(offsets):
       label=int(prob[i,head].argmax());score=float(prob[i,head,label])
       if a==b or label==0:emit();current=None;continue
       if label==1 or current is None or a>current[1]+1:emit();current=[a,b,[score]]
       else:current[1]=b;current[2].append(score)
      emit()
    # Do not use ID availability or a relation prediction to remove NER candidates.
    result[d['pmid']]=sorted(found.values(),key=lambda e:(e['start'],e['end'],e['kind']))
  return result
 finally:budget.stop();del network;gc.collect();torch.cuda.empty_cache()

def prepare():
 docs=read(HOME/'data/identity-train.json')['documents'];root=HOME/'identity';root.mkdir(exist_ok=True)
 split=foldmap(docs,5,'independent-ner-id');all_predictions={};folds=[]
 for f in range(5):
  train_docs=[d for d in docs if split[d['pmid']]!=f];held=[d for d in docs if split[d['pmid']]==f];path=root/('ner-fold-'+str(f))
  receipt=train(train_docs,path);out=path/'predictions.json'
  if not out.exists():atomic(out,predict(held,path),True)
  values=read(out);all_predictions.update(values);folds.append({'fold':f,'training_pmids':[d['pmid'] for d in train_docs],'held_pmids':[d['pmid'] for d in held],'weights':receipt['weights_sha256']})
  log('ner-fold-complete',fold=f,held=len(held))
 atomic(root/'ner-oof.json',{'predictions':all_predictions,'folds':folds,'external_identity_only':True},True)
 train(docs,root/'ner-full');atomic(root/'ner-ready.json',{'folds':folds,'documents':len(docs),'no_biored_task_labels':True},True)
 return folds
