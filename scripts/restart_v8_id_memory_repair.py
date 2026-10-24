"""Start a new pinned experiment after the reproducible run4 ID memory failure."""
import hashlib
import json
from pathlib import Path

from deploy_evidence_v8 import ROOT, remote


def main():
    source=json.loads((ROOT/'literature_service/runtime/v8-deployment/source-run5-id-memory.json').read_text())
    runner=(ROOT/'scripts/run_evidence_v8_pipeline.py').read_bytes()
    digest=hashlib.sha256(runner).hexdigest()
    code=r'''import hashlib,json,os,psutil,subprocess,time
from pathlib import Path
b=Path('/home/keunsoo/projects/nutriomics-evidence-v8');old=b/'runtime/nutriomics-evidence-v8-run4';h=b/'runtime/nutriomics-evidence-v8-run5';s=Path(SOURCE)
state=json.loads((old/'pipeline-state.json').read_text())
if state['status']!='failed' or state['stage']!='train-id':raise ValueError('Not the diagnosed ID memory failure')
if any(state.get(k) and psutil.pid_exists(state[k]) for k in ['pid','child_pid']):raise ValueError('Old job still live')
manifest=s/'source-manifest.json';new=json.loads(manifest.read_text());old_manifest=json.loads((Path(state['source_directory'])/'source-manifest.json').read_text())
changed=[n for n in sorted(set(new)|set(old_manifest)) if new.get(n)!=old_manifest.get(n)]
if changed!=['nutri_literature/evidence_v8/common.py','nutri_literature/evidence_v8/runner.py']:raise ValueError('Unexpected scientific change')
for name,sha in new.items():
 p=(s/name).resolve()
 if not p.is_relative_to(s.resolve()) or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:raise ValueError('Source integrity')
verification=b/'runtime/verification/id-memory-20261024'
test=json.loads((verification/'tests-result.json').read_text());probe=json.loads((verification/'actual-profile-probe/receipt.json').read_text())
if test['exit_code'] or not probe['canonical_values_and_hashes_identical'] or probe['peak_rss_bytes']>=8*1024**3:raise ValueError('Actual memory/test gate incomplete')
if h.exists():raise ValueError('New experiment already exists; inspect before resuming')
contract=json.loads((old/'pipeline-contract.json').read_text());assets=Path(contract['assets'])
if hashlib.sha256((assets/'asset-manifest.json').read_bytes()).hexdigest()!=contract['asset_manifest_sha256']:raise ValueError('Original assets changed')
h.mkdir();(h/'cache').mkdir();(h/'cache/huggingface').symlink_to((old/'cache/huggingface').resolve(),target_is_directory=True)
policy=json.loads((old/'resource-policy.json').read_text()) if (old/'resource-policy.json').exists() else {'forbidden_uuids':[],'desktop_allowlist':[{'executable':'/usr/bin/ptyxis','maximum_mib':128}]}
(h/'resource-policy.json').write_text(json.dumps(policy,sort_keys=True))
runners=[p for p in (b/'runtime/runners').glob('*.py') if hashlib.sha256(p.read_bytes()).hexdigest()==RUNNER_SHA]
if not runners:raise ValueError('Exact supervisor not found')
ledger=old/'gpu-ledger.json';ledger_value=json.loads(ledger.read_text()) if ledger.exists() else {}
with (h/'supervisor.log').open('ab') as log:
 p=subprocess.Popen([str(b/'.venv/bin/python'),'-u','-B',str(runners[0]),'--source',str(s),'--home',str(h),'--assets',str(assets)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
result={'experiment':h.name,'pid':p.pid,'source_directory':str(s),'source_sha256':SOURCE_SHA,
        'source_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest(),'runner_sha256':RUNNER_SHA,
        'started_at':time.time(),'previous_experiments':['nutriomics-evidence-v8-run4','nutriomics-evidence-v8-run3'],
        'source_changes':changed,'previous_experiments_preserved':True,'trained_weights_or_predictions_reused':False,
        'public_pretrained_revision_cache_reused':True,'predecessor_gpu_ledger_sha256':hashlib.sha256(ledger.read_bytes()).hexdigest() if ledger.exists() else None,
        'predecessor_gpu_used_seconds':ledger_value.get('used_seconds'),'memory_probe_peak_rss_bytes':probe['peak_rss_bytes'],
        'memory_probe_receipt_sha256':hashlib.sha256((verification/'actual-profile-probe/receipt.json').read_bytes()).hexdigest(),
        'default_engine':'heuristic-v1','additional_model_calls':0}
(h/'launch.json').write_text(json.dumps(result,sort_keys=True))
pointer=b/'runtime/active-v8-experiment.json'
if pointer.exists():(b/'runtime'/('active-v8-previous-'+str(time.time_ns())+'.json')).write_bytes(pointer.read_bytes())
next_pointer=pointer.with_suffix('.part');next_pointer.write_text(json.dumps({'experiment':h.name,'previous_experiments':result['previous_experiments']},sort_keys=True));next_pointer.replace(pointer)
print(json.dumps(result))
'''
    prefix='SOURCE='+repr(source['source_directory'])+'\nSOURCE_SHA='+repr(source['source_sha256'])+'\nRUNNER_SHA='+repr(digest)+'\n'
    result=json.loads(remote('100.84.183.6',prefix+code,timeout=90))
    path=ROOT/'literature_service/runtime/v8-deployment/launch-run5.json'
    with path.open('x',encoding='utf8') as stream:json.dump(result,stream,indent=2)
    print(json.dumps(result))


if __name__=='__main__':main()
