"""Decompose frozen training candidate losses; never tune from development labels."""
from collections import Counter
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import evaluation_v6
    from nutri_literature.chatgpt_repair_io import HOME
    from nutri_literature.chatgpt_evaluation import _read
    from nutri_literature.contract_v7 import sha, immutable

    selection = _read(HOME / 'selection.json')
    documents = _read(HOME / 'inputs/train.json')['documents']
    counts, losses, records = Counter(), Counter(), []
    for doc in documents:
        name = 'paper-' + doc['pmid'] + '.json'
        payload = _read(HOME / 'payloads/train' / name)
        rows = _read(HOME / 'training-candidates' / name)
        expected = evaluation_v6.relation_sets(doc, {'relations': []})[0]
        menu = {kind: {c['native_id'] for m in payload['mentions'] if m['kind'] == kind
                       for c in m['candidates']} for kind in ('chemical', 'gene')}
        selected_ids = {identifier for row in rows for identifier in row['key'][:2]}
        pairs = {tuple(sorted(row['key'][:2])) for row in rows}
        candidates = {(doc['pmid'], tuple(sorted(row['key'][:2])), row['key'][2]) for row in rows}
        chemical_ids = {identifier for entity in doc['entities'] if entity['kind'] == 'component'
                        for identifier in entity.get('native_ids', [])}
        gene_ids = {identifier for entity in doc['entities'] if entity['kind'] == 'gene'
                    for identifier in entity.get('native_ids', [])}
        for key in sorted(expected):
            cids, gids = set(key[1]) & chemical_ids, set(key[1]) & gene_ids
            if len(cids) != 1 or len(gids) != 1:
                raise ValueError('Ambiguous gold endpoint roles')
            cid, gid = next(iter(cids)), next(iter(gids))
            flags = {
                'chemical_in_menu': cid in menu['chemical'], 'gene_in_menu': gid in menu['gene'],
                'both_in_menu': cid in menu['chemical'] and gid in menu['gene'],
                'chemical_in_selected_rows': cid in selected_ids, 'gene_in_selected_rows': gid in selected_ids,
                'both_ids_in_selected_rows': cid in selected_ids and gid in selected_ids,
                'pair_in_rows': key[1] in pairs, 'type_in_rows': key in candidates,
            }
            counts.update(k for k, value in flags.items() if value)
            order = [
                ('chemical_missing_from_menu', flags['chemical_in_menu']),
                ('gene_missing_from_menu', flags['gene_in_menu']),
                ('chemical_missing_from_selected_rows', flags['chemical_in_selected_rows']),
                ('gene_missing_from_selected_rows', flags['gene_in_selected_rows']),
                ('pair_not_instantiated', flags['pair_in_rows']),
                ('type_not_instantiated', flags['type_in_rows']),
            ]
            first = next((reason for reason, passed in order if not passed), 'candidate_available')
            losses[first] += 1
            records.append({'key': key, 'checks': flags, 'first_loss_in_declared_order': first})
    if len(documents) != 384 or len(records) != 549 or counts['type_in_rows'] != selection['candidate_true_relations']:
        raise ValueError('Training coverage differs from frozen native candidate counts')
    result = {
        'papers': 384, 'gold_relations': 549, 'coverage_counts': dict(counts),
        'first_loss_counts': dict(losses), 'records': records,
        'selection_sha256': sha((HOME / 'selection.json').read_bytes()),
        'training_input_sha256': sha((HOME / 'inputs/train.json').read_bytes()),
        'post_selection_diagnostic_only': True, 'development_labels_read': False, 'policy_modified': False,
        'interpretation': 'Per-document native-ID coverage, not mention-boundary accuracy. First loss follows the declared order; independent missing-ID counts are also reported.',
    }
    output = HOME / 'analysis/training-candidate-losses.json'
    immutable(output, result)
    immutable(HOME / 'analysis/training-candidate-losses-reproduction.json', {
        'report_sha256': sha(output.read_bytes()), 'source_sha256': sha(Path(__file__).read_bytes()),
        'reproduced_native_training_candidate_count': selection['candidate_true_relations'],
        'selection_sha256': result['selection_sha256'], 'model_or_threshold_changed': False,
    })
    print(json.dumps({k: value for k, value in result.items() if k != 'records'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
