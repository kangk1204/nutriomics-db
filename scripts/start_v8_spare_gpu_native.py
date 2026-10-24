"""Start the already approved native tasks on one authenticated idle GPU peer.

Only official, previously audited training inputs leave the local machine. The
main 100-paper evaluation, labels, existing r2 job, and budgets are unchanged.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

from deploy_evidence_v8 import PROJECT, ROOT, remote

HOST = '100.68.188.102'
EXPERIMENT = 'nutriomics-native-v8-laptop-r1'
GPU = 'GPU-14c2b836-2cfd-fac1-4503-ed17f7d10d49'


def inputs_archive():
    directory = ROOT / 'literature_service/runtime/v8-deployment'
    files = {}
    with zipfile.ZipFile(directory / 'native-prepared-v2.zip') as archive:
        files['native-train.json'] = archive.read('inputs/native-train.json')
        files['manifest.json'] = archive.read('inputs/prior-manifest.json')
    files['protection.json'] = (ROOT / 'literature_service/runtime/relation-repair-v6-gpu/data/protection.json').read_bytes()
    prior = json.loads(files['manifest.json'])
    if (hashlib.sha256(files['native-train.json']).hexdigest() != prior['data_sha256']
            or prior['native_tasks_only'] is not True or prior['identity_overlap']
            or prior['relation_IDs_or_labels_from_protected_data'] is not False):
        raise ValueError('Previously audited train-only input contract changed')
    if hashlib.sha256(files['protection.json']).hexdigest() != '2224f16aafd174b592f6b24db892af3dfc3f0cd6c422132930fc606a9117e78d':
        raise ValueError('Source-only protection receipt changed')
    files['asset-manifest.json'] = json.dumps({'files': {
        name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
        'training_only': True, 'sealed_labels_exported': False}, sort_keys=True).encode()
    runner = (ROOT / 'scripts/run_evidence_v8_pipeline.py').read_bytes()
    files['runner.py'] = runner
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, blob in files.items():
            archive.writestr(name, blob)
    return buffer.getvalue(), hashlib.sha256(runner).hexdigest()


def start():
    blob, runner_sha = inputs_archive()
    source_receipt = json.loads((ROOT / 'literature_service/runtime/v8-deployment/source-100.68.188.102.json').read_text())
    code = r'''
import hashlib,io,json,os,subprocess,sys,time,zipfile
from pathlib import Path
b=Path(PROJECT);h=b/'runtime'/EXPERIMENT;s=Path(SOURCE);h.mkdir(parents=True,exist_ok=True)
manifest=s/'source-manifest.json';source_hash=hashlib.sha256(manifest.read_bytes()).hexdigest()
for name,digest in json.loads(manifest.read_text()).items():
 p=(s/name).resolve()
 if not p.is_relative_to(s.resolve()) or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('Pinned source changed')
if (h/'spare-launch.json').exists():raise ValueError('Existing spare GPU launch must be inspected, never duplicated')
asset=h/'import';asset.mkdir(exist_ok=True)
with zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read())) as archive:
 for name in archive.namelist():
  p=(asset/name).resolve()
  if not p.is_relative_to(asset.resolve()):raise ValueError('Input archive escaped experiment')
  value=archive.read(name)
  if p.exists() and p.read_bytes()!=value:raise ValueError('Immutable input changed')
  if not p.exists():p.write_bytes(value)
for name,digest in json.loads((asset/'asset-manifest.json').read_text())['files'].items():
 if hashlib.sha256((asset/name).read_bytes()).hexdigest()!=digest:raise ValueError('Training input hash')
runner=asset/'runner.py'
if hashlib.sha256(runner.read_bytes()).hexdigest()!=RUNNER_SHA:raise ValueError('Pinned runner changed')
policy={'forbidden_uuids':[],'desktop_allowlist':[],'selected_gpu_uuid':GPU,'gpu_gib':13,'rss_gib':8}
(h/'resource-policy.json').write_text(json.dumps(policy,sort_keys=True))
launcher=r"""import fcntl,hashlib,json,os,subprocess,sys,time
from pathlib import Path
b=Path(sys.argv[1]);h=Path(sys.argv[2]);s=Path(sys.argv[3]);gpu=sys.argv[4]
state=h/'spare-state.json'
def write(value):
 value.update(pid=os.getpid(),updated_at=time.time());q=state.with_suffix('.part');q.write_text(json.dumps(value,sort_keys=True));q.replace(state)
