"""Fetch an inspectable execution snapshot without reading any held-out labels."""
import sys,json,subprocess
from pathlib import Path
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v6_gpu.common import HOME,atomic,read
from nutri_literature.repair_v3.common import budget_status
HOST='keunsoo@100.93.148.94'
REMOTE='/home/keunsoo/projects/nutriomics-relation-v6-gpu'
code=f'''import sys,json,psutil,time
from pathlib import Path
r=Path({REMOTE!r});sys.path.insert(0,str(r))
from nutri_literature.repair_v6_gpu.runner import status
h=r/'runtime/relation-repair-v6-gpu';v=status();v['host']={HOST!r};v['project']={REMOTE!r}
v['prepared_papers']={{k:len(list((h/'prepared'/k).glob('paper*.json'))) for k in ['train','development','normalization','id-supervision']}}
v['job']=json.loads((h/'job-state.json').read_text())
if v['job'].get('running'):
 try:p=psutil.Process(v['job']['pid']);v['job']['process_alive']=p.is_running();v['job']['cpu_rss_gib']=round(p.memory_info().rss/1024**3,3)
 except psutil.NoSuchProcess:v['job']['process_alive']=False
for name in ['ner-oof','training-candidates-ner-only','dictionary-pilot','dictionary-abbreviation-pilot','training-candidates']:
 p=h/'diagnostics'/(name+'.json')
 if p.exists():
  d=json.loads(p.read_text());v[name]={{k:x for k,x in d.items() if k not in ['details','source_sha256']}}
models={{}}
for name,pattern,expected in [('identity','identity/rankers/*/complete.json',24),('document','relation-models/document/*/complete.json',24),('nli','relation-models/nli/*/complete.json',24),('auxiliary','auxiliary/model/complete.json',1)]:
 receipts=[]
 for p in h.glob(pattern):
  d=json.loads(p.read_text());receipts.append({{'path':str(p.relative_to(h)),'rows':d['rows'],'epochs':d['epochs'],'losses':d['losses'],'elapsed_seconds':d['elapsed_seconds'],'peak_gpu_mib':d['peak_gpu_mib'],'weights_sha256':d['weights_sha256']}})
 models[name]={{'completed':len(receipts),'planned':expected,'receipts':receipts}}
v['model_progress']=models
v['current_events']=[]
log=r/'runtime/pipeline-controller.log'
if log.exists():
 with log.open('rb') as f:
  f.seek(max(0,log.stat().st_size-65536));tail=f.read().decode('utf8',errors='replace')
 for line in tail.splitlines():
  try:
   event=json.loads(line)
   if isinstance(event,dict) and 'stage' in event:v['current_events'].append(event)
  except json.JSONDecodeError:pass
 v['current_events']=v['current_events'][-3:]
budget=v.get('gpu_budget',{{}});active=budget.get('active');used=budget.get('used_seconds',0.)
if active:used+=max(0.,time.time()-active['heartbeat'])
v['gpu_budget_live']={{'used_hours':used/3600,'remaining_hours':max(0.,budget.get('limit_seconds',0.)-used)/3600,'active_stage':active.get('stage') if active else None}}
preflight=h/'operational/relation-workload-preflight.json'
if preflight.exists():
 d=json.loads(preflight.read_text());v['relation_workload_preflight']={{k:x for k,x in d.items() if k!='details'}}
identity_analysis=h/'operational/id-disabled-analysis.json'
if identity_analysis.exists():
 d=json.loads(identity_analysis.read_text())
 v['id_disabled_analysis']={{k:x for k,x in d.items() if k!='folds'}}
 v['id_disabled_analysis']['folds']=[{{**{{k:x for k,x in f.items() if k!='automatic'}},'automatic':{{k:x for k,x in f['automatic'].items() if k!='training_only_examples'}}}} for f in d['folds']]
else:
 identity_progress=h/'operational/identity-progress-diagnostics.json'
 if identity_progress.exists():v['identity_progress_diagnostics']=json.loads(identity_progress.read_text())
worker=h/'operational/id-diagnostic-worker.json'
if worker.exists():v['id_diagnostic_worker']=json.loads(worker.read_text())
ceiling=h/'operational/dictionary-only-relation-ceiling.json'
if ceiling.exists():v['conditional_dictionary_only_relation_ceiling']={{k:x for k,x in json.loads(ceiling.read_text()).items() if k!='details'}}
print(json.dumps(v))
'''
if __name__=='__main__':
 response=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes',HOST,'python3','-'],input=code.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=40,check=True)
 remote=json.loads(response.stdout);value={'as_of_kst':datetime.now(timezone(timedelta(hours=9))).isoformat(),'server':remote,'LLM_budget':budget_status(),
  'historical_development_baseline':{'papers':100,'gold':154,'tp':65,'fp':63,'fn':89,'precision':65/128,'recall':65/154},
  'new_development_evaluation_complete':(HOME/'evaluation.json').exists(),'target':{'tp':65,'precision':.85,'recall':65/154},'default_engine':'heuristic-v1'}
 for key,path in [('teacher','teacher/worker.json'),('local_finisher','local-finisher/state.json')]:
  if (HOME/path).exists():value[key]=read(HOME/path)
 atomic(HOME/'execution-status.json',value);print(json.dumps(value,ensure_ascii=False,indent=2))
