"""Read only the three owned pipelines; no inference, training, or gold loading."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import shlex
import subprocess

if __package__:
    from .deploy_evidence_v8 import remote
else:
    from deploy_evidence_v8 import remote


def current_extraction_counts(connection):
    rows = connection.execute("""SELECT DISTINCT e.paper_id,e.engine_version
        FROM extractions e JOIN papers p ON p.paper_id=e.paper_id
        JOIN sources s ON s.source_id=p.current_source_id
        WHERE e.active=1 AND e.source_id=p.current_source_id AND e.source_hash=s.text_hash""").fetchall()
    papers, by_engine = set(), {}
    for paper_id, engine in rows:
        papers.add(paper_id)
        by_engine[engine] = by_engine.get(engine, 0) + 1
    return {"extraction_success_papers": len(papers),
            "extraction_success_scope": "At least one engine has an active extraction matching the current source ID and text hash",
            "live_current_extraction_papers_by_engine": by_engine,
            "live_abstract_papers": connection.execute("SELECT count(*) FROM papers WHERE trim(abstract)<>''").fetchone()[0],
            "live_queue_counts": [{"stage": stage, "status": status, "queue_items": n}
                for stage, status, n in connection.execute("SELECT stage,status,count(*) FROM queue GROUP BY stage,status ORDER BY stage,status")]}


def bounded_extraction_counts(connection, seconds=12):
    """Cancel a slow read before its SSH observer can leave an orphaned query."""
    import sqlite3
    import time
    deadline = time.monotonic() + seconds
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        return current_extraction_counts(connection)
    except sqlite3.OperationalError as error:
        if 'interrupted' not in str(error).lower():
            raise
        return {'extraction_read_status':'deadline_exceeded', 'extraction_read_budget_seconds':seconds}
    finally:
        connection.set_progress_handler(None, 0)


def frozen_search_counts(connection, manifest_path):
    """Keep the frozen denominator visible while a collector rewrites live state."""
    import hashlib
    import json
    from pathlib import Path
    path = Path(manifest_path)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    manifest = json.loads(raw)
    ids = manifest['pmids']
    if (not isinstance(ids, list) or any(not isinstance(p, str) or not p.isascii() or not p.isdecimal() for p in ids)
            or len(ids) != len(set(ids)) or manifest.get('expected_papers') != len(ids)
            or manifest.get('status') != 'search_complete'):
        raise ValueError('Frozen search manifest count or identifiers changed')
    digest = hashlib.sha256(json.dumps(ids, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if digest != manifest.get('pmids_sha256'):
        raise ValueError('Frozen search PMID hash changed')
    selected = set(ids)
    stored = len({str(r[0]) for r in connection.execute('SELECT pmid FROM papers') if str(r[0]) in selected})
    return {'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'pmids_sha256': digest,
            'query_version': manifest['query_version'], 'expected_papers': len(ids),
            'selected_unique': len(ids), 'stored_selected': stored, 'missing_count': len(ids)-stored,
            'search_complete': True, 'source': 'verified immutable manifest plus current read-only DB transaction'}


def training_artifact_progress(home):
    """Read training milestones without inferring accuracy or loading model weights."""
    import json
    from pathlib import Path
    home = Path(home)
    result = {}
    excluded = set()
    for task in ('id', 'relation'):
        result['completed_' + task + '_linear_fits'] = len(list((home/'fits'/task).glob('*/model.json')))
        completed = 0
        for path in (home/'fits'/task).glob('*/complete.json'):
            value = json.loads(path.read_text(encoding='utf8'))
            config = value.get('config')
            if isinstance(config, dict) and config.get('smoke_only') is True:
                excluded.add(path.parent)
            elif value.get('task') == task and isinstance(config, dict):
                completed += 1
        result['completed_' + task + '_neural_fits'] = completed
    result['smoke_neural_fits_excluded'] = len(excluded)
    checkpoints = [p for directory in (home/'ner/models', home/'fits/id', home/'fits/relation')
                   for p in directory.glob('*/progress.json') if p.parent not in excluded]
    if checkpoints:
        latest = max(checkpoints, key=lambda p: (p.stat().st_mtime, str(p)))
        history = json.loads(latest.read_text(encoding='utf8')).get('history', [])
        if history:
            last = history[-1]
            epoch = last.get('epoch')
            if type(epoch) is not int or epoch < 1 or type(last.get('optimizer_updated')) is not bool:
                raise ValueError('Training checkpoint metadata cannot be verified')
            task = 'ner' if latest.is_relative_to(home/'ner') else latest.relative_to(home).parts[1]
            result['latest_training_checkpoint'] = {'task':task, 'epoch':epoch,
                'optimizer_updated':last['optimizer_updated'], 'modified_at_unix':latest.stat().st_mtime,
                'scope':'Training progress only; not relation accuracy or fold completion'}
    return result


def gpu_progress():
    code = """import json,psutil,subprocess,shutil,hashlib
