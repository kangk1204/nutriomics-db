"""Compact factual progress, real process liveness and frozen-source audit."""
from pathlib import Path
import hashlib,json,sys,time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def status():
    import psutil
    from nutri_literature.extraction_v6 import source_manifest
    home=ROOT/'runtime/v7'
    result={'at_unix':time.time(),'server':'100.84.183.6','default_engine':'heuristic-v1',
            'scientific_targets_met':False,'nutrition_accuracy':'unmeasured','states':{}}
    for name in ('baseline','identity','normalization-r2','normalization-r3','joint'):
        path=home/(name+'-state.json')
        if not path.exists():continue
        value=json.loads(path.read_bytes());pid=value.get('pid')
        value['owner_alive']=bool(pid and psutil.pid_exists(pid))
        if value['owner_alive']:
            try:value['owner_alive']=str(ROOT) in ' '.join(psutil.Process(pid).cmdline())
            except psutil.NoSuchProcess:value['owner_alive']=False
        result['states'][name]=value
    for name in ('separated-head','identity-head','joint-head'):
        path=home/name/'manifest.json'
        if path.exists():
            value=json.loads(path.read_bytes())
            result[name]={k:value.get(k) for k in ('selection','outer_metrics','automatic_enabled','training_pmids')}
            result[name]['training_papers']=len(result[name].pop('training_pmids') or [])
            if name=='identity-head':
                from nutri_literature.adoption_v7 import identity_adoption
                result[name]['adoption']=identity_adoption(value)
                result[name]['automatic_enabled']=result[name]['adoption']['passed']
    for name in ('separated-development','joint-development'):
        for mode in ('end_to_end','gold_entities'):
            path=home/name/mode/'report.json'
            if path.exists():
                value=json.loads(path.read_bytes());result[name+'-'+mode]={k:value[k] for k in ('papers','metrics','input_sha256','gold_sha256')}
    hashes,rules=source_manifest()
    result['legacy_v6_rules']=rules
    result['legacy_v6_rules_unchanged']=rules=='rules-sha256:e6251da4f59c9d912b5fa6659136015134dcde9302a60ab0897a8abce4948317'
    result['protected_final_attempts']=len(list((home/'final-attempts').glob('*.json')))
    result['owned_jobs']=[]
    for proc in psutil.process_iter(['pid','cmdline','memory_info','cwd']):
        try:
            args=proc.info['cmdline'] or []
            if proc.pid!=__import__('os').getpid() and proc.info['cwd']==str(ROOT) and any('v7' in a for a in args):
                result['owned_jobs'].append({'pid':proc.pid,'command':args,'rss':proc.info['memory_info'].rss})
        except (psutil.NoSuchProcess,psutil.AccessDenied):pass
    return result


if __name__=='__main__':
    value=status()
    path=ROOT/'runtime/v7/validation/progress-latest.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,sort_keys=True))
    print(json.dumps(value,ensure_ascii=False))
