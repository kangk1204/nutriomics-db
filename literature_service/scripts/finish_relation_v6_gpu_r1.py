"""Finite local evaluation worker: protected gold stays local, no authentication is exported."""
import os,sys,time,json,subprocess,zipfile,hashlib,traceback
from pathlib import Path,PurePosixPath
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v6_gpu_r1.common import ROOT,HOME,atomic,read,lock,filehash

HOST='keunsoo@100.93.148.94'
REMOTE='/home/keunsoo/projects/nutriomics-relation-v6-gpu-r1'
SSH=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','StrictHostKeyChecking=yes',HOST,'python3','-']

STATUS=f'''import json
from pathlib import Path
r=Path({REMOTE!r});h=r/'runtime/relation-repair-v6-gpu-r1'
v=json.loads((h/'job-state.json').read_text());v['predictions_ready']=(h/'predictions/manifest.json').exists()
print(json.dumps(v))
'''

ARCHIVE=f'''import sys,zipfile,json,subprocess
from pathlib import Path
r=Path({REMOTE!r});h=r/'runtime/relation-repair-v6-gpu-r1'
code="from nutri_literature.repair_v6_gpu_r1.runner import verify_freeze;verify_freeze()"
subprocess.run([str(r/'.venv/bin/python'),'-B','-c',code],cwd=r,stdout=subprocess.DEVNULL,check=True)
names=['freeze.json','bootstrap.json','policy.json','audit.json','transfer-contract.json','gpu-budget.json','gpu-probe.json','gpu-task-contracts.json','gpu-window-contracts.json','job-state.json',
 'calibration/selection.json','identity/selection.json','relation-data/development.json','knowledge/manifest.json','knowledge/dense-manifest.json',
 'knowledge/base-source/manifest.json','auxiliary/manifest.json','auxiliary/cross-source-audit.json','prepared/manifest.json','normalized/manifest.json']
files=[h/n for n in names]
for group in ['data/*.json','prepared/development/*.json','normalized/development/*.json','predictions/**/*.json','diagnostics/*.json','identity/policies/*.json','identity/outer-*.json','identity/linear-rankers/*/*.json','identity/auto-profiles/*/manifest.json']:
 files.extend(h.glob(group))
with zipfile.ZipFile(sys.stdout.buffer,'w',zipfile.ZIP_DEFLATED,allowZip64=True) as z:
 for p in sorted(set(files)):
  if p.is_file():z.write(p,str(p.relative_to(h)))
'''

def download():
 temp=HOME/'server-results.zip.part';target=HOME/'server-results.zip'
 with temp.open('wb') as out:
  p=subprocess.run(SSH,input=ARCHIVE.encode(),stdout=out,stderr=subprocess.PIPE,timeout=1800)
 if p.returncode:raise RuntimeError('서버 산출물 검증/회수 실패; SSH 종료 '+str(p.returncode))
 os.replace(temp,target)
 with zipfile.ZipFile(target) as z:
  for member in z.infolist():
   name=PurePosixPath(member.filename)
   if name.is_absolute() or '..' in name.parts:raise ValueError('압축 경로 이탈')
   dest=HOME.joinpath(*name.parts).resolve()
   if not dest.is_relative_to(HOME.resolve()):raise ValueError('작업 경로 이탈')
   value=z.read(member)
   if dest.exists():
    if dest.read_bytes()!=value:raise ValueError('기존 실험 산출물과 서버 사본 불일치: '+member.filename)
    continue
   dest.parent.mkdir(parents=True,exist_ok=True);tmp=dest.with_name(dest.name+'.transfer-part')
   tmp.write_bytes(value);os.replace(tmp,dest)
 freeze=read(HOME/'freeze.json')
 for name,digest in freeze['source_sha256'].items():
  if filehash(ROOT.joinpath(*PurePosixPath(name.replace('\\','/')).parts))!=digest:raise ValueError('서버/로컬 과학 소스 불일치: '+name)
 return {'archive_sha256':filehash(target),'freeze_sha256':filehash(HOME/'freeze.json')}

def main():
 directory=HOME/'local-finisher';directory.mkdir(parents=True,exist_ok=True)
 with lock(directory):
  p=directory/'state.json';old=read(p) if p.exists() else {};started=old.get('started',time.time())
  state={'pid':os.getpid(),'started':started,'running':True,'host':HOST,'phase':'waiting-for-server-predictions','max_wall_hours':80}
  atomic(p,state);errors=0
  try:
   while time.time()-started<80*3600:
    try:
     result=subprocess.run(SSH,input=STATUS.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=40)
     if result.returncode:raise ConnectionError('서버 상태 SSH 종료 '+str(result.returncode))
     remote=json.loads(result.stdout);state['remote']=remote;state['last_check']=time.time();errors=0;state['consecutive_connection_failures']=0;state.pop('last_connection_error',None);atomic(p,state)
    except (OSError,subprocess.TimeoutExpired,ConnectionError,json.JSONDecodeError) as exc:
     errors+=1;state.update(last_connection_error=type(exc).__name__,consecutive_connection_failures=errors);atomic(p,state);time.sleep(min(300,30*errors));continue
    if remote.get('predictions_ready'):
     state['phase']='recovering-frozen-results';atomic(p,state);state['transfer']=download()
     state['phase']='unchanged-local-evaluation';atomic(p,state)
     from nutri_literature.repair_v6_gpu_r1.evaluation import evaluate_local
     first=evaluate_local();second=evaluate_local()
     if first!=second:raise ValueError('저장 예측의 오프라인 재채점 불일치')
     atomic(HOME/'offline-reproduction.json',{'identical':True,'metrics':first,'prediction_manifest_sha256':filehash(HOME/'predictions/manifest.json'),'sealed_labels_opened':False},True)
     state.update(phase='development-evaluation-complete',exit='completed',metrics=first);break
    if not remote.get('running') and remote.get('exit')=='failed':raise RuntimeError('서버 파이프라인 실패: '+remote.get('error_class','unknown'))
    time.sleep(60)
   else:raise TimeoutError('유한 파이프라인 80시간 경과; 미완료 기록 보존')
  except Exception as exc:
   state.update(exit='failed',error_class=type(exc).__name__,safe_message=str(exc)[:250])
  finally:state.update(running=False,finished=time.time());atomic(p,state)

if __name__=='__main__':main()