from pathlib import Path
b=Path('/home/keunsoo/projects/nutriomics-evidence-v8');out={}
pointer=b/'runtime/active-v8-experiment.json';identity=json.loads(pointer.read_text()) if pointer.exists() else {'experiment':'nutriomics-evidence-v8-run2'};active=identity['experiment']
import re
if not re.fullmatch(r'nutriomics-evidence-v8(?:-[a-z0-9]+)*',active):raise ValueError('Owned experiment pointer')
previous=identity.get('previous_experiments',[])
if not isinstance(previous,list) or any(not isinstance(n,str) or not re.fullmatch(r'nutriomics-evidence-v8(?:-[a-z0-9]+)*',n) for n in previous):raise ValueError('Owned predecessor experiment')
for name in dict.fromkeys([active,'nutriomics-evidence-v8-clinical',*previous]):
 h=b/'runtime'/name;p=h/'pipeline-state.json';v=json.loads(p.read_text()) if p.exists() else {'status':'not_started'}
 if v.get('pid'):v['supervisor_alive']=psutil.pid_exists(v['pid'])
 if v.get('child_pid'):v['child_alive']=psutil.pid_exists(v['child_pid'])
 v['prediction_manifest_exists']=(h/'predictions/manifest.json').exists()
 v['completed_ner_profiles']=len(list((h/'ner/profiles').glob('*.json')))
 v['completed_ner_models']=len(list((h/'ner/models').glob('*/complete.json')))
 v['completed_id_outer_folds']=len(list((h/'cross-validation/id').glob('outer-*.json')))
 v['completed_relation_outer_folds']=len(list((h/'cross-validation/relation').glob('outer-*.json')))
 v.update(training_artifact_progress(h))
 v['ner_training_manifest_exists']=(h/'ner/manifest.json').exists()
 v['selected_id_manifest_exists']=(h/'selected/id.json').exists()
 v['selected_relation_manifest_exists']=(h/'selected/relation.json').exists()
 child=h/'state.json'
 if child.is_file():v['child_stage_state']=json.loads(child.read_text())
 ner=h/'ner/manifest.json'
 if ner.is_file():v['public_training_ner_diagnostics']=json.loads(ner.read_text())
 launch=h/'launch.json'
 if launch.is_file():
  blob=launch.read_bytes();configuration=json.loads(blob)
  v['execution_launch_sha256']=hashlib.sha256(blob).hexdigest()
  if 'memory_probe_peak_rss_bytes' in configuration:v['io_memory_probe_peak_rss_bytes']=configuration['memory_probe_peak_rss_bytes']
  v['trained_weights_or_predictions_reused']=configuration.get('trained_weights_or_predictions_reused')
 v['smoke_results']={}
 for p in h.glob('smoke/*/complete.json'):v['smoke_results'][p.parent.name]=json.loads(p.read_text())
 for p in [h/'clinical/manifest.json',h/'clinical-role/manifest.json']:
  if p.exists():v[p.parent.name]=json.loads(p.read_text())
 out[name]=v
out['resources']={'available_memory_bytes':psutil.virtual_memory().available,'free_disk_bytes':shutil.disk_usage(b).free,'gpu':subprocess.check_output(['nvidia-smi','--query-gpu=uuid,name,memory.used,memory.total','--format=csv,noheader,nounits'],text=True).strip()}
print(json.dumps(out))
"""
    return json.loads(remote("100.84.183.6", inspect.getsource(training_artifact_progress) + '\n' + code, timeout=45))


def r2_progress():
    code = """import json,psutil,hashlib
from pathlib import Path
h=Path('/home/keunsoo/projects/nutriomics-relation-v6-gpu-r2/runtime/relation-repair-v6-gpu-r2')
p=h/'job-state.json';v=json.loads(p.read_text()) if p.exists() else {}
v['predictions_ready']=(h/'predictions/manifest.json').exists()
if v.get('pid'):v['process_alive']=psutil.pid_exists(v['pid'])
v['experiment_modified']=False
v['completed_relation_outer_folds']=len(list((h/'calibration').glob('outer-*.json')))
p=h/'identity/selection.json'
if p.is_file():
 raw=p.read_bytes();identity=json.loads(raw)
 v['id_training']={k:identity.get(k) for k in ['policy','automatic_outer_summary','automatic_outer_decisions','selection_uses_development','training_and_inference_menu_limit','gold_ID_injection']}
 v['id_training']['selection_sha256']=hashlib.sha256(raw).hexdigest()
 v['id_training']['scope']='Public training cross-validation only; not development or production ID changes'
b=Path('/home/keunsoo/projects/nutriomics-evidence-v8');n=b/'runtime/nutriomics-native-v8-r1'
for key,path in [('native_queue',n/'queue-state.json'),('native_pipeline',n/'pipeline-state.json'),('v8_environment',b/'runtime/environment-install.json')]:
 if path.exists():v[key]=json.loads(path.read_text())
