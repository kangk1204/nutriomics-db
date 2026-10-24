"""Alias-only contrastive retrieval: no corpus labels enter the shared retriever."""
import gc,heapq,sqlite3
from functools import lru_cache
from .common import *
from .knowledge import Knowledge

def records(kb):
 # Stable source rows, including historical records; deduplication happens by native ID after retrieval.
 for source,db in enumerate((kb.base,kb.overlay)):
  for row in db.execute('SELECT * FROM records ORDER BY rid'):
   v=dict(row);v['names']=json.loads(v['names']);yield source,v

def train_aliases():
 from .neural import tokenizer
 from .engine import train_network
 kb=Knowledge();tok=tokenizer();sample=[];seen=set()
 for _,r in records(kb):
  key=(r['kind'],r['native_id'])
  if key in seen:continue
  seen.add(key)
  if len(r['names'])<2:continue
  priority=int(sha(r['kind']+r['native_id'])[:16],16)
  item=(-priority,r['native_id'],r)
  if len(sample)<20000:heapq.heappush(sample,item)
  elif priority<-sample[0][0]:heapq.heapreplace(sample,item)
 rows=[]
 for _,_,r in sorted(sample,key=lambda v:(-v[0],v[1])):
  names=sorted(r['names'],key=lambda n:sha(r['native_id']+n));prefix=r['kind']+'; '
  encoded=tok(prefix+names[0],truncation=True,max_length=64)
  rows.append({**encoded,'positive':prefix+names[1],'target_key':r['kind']+':'+r['native_id']})
 kb.close();return train_network('dual',rows,HOME/'identity/alias-dual',dual_loss)

def dual_loss(network,inputs,batch):
 import torch
 from .neural import tokenizer
 global _TOK
 if '_TOK' not in globals():_TOK=tokenizer()
 q=network(**inputs);positive=_TOK([r['positive'] for r in batch],max_length=64,truncation=True,padding=True,return_tensors='pt').to(q.device)
 p=network(**positive);scores=(q.float()@p.float().T)/.05
 mask=torch.tensor([[a['target_key']==b['target_key'] for b in batch] for a in batch],device=q.device)
 return (torch.logsumexp(scores,1)-torch.logsumexp(scores.masked_fill(~mask,-1e4),1)).mean()

def build_index():
 import numpy as np,torch
 from .neural import tokenizer,Network
 from .engine import setup,load_weights
 path=HOME/'knowledge/dense-manifest.json'
 if path.exists():
  v=read(path)
  if filehash(HOME/'knowledge/dense.npy')!=v['vector_sha256']:raise ValueError('문맥 벡터 캐시 변경')
  return v
 tok=tokenizer();dev=setup();model=load_weights(Network('dual',len(tok)),HOME/'identity/alias-dual/model.safetensors').to(dev).eval();kb=Knowledge()
 count=sum(db.execute('SELECT count(*) FROM records').fetchone()[0] for db in (kb.base,kb.overlay))
 vector=HOME/'knowledge/dense.npy';array=np.lib.format.open_memmap(vector,mode='w+',dtype=np.float16,shape=(count,256))
 meta=HOME/'knowledge/dense-rows.sqlite3'
 if meta.exists():meta.rename(meta.with_name('dense-rows.incomplete.'+str(time.time_ns())+'.sqlite3'))
 db=sqlite3.connect(meta);db.execute('CREATE TABLE rows(n INTEGER PRIMARY KEY,kind TEXT,identifier TEXT,taxon INTEGER)');batch=[];index=0;budget=RuntimeBudget();budget.start('dense-index')
 def flush(batch,index):
  if not batch:return index
  texts=[kb.candidate_text(r) for _,r in batch];encoded=tok(texts,max_length=128,truncation=True,padding=True,return_tensors='pt').to(dev)
  with torch.inference_mode(),torch.autocast('cuda',dtype=torch.bfloat16):v=model(**encoded).float().cpu().numpy()
  array[index:index+len(batch)]=v
  db.executemany('INSERT INTO rows VALUES(?,?,?,?)',[(index+i,r['kind'],r['native_id'],r.get('taxon')) for i,(_,r) in enumerate(batch)])
  budget.check();return index+len(batch)
 try:
  for item in records(kb):
   batch.append(item)
   if len(batch)==128:
    index=flush(batch,index);batch=[]
    if index%10000<128:db.commit()
    if index%100000<128:log('dense-index-progress',rows=index,total=count)
  index=flush(batch,index);db.commit();array.flush();assert index==count
  result={'rows':count,'dimensions':256,'weights_sha256':filehash(HOME/'identity/alias-dual/model.safetensors'),
   'vector_sha256':filehash(vector),'metadata_sha256':filehash(meta),'tokenizer_revision':REVISION,
   'knowledge_sha256':filehash(HOME/'knowledge/manifest.json'),'source_only_alias_training':True}
  atomic(path,result,True);return result
 finally:budget.stop();db.close();kb.close();del array,model;gc.collect();torch.cuda.empty_cache()

class Retriever:
 def __init__(self):
  import numpy as np,torch
  from .neural import tokenizer,Network
  from .engine import load_weights,setup
  self.dev=setup();self.tok=tokenizer();self.model=load_weights(Network('dual',len(self.tok)),HOME/'identity/alias-dual/model.safetensors').to(self.dev).eval()
  self.vectors=torch.tensor(np.load(HOME/'knowledge/dense.npy',mmap_mode='r'),device=self.dev,dtype=torch.float16)
  self.db=sqlite3.connect((HOME/'knowledge/dense-rows.sqlite3').resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
  self.kinds={kind:torch.tensor([r[0] for r in self.db.execute('SELECT n FROM rows WHERE kind=?',(kind,))],device=self.dev) for kind in ('gene','chemical')}
  self.kind_vectors={kind:self.vectors[at] for kind,at in self.kinds.items()};del self.vectors
  self.budget=RuntimeBudget();self.budget.start('automatic-candidate-retrieval')
 @lru_cache(maxsize=16000)
 def query(self,kind,text,k=200):
  import torch
  self.budget.check()
  enc=self.tok([kind+'; '+text],truncation=True,max_length=128,return_tensors='pt').to(self.dev)
  with torch.inference_mode(),torch.autocast('cuda',dtype=torch.bfloat16):v=self.model(**enc).to(torch.float16)
  indices=self.kinds[kind];scores=(self.kind_vectors[kind]@v[0]).float();values,at=scores.topk(min(k,len(indices)))
  out={}
  for index,score in zip(indices[at].cpu().tolist(),values.cpu().tolist()):
   row=self.db.execute('SELECT identifier FROM rows WHERE n=?',(index,)).fetchone();out.setdefault(row[0],score)
  return list(out.items())
 def close(self):
  import torch
  self.budget.stop();self.query.cache_clear();self.db.close();del self.model,self.kind_vectors;gc.collect();torch.cuda.empty_cache()
