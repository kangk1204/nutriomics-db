"""Resume only a pinned, interrupted v8 pipeline after its server returns."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_evidence_v8 import ALLOWED_HOSTS, PROJECT, remote


def interrupted(state):
    return state.get("status") in {"running", "waiting"} and not any(
        state.get(key) for key in ("supervisor_alive", "child_alive", "predictions_ready")
    )


def resume(host, experiment):
    if not re.fullmatch(r"nutriomics-evidence-v8(?:-[a-z0-9]+)*", experiment):
        raise ValueError("Only an owned v8 core experiment can be resumed")
    runner = Path(__file__).with_name("run_evidence_v8_pipeline.py")
    runner_sha = hashlib.sha256(runner.read_bytes()).hexdigest()
    code = r'''import fcntl,hashlib,json,os,psutil,subprocess,time
from pathlib import Path
b=Path(PROJECT);h=b/'runtime'/EXPERIMENT
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def result(status,**kw):print(json.dumps({'status':status,**kw}))
with (h/'resume-owner.lock').open('a') as owner:
 fcntl.flock(owner,fcntl.LOCK_EX)
 contract=read(h/'pipeline-contract.json');state=read(h/'pipeline-state.json')
 if state.get('status') not in {'running','waiting'} or (h/'cancel-request.json').exists() or (h/'predictions/manifest.json').exists():
  result('not_an_interruption');raise SystemExit(0)
 for process in psutil.process_iter(['cmdline']):
  try:args=process.info['cmdline'] or []
  except (psutil.NoSuchProcess,psutil.AccessDenied):continue
  if str(h) in args:
   result('already_running',pid=process.pid);raise SystemExit(0)
 for key in ('pid','child_pid'):
  if state.get(key) and psutil.pid_exists(state[key]):
   result('ambiguous_live_pid_requires_inspection',pid=state[key]);raise SystemExit(0)
 lock=(h/'pipeline-owner.lock').open('a')
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:
  result('already_owned');raise SystemExit(0)
 source=Path(contract['source_directory']).resolve();assets=Path(contract['assets']).resolve()
 if str(h)!=contract['home'] or not source.is_relative_to(b/'sources') or not assets.is_relative_to(b/'assets'):
  raise ValueError('Owned path contract')
 expected=['bootstrap','probe','audit','smoke-ner','smoke-id','prepare','train-ner','train-id','train-relations','freeze','predict']
 if contract['stages']!=expected or contract.get('additional_generative_model_calls')!=0 or contract.get('default_engine')!='heuristic-v1':
  raise ValueError('Core-only no-API contract')
 manifest=source/'source-manifest.json'
 if sha(manifest)!=contract['source_manifest_sha256']:raise ValueError('Source manifest changed')
 for name,digest in read(manifest).items():
  p=(source/name).resolve()
  if not p.is_relative_to(source) or sha(p)!=digest:raise ValueError('Pinned source changed')
 if sha(assets/'asset-manifest.json')!=contract['asset_manifest_sha256']:raise ValueError('Asset contract changed')
 runners=[p for p in (b/'runtime/runners').glob('*.py') if sha(p)==RUNNER_SHA]
 if not runners:raise ValueError('Exact pinned supervisor unavailable')
 fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
 path=h/('resume-'+str(time.time_ns())+'.log')
 with path.open('xb') as output:
  process=subprocess.Popen([str(b/'.venv/bin/python'),'-u','-B',str(sorted(runners)[0]),'--source',str(source),'--home',str(h),'--assets',str(assets)],
   cwd=b,stdout=output,stderr=subprocess.STDOUT,start_new_session=True,env={**os.environ,'PYTHONUNBUFFERED':'1'})
 receipt={'pid':process.pid,'started_at':time.time(),'source_manifest_sha256':contract['source_manifest_sha256'],'runner_sha256':RUNNER_SHA,'log':str(path),'other_experiments_modified':False}
 stamp=h/('resume-'+str(time.time_ns())+'.json');stamp.write_text(json.dumps(receipt,sort_keys=True))
 result('resumed',**receipt)
'''
    prefix = "PROJECT=" + repr(PROJECT) + "\nEXPERIMENT=" + repr(experiment) + "\nRUNNER_SHA=" + repr(runner_sha) + "\n"
    try:
        return json.loads(remote(host, prefix + code, timeout=60))
    except RuntimeError as error:
        if "ValueError:" in str(error):
            raise ValueError("Resume refused a changed pinned contract: " + str(error)) from error
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=ALLOWED_HOSTS, required=True)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    print(json.dumps(resume(args.host, args.experiment)))


if __name__ == "__main__":
    main()