print(json.dumps(v))
"""
    return json.loads(remote("100.93.148.94", code, timeout=45))


def collection_progress():
    code = """import json,sqlite3
from pathlib import Path
h=Path('/home/keunsoo/projects/nutriomics-literature-platform/runtime');v=json.loads((h/'v8-expansion/state.json').read_text())
with sqlite3.connect((h/'data/literature.sqlite3').as_uri()+'?mode=ro',uri=True) as c:
 c.execute('BEGIN')
 v['live_stored_unique_papers']=c.execute('SELECT count(*) FROM papers').fetchone()[0]
 v['live_oa_available_papers']=c.execute(\"SELECT count(*) FROM papers WHERE oa_status='available'\").fetchone()[0]
 v['live_abstract_papers']=c.execute(\"SELECT count(*) FROM papers WHERE trim(abstract)<>''\").fetchone()[0]
 v['oa_state_counts']=dict(c.execute('SELECT oa_status,count(*) FROM papers GROUP BY oa_status'))
 manifest=Path(v['manifest']).resolve()
 if not manifest.is_relative_to((h/'v8-expansion').resolve()):raise ValueError('Owned collection manifest path')
 v['frozen_search_inventory']=frozen_search_counts(c,manifest)
 v.update(bounded_extraction_counts(c))
ledger=manifest.parent/'ingest.sqlite3'
if ledger.is_file():
 with sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True) as c:
  v['live_ingest_metadata_status']=dict(c.execute('SELECT status,count(*) FROM metadata GROUP BY status'))
for scope in ['public','internal']:
 p=h/'platform-snapshots'/(scope+'-current')/'manifest.json'
 if p.exists():v[scope+'_snapshot']=json.loads(p.read_text()).get('counts')
print(json.dumps(v))
"""
    code = (inspect.getsource(current_extraction_counts) + "\n" + inspect.getsource(bounded_extraction_counts)
            + "\n" + inspect.getsource(frozen_search_counts) + "\n" + code)
    known = "C:/Projects/00_NutriGenomics_auto/outputs/publication/2026-10-22/tailscale-known-hosts"
    command = ["ssh", "-J", "keunsoo@100.93.148.94", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=12", "-o", "UserKnownHostsFile=" + known, "keunsoo@100.110.101.17",
               "python3 -c " + shlex.quote(code)]
    p = subprocess.run(command, capture_output=True, timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr.decode(errors="replace")[-400:])
    return json.loads(p.stdout)


def spare_gpu_progress():
    code = """import json,os,subprocess,shutil
from pathlib import Path
b=Path('/home/keunsoo/projects/nutriomics-evidence-v8');h=b/'runtime/nutriomics-native-v8-laptop-r1';out={}
def alive(pid):
 try:os.kill(pid,0);return True
 except ProcessLookupError:return False
for key,path in [('launcher',h/'spare-state.json'),('pipeline',h/'pipeline-state.json'),('environment',b/'runtime/environment-install.json')]:
 if path.exists():
  v=json.loads(path.read_text())
  if v.get('pid'):v['process_alive']=alive(v['pid'])
  if v.get('child_pid'):v['child_alive']=alive(v['child_pid'])
  out[key]=v
out['host']='100.68.188.102';out['experiment']=h.name
out['selected_models']={task:(h/'selected'/(task+'.json')).is_file() for task in ['drugprot','cg']}
out['completed_neural_model_files']=len(list((h/'models').glob('*/*/complete.json')))
out['completed_outer_folds']={task:len(list((h/'cross-validation'/task).glob('outer-*.json'))) for task in ['drugprot','cg']}
out['gpu']=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,name,memory.used,memory.total,utilization.gpu','--format=csv,noheader,nounits'],text=True).strip()
memory={k:int(v.split()[0])*1024 for k,v in (line.split(':',1) for line in open('/proc/meminfo')) if k=='MemAvailable'}
out['available_memory_bytes']=memory['MemAvailable'];out['free_disk_bytes']=shutil.disk_usage(b).free
out['main_100_paper_evaluation']='separate existing run4; native labels are not BioRED labels'
print(json.dumps(out))
"""
    return json.loads(remote('100.68.188.102', code, timeout=45))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runtime = Path(__file__).resolve().parents[1] / "literature_service/runtime"
    measured = sorted(p.parent.name for p in runtime.glob("nutriomics-evidence-v8*/evaluation.json") if p.is_file())
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "additional_model_calls": 0,
              "default_engine": "heuristic-v1", "sealed_test_opened": False,
              "nutrition_accuracy": "unmeasured", "new_v8_performance_measured": bool(measured),
              "measured_v8_experiments": measured}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {name: pool.submit(fn) for name, fn in
                (("collection", collection_progress), ("r2", r2_progress), ("v8", gpu_progress),
                 ("spare_gpu", spare_gpu_progress))}
        for name, job in jobs.items():
            try:
                result[name] = job.result()
            except Exception as error:
                result[name] = {"status": "unavailable", "error": type(error).__name__ + ": " + str(error)[:500]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
