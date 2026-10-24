"""Fail-closed final protocol: raw re-score, bound artifacts, one cohort attempt."""
import json
from pathlib import Path
from .contract_v7 import immutable,sha,sources,passes,counts,canonical


def verify_report(path):
    from . import evaluation_v6
    path=Path(path);r=json.loads(path.read_bytes())
    if r.get('engine')!='heuristic-v7' or r.get('mode') not in ('end_to_end','gold_entities'):
        raise ValueError('V7 automatic/oracle report required')
    required=('input_sha256','gold_sha256','identity','per_paper')
    if any(not r.get(k) for k in required):raise ValueError('Unbound scientific report')
    if r['identity']['evaluator_sha256']!=sha(Path(evaluation_v6.__file__).read_bytes()):
        raise ValueError('Scientific evaluator changed')
    # Recompute totals from every paper rather than trusting a claimed pass.
    from . import benchmarks
    metric=benchmarks._aggregate(r['per_paper'],'relation')
    if metric!=r['metrics']['relation'] or not passes(metric):
        raise ValueError('Relation precision/recall gate not met')
    for row in r['per_paper']:
        file=path.parent/('paper-'+row['pmid']+'.json')
        if not file.exists():raise ValueError('Saved predictions missing')
        value=json.loads(file.read_bytes())
        if value['identity']!=r['identity']:raise ValueError('Prediction artifact changed')
    from .mapping_v7 import native_mapping
    raw_gold=Path(r['prepared_gold_path']).read_bytes()
    raw_inputs=Path(r['prepared_inputs_path']).read_bytes()
    if sha(raw_gold)!=r['gold_sha256'] or sha(raw_inputs)!=r['input_sha256']:
        raise ValueError('Stored comparison input/gold changed')
    gold={d['pmid']:d for d in json.loads(raw_gold)['documents']}
    for row in r['per_paper']:
        prediction=json.loads((path.parent/('paper-'+row['pmid']+'.json')).read_bytes())
        actual=evaluation_v6._metrics_for(gold[row['pmid']],prediction,native_mapping(prediction['entities']),r['mode']=='gold_entities','bioredirect')
        if actual['relation']!=row['relation']:raise ValueError('Raw prediction re-score differs')
    return r


def freeze(head,automatic,oracle,regression,audit,output,events=None):
    head=Path(head)
    model=json.loads((head/'manifest.json').read_bytes())
    if not passes(model['outer_metrics']):raise ValueError('Nested train-only automatic gate not met')
    if events is None:raise ValueError('CG official event comparison receipt is required; rule passage is not event accuracy')
    event=json.loads(Path(events).read_bytes())
    if (event.get('evaluation_scope')!='cg_official_event_instances' or event.get('current_model_sha256')!=sha((head/'manifest.json').read_bytes())
            or event.get('current_f1',0)<=event.get('baseline_f1',0)):
        raise ValueError('CG source-event improvement gate not met')
    if sha(Path(event['official_script_path']).read_bytes())!=event['official_script_sha256']:
        raise ValueError('Official CG evaluator identity differs')
    auto,provided=verify_report(automatic),verify_report(oracle)
    if auto['identity'].get('identity_model_manifest_sha256'):
        raise ValueError('Combined ID profile requires joint fold-isolated stack evaluation before final freezing')
    expected_model=sha((head/'manifest.json').read_bytes())
    if any(r['identity']['model_manifest_sha256']!=expected_model for r in (auto,provided)):
        raise ValueError('Comparison used a different model')
    if auto['mode']!='end_to_end' or provided['mode']!='gold_entities':raise ValueError('Both entity conditions required')
    if auto['input_sha256']!=provided['input_sha256'] or auto['gold_sha256']!=provided['gold_sha256']:
        raise ValueError('Automatic and oracle evaluated different papers')
    if not passes(model['outer_metrics']):raise ValueError('Nested train-only automatic gate not met')
    proof=json.loads(Path(regression).read_bytes());protection=json.loads(Path(audit).read_bytes())
    if protection.get('passed') is not True or protection.get('protected_overlap')!=0:
        raise ValueError('Heldout protection audit failed')
    if proof.get('passed') is not True or any(proof.get(k)!=0 for k in ('offset_mismatches','role_reversals','planned_as_observed','wrong_condition_merges')):
        raise ValueError('Source and semantic regression gates failed')
    record={'schema':'v7-frozen-relation-1','sources':sources()[0],'rules_version':sources()[1],
            'head_path':str(head.resolve()),'head_manifest_sha256':sha((head/'manifest.json').read_bytes()),
            'automatic_report_sha256':sha(Path(automatic).read_bytes()),'oracle_report_sha256':sha(Path(oracle).read_bytes()),
            'regression_sha256':sha(Path(regression).read_bytes()),'audit_sha256':sha(Path(audit).read_bytes()),
            'cg_events_sha256':sha(Path(events).read_bytes()),
            'nutrition_accuracy':'unmeasured','default_engine':'heuristic-v1'}
    immutable(output,record)
    return record


def reserve_attempt(home,spec,freeze_path):
    home=Path(home);spec_path=Path(spec)
    value=json.loads(spec_path.read_bytes());frozen=json.loads(Path(freeze_path).read_bytes())
    if frozen['schema']!='v7-frozen-relation-1' or frozen['sources']!=sources()[0]:
        raise ValueError('Frozen source identity changed')
    if value.get('split') not in ('test','final') or value.get('corpus') not in ('bc8','bioredirect'):
        raise ValueError('Protected BC8 final specification required')
    key=value.get('cohort_id') or value.get('inputs_sha256')
    if not key:raise ValueError('Final cohort identity missing')
    record={'cohort':key,'spec_sha256':sha(spec_path.read_bytes()),'freeze_sha256':sha(Path(freeze_path).read_bytes()),
            'test_became_exposed':True,'status':'reserved_before_gold'}
    # Different artifacts cannot consume the same cohort under an independent claim.
    immutable(home/'v7/final-attempts'/(sha(key)+'.json'),record)
    return value,frozen
