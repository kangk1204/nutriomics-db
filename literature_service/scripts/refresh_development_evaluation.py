"""Re-evaluate immutable development predictions before the protected freeze.

Fixes evaluator applicability, never predictions, partitions, labels or engines.
Old report receipts remain immutable and their pointers are audited as history.
"""
import json

from nutri_literature.benchmarks import (
    _receipt, compare_public, evaluate_public, evaluator_source_hashes,
)
from nutri_literature.db import digest, dumps, now, uid
from nutri_literature.settings import Settings


def main():
    settings=Settings.from_env()
    path=settings.home/'public_benchmarks'/'v2-measurement-index.json'
    state=json.loads(path.read_bytes())
    if not state.get('development_complete') or any(key.startswith(('freeze:','predict:test:')) for key in state['steps']):
        raise ValueError('Refresh requires completed development and no protected freeze/attempt')
    identity=digest(dumps(evaluator_source_hashes()))
    marker='development-evaluator-refresh:'+identity
    if state.get(marker) is True:
        return
    history=state.setdefault('development_evaluation_history',{})
    if identity not in history:
        record={'id':uid(),'at':now(),'evaluator_source_hashes':evaluator_source_hashes(),
                'reason':'Restrict metrics to each public corpus annotation coverage before final test',
                'prior_reports':{key:{'id':value['id'],'output_path':value['output_path']}
                                 for key,value in state['steps'].items()
                                 if key.startswith(('evaluate:development:','compare:development:'))}}
        _receipt(settings,'public_development_evaluation_refresh',record['id'],record)
        history[identity]=record
    def save():
        state['updated_at']=now()
        temporary=path.with_suffix('.tmp')
        temporary.write_text(dumps(state),encoding='utf-8')
        temporary.replace(path)
    save()
    for key,prediction in list(state['steps'].items()):
        if not key.startswith('predict:development:'):
            continue
        evaluated='evaluate:'+key[len('predict:'):]
        if state.get(marker+':'+evaluated):
            continue
        pieces=key.split(':')
        prepared=state['steps']['prepare:development:'+':'.join(pieces[2:4])]
        print(dumps({'at':now(),'step':evaluated,'status':'refresh_started'}),flush=True)
        state['steps'][evaluated]=evaluate_public(settings,prepared['gold_path'],prediction['prediction_run'],reuse=True)
        state[marker+':'+evaluated]=True
        save()
    for key in list(state['steps']):
        if not key.startswith('compare:development:') or state.get(marker+':'+key):
            continue
        pieces=key.split(':')
        identity_key=':'.join(pieces[2:4])
        mode=pieces[4]
        prepared=state['steps']['prepare:development:'+identity_key]
        prefix='predict:development:'+identity_key+':'+mode+':'
        baseline=state['steps'][prefix+'heuristic-v1']
        candidate=state['steps'][prefix+'heuristic-v2']
        print(dumps({'at':now(),'step':key,'status':'refresh_started'}),flush=True)
        state['steps'][key]=compare_public(settings,prepared['gold_path'],baseline['prediction_run'],candidate['prediction_run'],iterations=2000)
        state[marker+':'+key]=True
        save()
    state[marker]=True
    save()
    print(dumps({'status':'refreshed','index_path':str(path)}),flush=True)


if __name__=='__main__':
    main()
