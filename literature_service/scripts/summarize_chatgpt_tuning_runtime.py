"""Measured request durations/tokens, without inferring price or account quota."""
from pathlib import Path
import json
import math
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from nutri_literature import chatgpt_tuning as base,chatgpt_joint_verifier as joint
    from nutri_literature.contract_v7 import immutable,sha
    groups=[]
    for model,effort in base.MODELS:
        for split,total in [('calibration',40),('development',100)]:
            groups.append((model+' '+effort+' · '+split,base.directory(model)/split/'responses',total,'new'))
    for phase,total in [('original-calibration',40),('review-calibration',40),('review-development',100)]:
        groups.append(('Sol medium · '+phase,joint.HOME/phase/'responses',total,'new'))
    for folder in ('gpt-6.1-sol-medium-0fd4d4e0','gpt-6-luna-max-ad90ee79'):
        groups.append((folder,base.PRIOR/folder/'responses',100,'reused'))
    result=[]
    ids={'new':set(),'reused':set()}
    for name,path,total,scope in groups:
        files=sorted(path.glob('*.json'))
        rows=[json.loads(p.read_bytes()) for p in files]
        if len(rows)!=total or any(r.get('test_only') or r.get('status')!='completed' for r in rows):
            raise ValueError('Incomplete real request runtime partition')
        ids[scope].update(r['response_id'] for r in rows)
        elapsed=sorted(r['elapsed_seconds'] for r in rows)
        result.append({'group':name,'scope':scope,'completed_requests':total,
                       'mean_seconds_per_request':statistics.mean(elapsed),'median_seconds_per_request':statistics.median(elapsed),
                       'p95_seconds_per_request':elapsed[math.ceil(.95*total)-1],
                       'input_tokens':sum(r.get('usage',{}).get('input_tokens',0) for r in rows),
                       'output_tokens_including_reasoning':sum(r.get('usage',{}).get('output_tokens',0) for r in rows),
                       'reasoning_tokens':sum(r.get('usage',{}).get('output_tokens_details',{}).get('reasoning_tokens',0) for r in rows),
                       'actual_models':sorted({r['actual_model'] for r in rows}),
                       'response_files_sha256':{p.name:sha(p.read_bytes()) for p in files}})
    if len(ids['new'])!=460 or len(ids['reused'])!=200 or ids['new']&ids['reused']:
        raise ValueError('New and reused response identity counts differ')
    selected=json.loads((base.HOME/'overall-selection.json').read_bytes())['selected_family']
    report={'new_completed_requests':460,'reused_completed_requests':200,'training_selected_family':selected,
            'production_model_requests_per_paper_for_selected_family':4 if selected=='joint' else 1,
            'request_duration_is_not_pipeline_end_to_end_latency':True,'pipeline_end_to_end_latency':'unmeasured',
            'quota_remaining':'not_inferred','monetary_cost':'not_inferred',
            'usage_scope':'only persisted successful responses; excludes pilots, failures, and total account usage',
            'groups':result,'source_sha256':sha(Path(__file__).read_bytes())}
    immutable(base.HOME/'request-performance.json',report)
    print(json.dumps({'new_requests':460,'reused_requests':200,'selected_family':selected,
                      'groups':[{k:v for k,v in r.items() if k!='response_files_sha256'} for r in result]},ensure_ascii=False))


if __name__=='__main__':
    main()
