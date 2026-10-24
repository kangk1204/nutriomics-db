"""Deploy only the stored-experiment API/UI; retain frozen scientific engines."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import urlopen


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root=Path('/home/keunsoo/projects/nutriomics-literature').resolve()
    staged=Path('/home/keunsoo/projects/nutriomics-literature-v3-development').resolve()
    runtime=root/'runtime'
    backup=runtime/'deployments'/('v3-ui-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
    backup.mkdir(parents=True)
    from nutri_literature.extraction_v2 import _RULE_SOURCE_HASHES, RULES_VERSION
    if RULES_VERSION!='rules-sha256:11b4f00cf76a1b56b5cc273bd150474e021b4ba400f274b8b168f6f9486eae66':
        raise ValueError('Frozen v2 engine changed')
    for name,expected in _RULE_SOURCE_HASHES.items():
        if sha(root/name)!=expected:
            raise ValueError('Production scientific source changed: '+name)
    files=['nutri_literature/api.py','nutri_literature/experiments_v3.py','ui/app.py']
    old={}
    for name in files:
        target=(root/name).resolve()
        if not target.is_relative_to(root):
            raise ValueError('Deployment target outside production root')
        if target.exists():
            preserved=backup/name
            preserved.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(target,preserved)
            old[name]=sha(target)
        else:
            old[name]=None
    env={**os.environ,'LITERATURE_HOME':str(runtime)}
    command=[str(root/'.venv/bin/supervisorctl'),'-c',str(root/'scripts/supervisord.conf'),'restart','literature_api','literature_ui']
    try:
        for name in files:
            shutil.copy2(staged/name,root/name)
        subprocess.run(command,env=env,cwd=root,check=True,capture_output=True,text=True)
        responses={}
        for attempt in range(30):
            try:
                for key,url in {'api':'http://127.0.0.1:8010/healthz','ui':'http://127.0.0.1:8510/_stcore/health',
                                'experiments':'http://127.0.0.1:8010/api/v1/experiments',
                                'nutrition':'http://127.0.0.1:8010/api/v1/experiments/nutrition'}.items():
                    with urlopen(url,timeout=10) as response:
                        raw=response.read()
                        responses[key]=json.loads(raw) if key!='ui' else raw.decode()
                if responses['api']['engine']!='heuristic-v1' or len(responses['experiments']['reports'])!=5 or len(responses['nutrition']['items'])!=20:
                    raise ValueError('Default engine, experiment reports or preview count incorrect')
                break
            except OSError:
                if attempt==29:
                    raise
                time.sleep(1)
        for name,expected in _RULE_SOURCE_HASHES.items():
            if sha(root/name)!=expected:
                raise ValueError('Scientific source changed during UI deployment')
        record={'status':'deployed_development_comparison_and_nutrition_previews','at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                'backup':str(backup),'files':{name:{'before':old[name],'after':sha(root/name)} for name in files},
                'default_engine':'heuristic-v1','frozen_v2_rules_version':RULES_VERSION,
                'development_reports':5,'nutrition_previews':20,'api_health':responses['api'],'ui_health':responses['ui'],
                'workers_restarted':False,'promotion_eligible':False,'scientific_sources_unchanged':True}
        (runtime/'experiments/v3/deploy-ui-receipt.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
        print(json.dumps(record),flush=True)
    except Exception:
        for name in files:
            if old[name] is None:
                (root/name).unlink(missing_ok=True)
            else:
                shutil.copy2(backup/name,root/name)
        subprocess.run(command,env=env,cwd=root,check=False,capture_output=True)
        raise


if __name__=='__main__':
    main()
