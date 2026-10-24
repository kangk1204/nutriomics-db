from __future__ import annotations
import contextlib,hashlib,json,os,random,time,unicodedata
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
HOME=Path(os.environ.get('REPAIR_GPU_HOME',ROOT/'runtime/relation-repair-v6-gpu-r1'))
SEED=20261021
MODEL='microsoft/BiomedNLP-BiomedBERT-base-uncased-abstract-fulltext'
REVISION='e1354b7a3a09615f6aba48dfad4b7a613eef7062'
TYPES=('Association','Bind','Negative_Correlation','Positive_Correlation','Comparison','Conversion','Cotreatment','Drug_Interaction')
POLICY={'engine':'relation-repair-v6-gpu-r1','seed':SEED,'model':MODEL,'revision':REVISION,
 'precision':.85,'recall':65/154,'tp':65,'id_precision':.9,'id_minimum':50,
 'outer_folds':5,'inner_folds':3,'C':[.1,1.,10.],'thresholds':[i/100 for i in range(5,100,5)]+[.99],
 'epochs':3,'learning_rate':2e-5,'warmup':.1,'weight_decay':.01,'microbatch':4,'accumulation':8,
 'id_max_tokens':256,'relation_max_tokens':512,'overlap':64,'gpu_hours':72,'gpu_gib':13,'cpu_gib':8,
 'supported_taxa':[9606,10090,10116],'default_engine':'heuristic-v1','clinical_redesign':False,
 'id_candidate_limit':50,'id_ranker':'conditional-linear-auto-v1','id_training_gold_injection':False,
 'id_policy_per_kind':True,'id_training_predictions':'nested cross-fitted automatic NER and official aliases'}

def read(path):return json.loads(Path(path).read_text(encoding='utf8'))
def canonical(v):return json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def sha(v):return hashlib.sha256(v.encode('utf8') if isinstance(v,str) else v).hexdigest()
def filehash(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def textkey(s):return sha(' '.join(unicodedata.normalize('NFKC',s).casefold().split()))
def atomic(path,value,immutable=False):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);blob=canonical(value).encode('utf8')
 if path.exists() and immutable:
  if path.read_bytes()!=blob:raise ValueError('동결 산출물 불일치: '+str(path))
  return path
 temp=path.with_name(path.name+'.tmp-'+str(os.getpid()))
 with temp.open('xb') as f:f.write(blob);f.flush();os.fsync(f.fileno())
 os.replace(temp,path);return path
def log(stage,**v):print(canonical({'stage':stage,'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),**v}),flush=True)
def source_hashes():
 files=[*Path(__file__).parent.glob('*.py'),ROOT/'scripts/relation_repair_v6_gpu_r1.py',ROOT/'requirements-relation-v6-gpu-r1.txt']
 return {str(p.relative_to(ROOT)):filehash(p) for p in files if p.exists()}
def foldmap(documents,n,salt):
 # Connected components for PMID/PMCID/normalized text; deterministic balanced assignment.
 parent=list(range(len(documents)));seen={}
 def find(i):
  while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
  return i
 for i,d in enumerate(documents):
  keys=[('pmid',str(d['pmid'])),('text',textkey(d['text']))]
  if d.get('pmcid'):keys.append(('pmcid',str(d['pmcid'])))
  for k in keys:
   if k in seen:parent[find(i)]=find(seen[k])
   else:seen[k]=i
 groups={}
 for i,d in enumerate(documents):groups.setdefault(find(i),[]).append(str(d['pmid']))
 ordered=sorted(groups.values(),key=lambda g:sha(str(SEED)+salt+'|'.join(sorted(g))))
 loads=[0]*n;out={}
 for group in sorted(ordered,key=lambda g:-len(g)):
  f=min(range(n),key=lambda j:loads[j]);loads[f]+=len(group)
  out.update({p:f for p in group})
 return out
def metric(expected,predicted):
 a,b=set(expected),set(predicted);tp,fp,fn=len(a&b),len(b-a),len(a-b);p=tp/(tp+fp) if tp+fp else 0.;r=tp/(tp+fn) if tp+fn else 0.
 return dict(tp=tp,fp=fp,fn=fn,precision=p,recall=r,f1=2*p*r/(p+r) if p+r else 0.)
def wilson(ok,n):
 if not n:return 0.
 z=1.959963984540054;p=ok/n
 return (p+z*z/(2*n)-z*((p*(1-p)+z*z/(4*n))/n)**.5)/(1+z*z/n)
def exact(text,span):return 0<=span['start']<span['end']<=len(text) and text[span['start']:span['end']]==span['text']
@contextlib.contextmanager
def lock(home=HOME):
 import psutil
 home.mkdir(parents=True,exist_ok=True);p=home/'owner.json'
 if p.exists():
  old=read(p)
  if psutil.pid_exists(old['pid']):
   try:
    if abs(psutil.Process(old['pid']).create_time()-old['created'])<1:raise RuntimeError('이미 실행 중인 소유 작업')
   except psutil.NoSuchProcess:pass
 value={'pid':os.getpid(),'created':psutil.Process().create_time(),'started':time.time()};atomic(p,value)
 try:yield
 finally:atomic(p,{**value,'finished':time.time(),'running':False})

class RuntimeBudget:
 def __init__(self,home=HOME):
  self.home=home;self.path=(home/'gpu-budget.json').resolve();self.active=None
  if not self.path.exists():raise FileNotFoundError('수정본은 원래 GPU 예산 장부를 연결해야 함; 새 예산 생성 금지')
  if read(self.path)['limit_seconds']!=72*3600:raise ValueError('원래 72시간 GPU 한도 변경 금지')
 def start(self,stage):
  v=read(self.path)
  if v.get('active'):
   # A crashed process cannot reclaim unrecorded time. Charge conservatively.
   v['used_seconds']+=max(0.,time.time()-v['active']['heartbeat']);v['active']=None
  if v['used_seconds']>=v['limit_seconds']:raise TimeoutError('72 GPU시간 실험 예산 소진')
  self.active=(stage,time.monotonic());v['active']={'stage':stage,'heartbeat':time.time(),'pid':os.getpid()};atomic(self.path,v)
  return v['limit_seconds']-v['used_seconds']
 def check(self):
  if not self.active:return
  import psutil
  if psutil.Process().memory_info().rss>POLICY['cpu_gib']*1024**3:raise MemoryError('작업 CPU RSS 8GiB 예산 초과; 체크포인트 보존')
  v=read(self.path);elapsed=time.time()-v['active']['heartbeat']
  if v['used_seconds']+elapsed>=v['limit_seconds']:raise TimeoutError('72 GPU시간 실험 예산 소진; 체크포인트 보존')
  if elapsed>=20:
   v['used_seconds']+=elapsed;v['active']['heartbeat']=time.time();atomic(self.path,v)
 def stop(self):
  if self.active:
   stage,start=self.active;elapsed=time.monotonic()-start;v=read(self.path)
   v['used_seconds']+=max(0.,time.time()-v['active']['heartbeat']);v['active']=None
   v['sessions'].append({'stage':stage,'seconds':elapsed});atomic(self.path,v);self.active=None
