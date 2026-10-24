"""Reproducible, resumable CPU public measurements with development/test separation.

Run development first. The test phase freezes all components before any test
prediction, and the benchmark library consumes each protected attempt once.
"""
import argparse
import json
import os
from pathlib import Path

from nutri_literature.benchmarks import (
    _provenance, _receipt, compare_public, evaluate_public, freeze_benchmark, predict_public, verify_receipt,
)
from nutri_literature.db import dumps, initialize, now, uid
from nutri_literature.public_corpora import prepare_public
from nutri_literature.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path)
    parser.add_argument('--phase', choices=['prepare', 'development', 'test'], required=True)
    parser.add_argument('--skip-unavailable-ebm-test',action='store_true',
                        help='Continue other frozen corpora only after an audited native EBM test diagnostic')
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.home:
        settings = Settings(args.home, settings.config)
    initialize(settings)
    os.environ.setdefault('LITERATURE_NLP_LOCK', '/home/keunsoo/projects/nutriomics-literature/runtime/locks/nlp-cpu.lock')
    output = settings.home / 'public_benchmarks' / 'v2-measurement-index.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(output.read_bytes()) if output.exists() else {'schema_version':'literature-public-measurement-index-v1','started_at':now(),'steps':{}}
    def step(key, operation):
        if key in state['steps']:
            return state['steps'][key]
        print(dumps({'at':now(),'step':key,'status':'started'}),flush=True)
        value = operation()
        state['steps'][key] = value
        state['updated_at'] = now()
        temporary = output.with_suffix('.tmp')
        temporary.write_text(dumps(state),encoding='utf-8')
        temporary.replace(output)
        print(dumps({'at':now(),'step':key,'status':'completed','documents':value.get('documents')}),flush=True)
        return value
    sources = [('biored','abstract'),('bioredirect','abstract'),('ebmnlp','abstract'),
               ('bioscope','abstract'),('bioscope','fulltext'),('evidenceoutcomes','abstract')]
    development = {}
    # Reserve official test IDs before importing overlapping preregistered sources.
    for corpus, level in sources:
        identity = corpus + ':' + level
        development[identity] = step('prepare:development:'+identity,
            lambda c=corpus,source_kind=level: prepare_public(settings,c,'development',source_level=source_kind))
    if args.phase == 'prepare':
        return
    engines = ['heuristic-v1','heuristic-v2']
    measured = {engine:_provenance(settings,engine)[1] for engine in engines}
    prior = state.get('provenance')
    if prior and prior != measured:
        core_prior = {engine:{k:v for k,v in value.items() if k != 'evaluator_source_hashes'}
                      for engine,value in prior.items()}
        core_current = {engine:{k:v for k,v in value.items() if k != 'evaluator_source_hashes'}
                        for engine,value in measured.items()}
        # The initial development receipt predates evaluator-file hash tracking.
        # Preserve it, and permit this one metadata addition before any test
        # attempt. All extraction, dictionary, parser and label mapping stay fixed.
        metadata_only = (args.phase == 'test' and state.get('development_complete')
                         and not any(key.startswith(('freeze:', 'prepare:test:', 'predict:test:'))
                                     for key in state['steps'])
                         and all('evaluator_source_hashes' not in p for p in prior.values())
                         and all('evaluator_source_hashes' in p for p in measured.values())
                         and core_prior == core_current)
        if not metadata_only:
            raise ValueError('Measured engine/dictionary/crosswalk changed; keep this index as history and start a new development release')
        migration = {'id':uid(), 'at':now(), 'prior_provenance':prior, 'current_provenance':measured,
                     'reason':'Freeze evaluator source hashes after development applicability checks; extraction, labels and projection unchanged'}
        _receipt(settings,'public_measurement_metadata_migration',migration['id'],migration)
        state.setdefault('provenance_history',[]).append(migration)
    state['provenance'] = measured
    if args.phase == 'test':
        if not state.get('development_complete'):
            raise ValueError('Complete development measurements before freezing test prediction')
        # Freeze both engines across all releases before reading any test labels.
        for identity,item in development.items():
            for engine in engines:
                step('freeze:'+identity+':'+engine,
                     lambda i=item,e=engine: freeze_benchmark(settings,i['inputs_path'],e))
    partition = 'development' if args.phase == 'development' else 'test'
    for corpus,level in sources:
        identity=corpus+':'+level
        if partition=='test' and corpus=='ebmnlp' and args.skip_unavailable_ebm_test:
            diagnostic_path=settings.home/'validation'/'ebm-test-diagnostic.json'
            diagnostic=json.loads(diagnostic_path.read_bytes())
            release=json.loads(Path(development[identity]['manifest_path']).read_bytes())
            if (not verify_receipt(settings,'public_corpus_diagnostic',diagnostic)
                    or diagnostic['source_sha256']!=release['source_sha256']
                    or diagnostic['status']!='native_test_layout_unsupported_in_frozen_protocol'
                    or diagnostic['parsed_test_papers']!=0 or diagnostic['native_expert_test_papers']<=0):
                raise ValueError('Cannot skip a corpus without its exact audited unsupported-source diagnostic')
            def unavailable():
                value={'id':uid(),'corpus':corpus,'source_level':level,'at':now(),
                       'status':'not_evaluated','reason':diagnostic['status'],'diagnostic_path':str(diagnostic_path),
                       'diagnostic_id':diagnostic['id'],'source_sha256':diagnostic['source_sha256']}
                _receipt(settings,'public_corpus_unavailable',value['id'],value)
                return value
            step('unavailable:test:'+identity,unavailable)
            continue
        prepared = development[identity] if partition == 'development' else step('prepare:test:'+identity,
            lambda c=corpus,source_kind=level: prepare_public(settings,c,'test',source_level=source_kind))
        modes = ['end_to_end','gold_entities'] if corpus in {'biored','bioredirect'} else ['end_to_end']
        for mode in modes:
            predictions={}
            for engine in engines:
                frozen = state['steps'].get('freeze:'+identity+':'+engine) if partition=='test' else None
                freeze_path = frozen['freeze_path'] if frozen else None
                key=partition+':'+identity+':'+mode+':'+engine
                predictions[engine]=step('predict:'+key,
                    lambda p=prepared,e=engine,f=freeze_path,m=mode: predict_public(settings,p['inputs_path'],e,f,m))
                step('evaluate:'+key,
                    lambda p=prepared,r=predictions[engine],f=freeze_path: evaluate_public(settings,p['gold_path'],r['prediction_run'],f))
            step('compare:'+partition+':'+identity+':'+mode,
                 lambda p=prepared,a=predictions['heuristic-v1'],b=predictions['heuristic-v2']:
                     compare_public(settings,p['gold_path'],a['prediction_run'],b['prediction_run'],iterations=2000))
        if partition == 'development' and corpus=='bioredirect':
            for stage in ['entities_v2_relations_v1','syntax_without_context']:
                key=partition+':'+identity+':gold_entities:'+stage
                prediction=step('predict:'+key,lambda p=prepared,a=stage:
                    predict_public(settings,p['inputs_path'],'heuristic-v2',mode='gold_entities',ablation_stage=a))
                step('evaluate:'+key,lambda p=prepared,r=prediction:
                    evaluate_public(settings,p['gold_path'],r['prediction_run']))
    unavailable=[value for key,value in state['steps'].items() if key.startswith('unavailable:test:')]
    state[partition+'_complete']=not unavailable if partition=='test' else True
    if partition=='test':
        state['test_execution_complete']=True
        state['test_unavailable']=unavailable
    state['updated_at']=now()
    output.write_text(dumps(state),encoding='utf-8')
    print(dumps({'status':'partial_complete' if partition=='test' and unavailable else 'complete',
                 'phase':partition,'index_path':str(output)}),flush=True)


if __name__=='__main__':
    main()
