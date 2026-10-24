"""Read-only model diagnostics on normalization TRAIN folds; never changes a policy."""
import argparse
import collections
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v6_gpu.common import (
    HOME, POLICY, atomic, canonical, filehash, foldmap, lock, read, sha, wilson,
)


def alignment(document, mention):
    entities = [e for e in document.get('entities', []) if e['kind'] == mention['kind']]
    matched = [e for e in entities if (e['start'], e['end']) == (mention['start'], mention['end'])]
    if matched:
        return 'exact_annotated_boundary', {v for e in matched for v in e.get('native_ids', [])}
    if any(e['start'] < mention['end'] and mention['start'] < e['end'] for e in entities):
        return 'overlapping_annotation_different_boundary', set()
    return 'no_annotation_at_position_not_a_biological_error_verdict', set()


def reason(document, mention, record):
    status, truth = alignment(document, mention)
    if not truth:
        if status == 'exact_annotated_boundary':
            return 'exact_annotation_without_reference_ID'
        return status
    if record['strict_correct']:
        return 'correct_proposed_ID'
    if not truth.intersection(mention['candidate_ids'][:50]):
        return 'gold_ID_absent_from_inference_menu_50'
    if not record.get('species_evidence_compatible'):
        return 'species_evidence_blocks_selection'
    return 'gold_ID_in_menu_but_ranked_below_wrong_ID_or_NIL'


def diagnostic_grid(records, category, kind=None):
    eligible = [r for r in records if r.get('annotation_scope_complete')
                and r.get('species_evidence_compatible') and r['proposed_id']
                and r['proposed_id'] != r['previous_id']
                and bool(r['previous_id']) == (category == 'change')
                and (kind is None or r['kind'] == kind)]
    options = []
    for threshold in POLICY['thresholds']:
        for margin in (0., .25, .5, 1., 2.):
            chosen = [r for r in eligible if r['priority'] >= threshold and r['margin'] >= margin]
            n = len(chosen)
            if n >= POLICY['id_minimum']:
                ok = sum(bool(r['strict_correct']) for r in chosen)
                options.append({'threshold': threshold, 'margin': margin, 'n': n, 'correct': ok,
                                'precision': ok/n, 'wilson_lower': wilson(ok, n)})
    return {'eligible': len(eligible), 'best_supported_lower_bound':
            max(options, key=lambda r: (r['wilson_lower'], r['n'])) if options else None}


def summarize_automatic(documents, decisions, prepared):
    bypaper = {d['pmid']: d for d in documents}
    counts = collections.Counter()
    groups = {}
    examples = []
    example_counts = collections.Counter()
    for record in decisions:
        if not record.get('annotation_scope_complete'):
            counts['unannotated_kind_excluded'] += 1
            continue
        d = bypaper[record['pmid']]
        m = prepared[record['pmid']][record['key']]
        if d['text'][m['start']:m['end']] != m['text']:
            raise ValueError('Diagnostic mention does not match saved source')
        category = 'change' if record['previous_id'] else 'new'
        group = groups.setdefault(record['kind'] + ':' + category, {
            'mentions': 0, 'different_non_NIL_proposals': 0,
            'high_priority_099': 0, 'high_priority_099_correct': 0,
            'correct_previous_damage_averted': 0, 'wrong_previous_repair_opportunities': 0,
            'correct_previous_damage_averted_all_proposals': 0,
            'actual_new_or_changed': 0, 'actual_new_or_changed_correct': 0,
            'high_priority_first_failure': collections.Counter(),
            'high_priority_mention_sources': collections.Counter(),
        })
        group['mentions'] += 1
        if record['reason'] in ('new', 'changed'):
            group['actual_new_or_changed'] += 1
            group['actual_new_or_changed_correct'] += bool(record['selected_correct'])
        if not record['proposed_id'] or record['proposed_id'] == record['previous_id']:
            continue
        group['different_non_NIL_proposals'] += 1
        group['correct_previous_damage_averted_all_proposals'] += bool(record['previous_correct']) and not bool(record['strict_correct']) and record['selected_id'] == record['previous_id']
        if not record.get('species_evidence_compatible'):
            counts['different_proposal_species_blocked'] += 1
            continue
        if record['priority'] < .99:
            continue
        group['high_priority_099'] += 1
        group['high_priority_099_correct'] += bool(record['strict_correct'])
        first = reason(d, m, record)
        group['high_priority_first_failure'][first] += 1
        group['high_priority_mention_sources'][m.get('source', 'NER_model')] += 1
        group['correct_previous_damage_averted'] += bool(record['previous_correct']) and not bool(record['strict_correct']) and record['selected_id'] == record['previous_id']
        group['wrong_previous_repair_opportunities'] += bool(record['previous_id']) and not bool(record['previous_correct']) and bool(record['strict_correct'])
        example_group = (record['kind'], category, first)
        if not record['strict_correct'] and example_counts[example_group] < 2 and len(examples) < 40:
            example_counts[example_group] += 1
            examples.append({'pmid': d['pmid'], 'kind': record['kind'],
                             'start': m['start'], 'end': m['end'], 'text': m['text'],
                             'source_context': d['text'][max(0,m['start']-120):m['end']+120],
                             'mention_source': m.get('source', 'NER_model'),
                             'previous_id': record['previous_id'], 'proposed_id': record['proposed_id'],
                             'previous_correct_by_annotation': record['previous_correct'],
                             'first_failure': first, 'priority': record['priority'],
                             'priority_is_accuracy_probability': False,
                             'diagnostic_only_not_a_model_exception': True})
    return {'counts': dict(counts), 'groups': groups, 'training_only_examples': examples}


