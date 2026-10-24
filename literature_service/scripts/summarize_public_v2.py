"""Export measured public endpoints and the real promotion decision, without text."""
import json

from nutri_literature.db import dumps, now
from nutri_literature.operations_v2 import soak_receipt
from nutri_literature.promotion import promotion_status
from nutri_literature.settings import Settings


def summarize(settings):
    source=settings.home/'public_benchmarks'/'v2-measurement-index.json'
    index=json.loads(source.read_bytes())
    measured=[]
    comparisons=[]
    for key,value in index['steps'].items():
        if key.startswith('evaluate:'):
            measured.append({'key':key,**{name:value.get(name) for name in [
                'id','corpus','split','mode','documents','official_split','preregistered_split',
                'complete_partition','freeze_id','ablation_stage','metrics','normalization',
                'relation_by_type','relation_coverage','provenance_coverage','scope_by_source',
                'annotation_applicability','output_path',
            ]},'engine':value['provenance']['engine_version']})
        elif key.startswith('compare:'):
            comparisons.append({'key':key,**{name:value.get(name) for name in [
                'id','corpus','split','mode','documents','bootstrap','output_path',
            ]}})
    result={'schema_version':'literature-public-measurement-summary-v1','measured_at':now(),
            'development_complete':index.get('development_complete',False),
            'test_complete':index.get('test_complete',False),'measurements':measured,
            'test_execution_complete':index.get('test_execution_complete',False),
            'test_unavailable':index.get('test_unavailable',[]),
            'comparisons':comparisons,'provenance':index.get('provenance'),
            'nutrition_accuracy':'unmeasured_pending_domain_gold','review_time_saved':'unmeasured'}
    if index.get('test_complete') or index.get('test_execution_complete'):
        # The prespecified gene endpoint is BioRED; role-aware relations are
        # measured on BioREDirect. Other public endpoints remain in the report.
        keys=['evaluate:test:biored:abstract:end_to_end:heuristic-v2',
              'evaluate:test:bioredirect:abstract:gold_entities:heuristic-v2',
              'evaluate:test:bioscope:abstract:end_to_end:heuristic-v2',
              'evaluate:test:bioscope:fulltext:end_to_end:heuristic-v2']
        reports=[index['steps'][key] for key in keys]
        comparison=index['steps']['compare:test:bioredirect:abstract:gold_entities']
        operation=soak_receipt(settings)
        result['promotion']=promotion_status(reports,comparison,operation,settings=settings)
        result['operational_receipt_path']=operation['output_path']
    target=settings.home/'validation'/'public-v2-summary.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(dumps(result),encoding='utf-8')
    return {**result,'output_path':str(target)}


if __name__=='__main__':
    value=summarize(Settings.from_env())
    print(dumps({'output_path':value['output_path'],'development_complete':value['development_complete'],
                 'test_complete':value['test_complete'],'measurements':len(value['measurements']),
                 'promotion':value.get('promotion')}))
