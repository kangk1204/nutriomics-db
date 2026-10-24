"""Continue owned run5 with a pinned operational guard; never edit training sources."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_evidence_v8 import PROJECT, ROOT, remote

EXPERIMENT = 'nutriomics-evidence-v8-run5'
SOURCE_SHA = 'fdf372bc72352bfc5e498afa4202ebe9de7d047d45ca916042679ccd359747f4'


REMOTE = r'''
import base64,fcntl,hashlib,json,os,psutil,subprocess,sys,time
from pathlib import Path
b=Path(PROJECT);h=b/'runtime'/EXPERIMENT;INPUT=json.loads(sys.stdin.buffer.read())
def read(p):return json.loads(p.read_text())
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def immutable(p,blob):
 p.parent.mkdir(parents=True,exist_ok=True)
 if p.exists() and p.read_bytes()!=blob:raise ValueError('Immutable resource continuation conflict: '+p.name)
 if not p.exists():p.write_bytes(blob)
def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':')).encode()
contract=read(h/'pipeline-contract.json');s=Path(contract['source_directory']);a=Path(contract['assets'])
if sha(s/'source-manifest.json')!=SOURCE_SHA or contract['source_manifest_sha256']!=SOURCE_SHA:
 raise ValueError('Different scientific source; this continuation is not applicable')
for name,digest in read(s/'source-manifest.json').items():
 p=(s/name).resolve()
 if not p.is_relative_to(s) or sha(p)!=digest:raise ValueError('Scientific source changed')
if sha(a/'asset-manifest.json')!=contract['asset_manifest_sha256']:raise ValueError('Assets changed')
policy={'user_id':os.getuid(),'maximum_total_desktop_mib':512,'additional_desktops':[
 {'executable':'/usr/bin/nautilus','maximum_mib':64},
 {'executable':'/opt/google/chrome/chrome','maximum_mib':256}]}
files={name:base64.b64decode(blob) for name,blob in INPUT.items()}
files['desktop-policy.json']=encoded(policy)
predecessors={str(p.relative_to(h)):sha(p) for p in sorted((h/'pipeline-logs').glob('*.complete.json'))}
config={'schema_version':'v8-resource-admission-v1','scientific_source_manifest_sha256':SOURCE_SHA,
 'files':{name:hashlib.sha256(blob).hexdigest() for name,blob in files.items()},'predecessor_receipts':predecessors}
blob=encoded(config);digest=hashlib.sha256(blob).hexdigest();d=h/'runtime-control'/('desktop-'+digest[:16])
for name,value in files.items():immutable(d/name,value)
immutable(d/'execution.json',blob)
execution={'configuration':str(d/'execution.json'),'sha256':digest,
 'scientific_source_manifest_sha256':SOURCE_SHA,'files':config['files']}
if PHASE=='prepare':
 if (d/'probe-receipt.json').exists():
  receipt=read(d/'probe-receipt.json')
  if receipt['returncode']!=0:raise ValueError('Previous real GPU probe did not pass')
  print(json.dumps({'status':'probe_already_passed','execution':execution,'probe':receipt}));raise SystemExit(0)
 # Probe in another owned home: never replace the live training state.
 ph=b/'runtime/verification'/('desktop-'+digest[:16]);pd=ph/'runtime-control'/d.name
 for name,value in {**files,'execution.json':blob}.items():immutable(pd/name,value)
 immutable(ph/'resource-policy.json',(h/'resource-policy.json').read_bytes())
 env={**os.environ,'OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1'}
 proc=subprocess.run([str(b/'.venv/bin/python'),'-B',str(pd/'v8_resource_layer.py'),
  '--configuration',str(pd/'execution.json'),'--configuration-sha256',digest,
  '--source',str(s),'--home',str(ph),'--stage','probe'],capture_output=True,text=True,env=env,timeout=90)
 receipt={'configuration_sha256':digest,'returncode':proc.returncode,'stdout':proc.stdout,'stderr':proc.stderr,
  'source_files_verified':len(read(s/'source-manifest.json')),'observed_at':time.time()}
 immutable(d/'probe-receipt.json',encoded(receipt))
 if proc.returncode:raise ValueError('Actual GPU probe failed: '+proc.stdout[-700:]+proc.stderr[-300:])
 print(json.dumps({'status':'probe_passed','execution':execution,'probe':receipt}));raise SystemExit(0)
if PHASE!='activate':raise ValueError('Unknown continuation phase')
if read(d/'probe-receipt.json')['returncode']!=0:raise ValueError('No successful real GPU probe')
with (h/'resume-owner.lock').open('a') as owner:
 fcntl.flock(owner,fcntl.LOCK_EX)
 state=read(h/'pipeline-state.json')
 if (h/'resource-execution-current.json').exists():
  if read(h/'resource-execution-current.json')!=execution:raise ValueError('Different continuation already selected')
  if state.get('resource_execution')==execution and psutil.pid_exists(state.get('pid',0)):
   print(json.dumps({'status':'already_running','execution':execution,'pid':state['pid']}));raise SystemExit(0)
 if state.get('status') not in {'waiting','cancelled'} or (state.get('child_pid') and psutil.pid_exists(state['child_pid'])):
  raise ValueError('Only an idle owned resource wait may be continued')
 oldpid=state['pid']
 immutable(d/'predecessor-pipeline-state.json',(h/'pipeline-state.json').read_bytes())
 # Use the existing supervisor's cooperative cancellation; no signal to unrelated processes.
 cancel=h/'cancel-request.json'
 if not cancel.exists():cancel.write_bytes(encoded({'reason':'verified_desktop_guard_continuation','at':time.time()}))
 for _ in range(20):
  if not psutil.pid_exists(oldpid) or psutil.Process(oldpid).status()==psutil.STATUS_ZOMBIE:break
  time.sleep(2)
 else:raise ValueError('Owned supervisor has not stopped; cancellation retained')
 lock=(h/'pipeline-owner.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 if any(sha(h/name)!=value for name,value in predecessors.items()):raise ValueError('Completed receipts changed')
 # Full bytes of completed model weights and scientific/cache manifests are audited before continuation.
 protected={str(p.relative_to(h)):sha(p) for p in sorted(h.rglob('*')) if p.is_file() and
  ('runtime-control' not in p.parts) and (p.name in {'model.safetensors','complete.json','pipeline-contract.json',
    'gpu-ledger.json','resource-policy.json'} or p.name.endswith('.complete.json'))}
 immutable(d/'checkpoint-preservation.json',encoded({'files':protected,'observed_at':time.time()}))
 immutable(d/'predecessor-cancel-request.json',cancel.read_bytes());cancel.unlink()
 immutable(h/'resource-execution-current.json',encoded(execution))
 runners=b/'runtime/runners';runners.mkdir(exist_ok=True)
 immutable(runners/(config['files']['supervisor.py']+'.py'),files['supervisor.py'])
 fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
 with (d/'supervisor.log').open('ab',buffering=0) as out:
  proc=subprocess.Popen([str(b/'.venv/bin/python'),'-u','-B',str(d/'supervisor.py'),'--source',str(s),
   '--home',str(h),'--assets',str(a),'--resource-execution',str(d/'execution.json'),
   '--resource-execution-sha256',digest],stdout=out,stderr=subprocess.STDOUT,start_new_session=True,
   env={**os.environ,'PYTHONUNBUFFERED':'1'})
 receipt={'status':'resumed','pid':proc.pid,'execution':execution,'preserved_files':len(protected),
  'checkpoint_manifest_sha256':sha(d/'checkpoint-preservation.json'),'old_supervisor_pid':oldpid,
  'other_jobs_modified':False,'additional_model_calls':0,'at':time.time()}
 immutable(d/'launch-receipt.json',encoded(receipt));print(json.dumps(receipt))
'''


def deploy(phase):
    files = {'v8_resource_layer.py': Path(__file__).with_name('v8_resource_layer.py'),
             'supervisor.py': Path(__file__).with_name('run_evidence_v8_pipeline.py')}
    payload = {name:base64.b64encode(path.read_bytes()).decode() for name,path in files.items()}
    prefix = '\n'.join(name+'='+repr(value) for name,value in {
        'PROJECT':PROJECT,'EXPERIMENT':EXPERIMENT,'SOURCE_SHA':SOURCE_SHA,'PHASE':phase}.items())+'\n'
    receipt = json.loads(remote('100.84.183.6',prefix+REMOTE,json.dumps(payload).encode(),timeout=180))
    home=ROOT/'literature_service/runtime'/EXPERIMENT
    path=home/('resource-'+phase+'-receipt.json')
    path.write_text(json.dumps(receipt,sort_keys=True),encoding='utf8')
    if phase=='activate':
        expected=home/'resource-execution-expected.json'
        value=json.dumps(receipt['execution'],sort_keys=True)
        if expected.exists() and expected.read_text(encoding='utf8')!=value:
            raise ValueError('Local approved resource execution changed')
        expected.write_text(value,encoding='utf8')
    return receipt


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['prepare','activate'])
    print(json.dumps(deploy(parser.parse_args().phase),ensure_ascii=False))
