"""Four simultaneous readers; distinguish first HTTP reads from truly cold disk tests."""
import argparse,json,time,urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import quantiles


def main():
    p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:18125');p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    urls=['/api/v1/explorer/literature/papers?page_size=50','/api/v1/explorer/literature/papers?q=vitamin&page_size=50']
    def request(path):
        start=time.perf_counter()
        with urllib.request.urlopen(args.base+path,timeout=10) as r:
            value=json.load(r);assert r.status==200 and len(value['items'])<=50
        return time.perf_counter()-start
    first=[request(v) for v in urls]
    with ThreadPoolExecutor(max_workers=4) as pool:times=list(pool.map(request,urls*50))
    result={'concurrent_users':4,'requests':len(times),'warm_p95_seconds':sorted(times)[94],
            'first_HTTP_read_max_seconds':max(first),'OS_disk_cold_test':False,
            'warm_target_passed':sorted(times)[94]<=1.,'first_HTTP_read_target_passed':max(first)<=3.,
            'all_responses_valid':True,'errors':0}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2));print(json.dumps(result))


if __name__=='__main__':main()