def analyze():
    documents = read(HOME/'data/identity-train.json')['documents']
    folds = foldmap(documents, 5, 'independent-ner-id')
    result = []
    for f in range(5):
        path = HOME/'identity'/('outer-'+str(f)+'.json')
        if not path.exists():
            continue
        outer = read(path)
        training = [d for d in documents if folds[d['pmid']] != f]
        inner = foldmap(training, 3, 'identity-inner')
        calibration = []
        for inner_f in range(3):
            pmids = sorted(d['pmid'] for d in training if inner[d['pmid']] != inner_f)
            model = HOME/'identity/rankers'/sha(canonical(pmids))[:18]
            cache = HOME/'identity/score-cache'/sha(str(model.relative_to(HOME))+'id-supervision')
            for p in sorted(cache.glob('*.json')):
                calibration.extend(read(p)['records'])
        held = [d for d in documents if d['pmid'] in outer['held_pmids']]
        prepared = {d['pmid']: {m['key']:m for m in read(HOME/'prepared/normalization'/('paper-'+d['pmid']+'.json'))['mentions']} for d in held}
        result.append({'fold': f, 'held_papers': len(held), 'actual_policy': outer['policy'],
                       'calibration_records': len(calibration),
                       'calibration_policy_diagnostics': {
                           category: {kind: diagnostic_grid(calibration, category, None if kind == 'all' else kind)
                                      for kind in ('all', 'gene', 'chemical')}
                           for category in ('new', 'change')},
                       'automatic': summarize_automatic(held, outer['decisions'], prepared),
                       'outer_receipt_sha256': filehash(path)})
    value = {'as_of_unix': time.time(), 'completed_outer_folds': len(result), 'folds': result,
             'scope': 'Normalization training folds only; not the development 100 papers/154 relations',
             'training_menu_limit': 16, 'inference_menu_limit': 50,
             'training_gold_ID_injection': True, 'inference_gold_ID_injection': False,
             'calibration_profile': 'gold-boundary supervision with at most three ordinary-word NIL examples per paper',
             'outer_profile': 'automatic OOF NER plus whole official-alias candidates',
             'selection_gate': {'minimum': POLICY['id_minimum'], 'wilson_95_lower': POLICY['id_precision']},
             'development_gold_read': False, 'models_or_policies_modified': False,
             'data_sha256': filehash(HOME/'data/identity-train.json'),
             'analysis_source_sha256': filehash(Path(__file__)),
             'final_for_id_stage': (HOME/'identity/selection.json').exists()}
    atomic(HOME/'operational/id-disabled-analysis.json', value)
    if value['final_for_id_stage']:
        final_path = HOME/'diagnostics/id-disabled-analysis.json'
        if final_path.exists():
            previous = read(final_path)
            if previous['analysis_source_sha256'] != value['analysis_source_sha256'] or previous['data_sha256'] != value['data_sha256']:
                raise ValueError('Final ID diagnostic inputs changed')
        else:
            atomic(final_path, value, True)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    if hasattr(os, 'nice'):
        os.nice(10)
    state_path = HOME/'operational/id-diagnostic-worker.json'
    state = {'pid': os.getpid(), 'running': True, 'started': time.time(), 'mode': 'read-only-training-diagnostics'}
    atomic(state_path, state)
    try:
        previous = None
        while True:
            signature = [filehash(p) for p in sorted((HOME/'identity').glob('outer-*.json'))]
            signature.append((HOME/'identity/selection.json').exists())
            if signature != previous:
                v = analyze(); previous = signature
                state.update(last_analysis=time.time(), completed_outer_folds=v['completed_outer_folds'], final_for_id_stage=v['final_for_id_stage'])
                atomic(state_path, state)
                print(json.dumps({k:v[k] for k in ('completed_outer_folds','final_for_id_stage','models_or_policies_modified')}), flush=True)
            if not args.watch or signature[-1]:
                state['exit'] = 'completed'; break
            job = read(HOME/'job-state.json')
            if not job.get('running'):
                state['exit'] = 'incomplete-main-job-stopped'; break
            if time.time()-state['started'] >= 72*3600:
                state['exit'] = 'incomplete-finite-wall-limit'; break
            time.sleep(60)
    except Exception as exc:
        state.update(exit='failed', error_class=type(exc).__name__)
        raise
    finally:
        state.update(running=False, finished=time.time()); atomic(state_path, state)


if __name__ == '__main__':
    with lock(HOME/'operational/id-diagnostic-lock'):
        main()