with (h/'spare-owner.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 try:
  for _ in range(240):
   p=b/'runtime/environment-install.json';v=json.loads(p.read_text()) if p.exists() else {}
   if v.get('status')=='ready':break
   if v.get('status')=='failed':raise RuntimeError('Isolated environment installation failed')
   write({'status':'waiting_environment','environment_status':v.get('status')});time.sleep(30)
  else:raise RuntimeError('Finite environment preparation timeout')
  py=str(b/'.venv/bin/python');asset=h/'import'
  env={**os.environ,'EVIDENCE_V8_NATIVE_HOME':str(h),'EVIDENCE_V8_NATIVE_GPU_UUID':gpu,
       'HF_HOME':str(h/'cache/huggingface'),'OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1',
       'OPENBLAS_NUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1','TOKENIZERS_PARALLELISM':'false'}
  try:os.nice(10)
  except PermissionError:pass
  write({'status':'preparing_audited_training_inputs'})
  subprocess.run([py,'-u','-B',str(s/'scripts/evidence_v8_native.py'),'prepare','--home',str(h),
                  '--source',str(asset/'native-train.json'),'--protection',str(asset/'protection.json')],env=env,check=True)
  write({'status':'starting_native_pipeline','stages':['audit','smoke','encode','train-drugprot','train-cg','freeze']})
  r=subprocess.run([py,'-u','-B',str(asset/'runner.py'),'--source',str(s),'--home',str(h),'--assets',str(asset),'--native-only'],env=env)
  pipeline=json.loads((h/'pipeline-state.json').read_text()) if (h/'pipeline-state.json').exists() else {}
  write({'status':pipeline.get('status','failed'),'returncode':r.returncode,'scientific_goal_achieved':False})
 except Exception as exc:
  write({'status':'failed','error':type(exc).__name__+': '+str(exc)});raise
"""
path=h/'spare-launcher.py';path.write_text(launcher)
log=(h/'spare-launcher.log').open('ab')
p=subprocess.Popen(['python3','-u','-B',str(path),str(b),str(h),str(s),GPU],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
result={'host':HOST,'experiment':EXPERIMENT,'pid':p.pid,'gpu_uuid':GPU,'source_directory':str(s),
        'source_manifest_sha256':source_hash,'runner_sha256':RUNNER_SHA,'launcher_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'asset_manifest_sha256':hashlib.sha256((asset/'asset-manifest.json').read_bytes()).hexdigest(),
        'started_at':time.time(),'additional_model_calls':0,'sealed_labels_exported':False,'r2_job_modified':False,
        'main_development_evaluation':'separate existing run4; native labels are not BioRED labels'}
(h/'spare-launch.json').write_text(json.dumps(result,sort_keys=True));print(json.dumps(result))
'''
    prefix = '\n'.join(name + '=' + repr(value) for name, value in {
        'PROJECT': PROJECT, 'EXPERIMENT': EXPERIMENT, 'GPU': GPU, 'HOST': HOST,
        'SOURCE': source_receipt['source_directory'], 'RUNNER_SHA': runner_sha}.items())
    result = json.loads(remote(HOST, prefix + '\n' + code, blob, timeout=180))
    output = ROOT / 'literature_service/runtime/v8-deployment/launch-native-spare-laptop.json'
    with output.open('x', encoding='utf8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    start()
