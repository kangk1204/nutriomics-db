"""Re-score stored development predictions and retain every first-stage loss."""
import json
from pathlib import Path
from collections import Counter
from .contract_v7 import immutable,sha,canonical


def diagnose(report_path, output):
    from . import evaluation_v6,benchmarks
    from .mapping_v7 import native_mapping
    report_path=Path(report_path);report=json.loads(report_path.read_bytes())
    raw=Path(report['prepared_gold_path']).read_bytes()
    if sha(raw)!=report['gold_sha256']:raise ValueError('Development gold checksum differs')
    gold={d['pmid']:d for d in json.loads(raw)['documents']}
    first,role,fp=Counter(),Counter(),Counter();coverage=Counter();errors=[];files=[]
    for row in report['per_paper']:
        path=report_path.parent/('paper-'+row['pmid']+'.json');prediction=json.loads(path.read_bytes())
        if prediction['identity']!=report['identity']:raise ValueError('Development prediction identity differs')
        oracle=report['mode']=='gold_entities';mapping=native_mapping(prediction['entities'])
        observed=evaluation_v6._metrics_for(gold[row['pmid']],prediction,mapping,oracle,'bioredirect')
        if observed!= {k:v for k,v in row.items() if k!='pmid'}:raise ValueError('Stored development metric differs')
        a,b,c,example=evaluation_v6.failures(gold[row['pmid']],prediction,mapping,oracle)
        first.update(a);role.update(b);fp.update(c);errors.extend(example)
        candidate=evaluation_v6.candidate_coverage(gold[row['pmid']],prediction)
        coverage.update({k:candidate[k] for k in ('tp','gold_relations')})
        files.append({'filename':path.name,'sha256':sha(path.read_bytes())})
    result={'schema':'v7-saved-development-diagnostics-1','engine':'heuristic-v7','mode':report['mode'],
        'papers':len(files),'report_sha256':sha(report_path.read_bytes()),'prediction_files':files,
        'first_failure_counts':dict(first),'direction_failure_counts':dict(role),'false_positive_counts':dict(fp),
        'candidate_coverage':{'tp':coverage['tp'],'total':coverage['gold_relations'],
                             'recall':coverage['tp']/max(1,coverage['gold_relations'])},
        'error_examples':sorted(errors,key=canonical),'selection_used_gold':False,'sealed_test_opened':False,
        'nutrition_accuracy':'unmeasured','evaluator_sha256':sha(Path(evaluation_v6.__file__).read_bytes())}
    if Path(output).exists():
        previous=json.loads(Path(output).read_bytes())
        normalized={**previous,'error_examples':sorted(previous['error_examples'],key=canonical)}
        if canonical(normalized)!=canonical(result):raise ValueError('Stored diagnostic inputs or counts changed')
        return previous
    immutable(output,result);return result
