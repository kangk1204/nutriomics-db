"""Measure saved-result API reads under five concurrent clients; no NLP calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import time

import requests

from nutri_literature.db import dumps, now
from nutri_literature.settings import Settings

paths=['/api/v1/papers?limit=20&engine=heuristic-v2','/api/v1/events?limit=20&engine=heuristic-v2',
       '/api/v1/evidence-map?limit=50&engine=heuristic-v2','/api/v1/runs?limit=5&engine=heuristic-v2']


def request(path):
    start=time.perf_counter()
    try:
        response=requests.get('http://127.0.0.1:8010'+path,timeout=30)
        return {'path':path,'seconds':time.perf_counter()-start,'http_status':response.status_code,'bytes':len(response.content)}
    except requests.RequestException as error:
        return {'path':path,'seconds':time.perf_counter()-start,'http_status':None,'error':type(error).__name__}


if __name__=='__main__':
    with ThreadPoolExecutor(max_workers=5) as pool:
        samples=list(pool.map(request,paths*10))
    values=sorted(s['seconds'] for s in samples)
    p95=values[int(.95*(len(values)-1))]
    record={'measured_at':now(),'clients':5,'requests':len(samples),'p95_seconds':p95,'target_seconds':2,
            'passed':p95<=2 and all(s['http_status']==200 for s in samples),'samples':samples}
    destination=Settings.from_env().home/'validation'/'api-v2-latency.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(dumps(record),encoding='utf-8')
    print(json.dumps({k:v for k,v in record.items() if k!='samples'}))
