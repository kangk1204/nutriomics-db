"""Same-cohort differences, all TP/FP/FN retained, paper bootstrap."""
import json,random
from pathlib import Path
from .contract_v7 import POLICY,immutable


def metric(rows):
    values={k:sum(r[k] for r in rows) for k in ('tp','fp','fn')}
    p=values['tp']/max(1,values['tp']+values['fp']);r=values['tp']/max(1,values['tp']+values['fn'])
    return {**values,'precision':p,'recall':r,'f1':2*p*r/(p+r) if p+r else 0.}


def compare(baseline,current,output):
    baseline,current=Path(baseline),Path(current)
    old,new=json.loads(baseline.read_bytes()),json.loads(current.read_bytes())
    for key in ('input_sha256','gold_sha256'):
        if old[key]!=new[key]:raise ValueError('Same-criterion comparison mismatch: '+key)
    previous=old['per_paper']['recall_ensemble'] if isinstance(old['per_paper'],dict) else old['per_paper']
    before={r['pmid']:r['relation'] for r in previous}
    after={r['pmid']:r['relation'] for r in new['per_paper']}
    if set(before)!=set(after):raise ValueError('Comparison PMID set differs')
    ids=sorted(before);rng=random.Random(POLICY['seed']);samples={k:[] for k in ('precision','recall','f1')}
    for _ in range(2000):
        chosen=rng.choices(ids,k=len(ids));a=metric([before[i] for i in chosen]);b=metric([after[i] for i in chosen])
        for key in samples:samples[key].append(b[key]-a[key])
    intervals={k:[sorted(v)[49],sorted(v)[1949]] for k,v in samples.items()}
    a,b=metric(list(before.values())),metric(list(after.values()))
    result={'engine':'heuristic-v7','baseline':'v6 existing-ID fixed union','papers':len(ids),
            'before':a,'after':b,'delta':{k:b[k]-a[k] for k in ('tp','fp','fn','precision','recall','f1')},
            'paired_bootstrap_2000_ci95':intervals,'input_sha256':new['input_sha256'],'gold_sha256':new['gold_sha256'],
            'evaluation_scope':'repeatedly exposed development data; not independent final performance',
            'nutrition_accuracy':'unmeasured','default_engine':'heuristic-v1'}
    immutable(output,result)
    return result
