"""Save actual test, lint and dependency outcomes from the current environment."""
import json
import os
import re
import subprocess
import sys
import time

from nutri_literature.db import digest, dumps, now
from nutri_literature.settings import PROJECT, Settings


if __name__=='__main__':
    settings=Settings.from_env()
    os.environ.setdefault('LITERATURE_NLP_LOCK',str(settings.home/'locks'/'nlp-cpu.lock'))
    destination=settings.home/'validation'
    destination.mkdir(parents=True,exist_ok=True)
    commands=[['-m','pytest','-q'],['-m','ruff','check','nutri_literature','tests','scripts','ui'],
              ['-m','pip','check']]
    checks=[]
    for args in commands:
        start=time.perf_counter()
        completed=subprocess.run([sys.executable,*args],cwd=PROJECT,capture_output=True,text=True)
        output=completed.stdout+completed.stderr
        log=destination/('verify-'+args[1]+'.log')
        log.write_text(output,encoding='utf-8')
        record={'command':[sys.executable,*args],'exit_code':completed.returncode,
                'seconds':round(time.perf_counter()-start,3),'log_path':str(log),'log_sha256':digest(output)}
        if args[1]=='pytest':
            for kind in ['passed','skipped','failed']:
                found=re.search(r'(\d+) '+kind,output)
                record[kind]=int(found.group(1)) if found else 0
        checks.append(record)
        print(dumps(record),flush=True)
    result={'checked_at':now(),'python':sys.version,'checks':checks,'passed':all(c['exit_code']==0 for c in checks),
            'meaning':'Software and dependency checks; not scientific accuracy or seven-day uptime'}
    path=destination/'software-v2.json'
    path.write_text(dumps(result),encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'output_path':str(path)}))
    raise SystemExit(0 if result['passed'] else 1)
