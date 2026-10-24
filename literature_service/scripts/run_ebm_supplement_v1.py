"""Measure a separately registered expert subset without retrying protected runs."""
import json
import os
from pathlib import Path

from nutri_literature.benchmarks import (
    _receipt, compare_public, evaluate_public, freeze_benchmark, predict_public, verify_receipt,
)
from nutri_literature.db import digest, dumps, initialize, now, uid
from nutri_literature.settings import Settings
from prepare_ebm_expert_supplement import _validated_protocol, prepare_supplement, register_protocol


STEP_KIND = 'ebm_expert_supplement_step_v1'


def main():
    settings = Settings.from_env()
    initialize(settings)
    os.environ.setdefault('LITERATURE_NLP_LOCK', str(settings.home / 'locks' / 'nlp-cpu.lock'))
    primary = json.loads((settings.home / 'public_benchmarks' / 'v2-measurement-index.json').read_bytes())
    original = primary['steps']['prepare:development:ebmnlp:abstract']
    source = Path(original['inputs_path']).parent / 'source.release'
    output = settings.home / 'public_benchmarks' / 'ebm-expert-supplement-v1'
    registered = register_protocol(
        settings, source,
        primary['steps']['freeze:ebmnlp:abstract:heuristic-v1']['freeze_path'],
        primary['steps']['freeze:ebmnlp:abstract:heuristic-v2']['freeze_path'], output,
    )
    protocol_path = Path(registered['protocol_path'])
    protocol = registered['protocol']
    state_path = output / 'measurement-index.json'
    identity = {'protocol_id': protocol['id'], 'protocol_sha256': digest(dumps(protocol)),
                'runner_source_sha256': digest(Path(__file__).read_bytes())}
    if state_path.exists():
        state = json.loads(state_path.read_bytes())
        if state['identity'] != identity:
            raise ValueError('Supplement protocol or runner changed; preserve prior attempts')
    else:
        state = {'schema_version': 'literature-ebm-expert-supplement-measurement-v1',
                 'identity': identity, 'started_at': now(), 'steps': {}}

    def step(key, operation):
        _validated_protocol(settings, protocol_path)
        if key in state['steps']:
            entry = state['steps'][key]
            if not verify_receipt(settings, STEP_KIND, entry):
                raise ValueError('Supplement measurement step differs from audited receipt')
            return entry['result']
        print(dumps({'at': now(), 'step': key, 'status': 'started'}), flush=True)
        result = operation()
        entry = {'id': uid(), 'at': now(), 'key': key, 'identity': identity, 'result': result}
        _receipt(settings, STEP_KIND, entry['id'], entry)
        state['steps'][key] = entry
        state['updated_at'] = now()
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(dumps(state), encoding='utf-8')
        temporary.replace(state_path)
        print(dumps({'at': now(), 'step': key, 'status': 'completed',
                     'documents': result.get('documents')}), flush=True)
        return result

    prepared = step('prepare', lambda: prepare_supplement(settings, protocol_path))
    frozen = {}
    for engine in ['heuristic-v1', 'heuristic-v2']:
        frozen[engine] = step('freeze:' + engine,
                              lambda e=engine: freeze_benchmark(settings, prepared['inputs_path'], e))
    predictions = {}
    measured = {}
    for engine in ['heuristic-v1', 'heuristic-v2']:
        predictions[engine] = step('predict:' + engine, lambda e=engine: predict_public(
            settings, prepared['inputs_path'], e, freeze_path=frozen[e]['freeze_path']))
        measured[engine] = step('evaluate:' + engine, lambda e=engine: evaluate_public(
            settings, prepared['gold_path'], predictions[e]['prediction_run']))
    comparison = step('compare', lambda: compare_public(
        settings, prepared['gold_path'], predictions['heuristic-v1']['prediction_run'],
        predictions['heuristic-v2']['prediction_run'], iterations=2000))
    summary = {
        'schema_version': 'literature-ebm-expert-supplement-summary-v1',
        'id': uid(), 'measured_at': now(), 'protocol': protocol,
        'prepared': prepared, 'supplement_completed': True,
        'original_protocol_test_complete': False, 'original_expert_test_complete': False,
        'measurements': {engine: {key: value for key, value in result.items() if key != 'per_paper'}
                         for engine, result in measured.items()},
        'comparison': comparison,
        'nutrition_accuracy': 'unmeasured_pending_domain_gold',
        'no_algorithm_changes_or_tuning': True,
    }
    target = settings.home / 'validation' / 'ebm-expert-supplement-summary.json'
    # A completed run can be re-exported with the same audited measurements.
    if target.exists():
        previous = json.loads(target.read_bytes())
        if {k: v for k, v in previous.items() if k not in {'id', 'measured_at'}} != {
            k: v for k, v in summary.items() if k not in {'id', 'measured_at'}
        }:
            raise ValueError('Supplement summary changed after measured completion')
        summary = previous
    else:
        _receipt(settings, 'ebm_expert_supplement_summary_v1', summary['id'], summary)
        target.write_text(dumps(summary), encoding='utf-8')
    print(dumps({'summary_path': str(target), 'supplement_completed': True,
                 'documents': prepared['documents'], 'original_protocol_test_complete': False}))


if __name__ == '__main__':
    main()
