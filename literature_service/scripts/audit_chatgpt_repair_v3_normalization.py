"""Full automatic versus aligned-only normalization denominators, after scoring."""
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v3.common import HOME, _read, immutable, sha, verify_source_freeze
from nutri_literature.repair_v3.runner import gold, load_papers
from nutri_literature.repair_v3.identity import choose, aligned_ids


def main():
    verify_source_freeze()
    docs, _ = gold()
    truth = {d['pmid']: d for d in docs}
    policy = _read(HOME/'selection.json')['model']['identity']
    counts = Counter()
    for paper in load_papers('development'):
        document = truth[paper['pmid']]
        for m in paper['mentions']:
            predicted = choose(m, policy['model'], policy['threshold'], policy['margin'], policy['enabled'])['selected_id']
            if predicted is None:
                continue
            kind = 'component' if m['kind'] == 'chemical' else 'gene'
            exact = {i for e in document['entities'] if e['kind'] == kind
                     and e['start'] == m['start'] and e['end'] == m['end'] for i in e.get('native_ids', [])}
            relaxed = aligned_ids(m, document)
            counts['all_selected'] += 1
            counts['all_strict_boundary_id_correct'] += int(predicted in exact)
            new = predicted not in m['old_ids']
            if new:
                counts['new_selected'] += 1
                counts['new_strict_boundary_id_correct'] += int(predicted in exact)
                counts['new_aligned_subset'] += int(bool(relaxed))
                counts['new_aligned_id_correct'] += int(predicted in relaxed)
                counts['new_unaligned_or_ambiguous'] += int(not relaxed)
    result = {'schema': 'repair-v3-full-normalization-audit', 'counts': dict(counts),
        'strict_new_boundary_id_precision': counts['new_strict_boundary_id_correct']/max(1, counts['new_selected']),
        'conditional_aligned_new_id_precision': counts['new_aligned_id_correct']/max(1, counts['new_aligned_subset']),
        'scope': 'same exposed development100; unaligned automatic mentions remain in strict denominator',
        'unmatched_is_entity_boundary_or_type_error_not_proven_wrong_id': True,
        'policy_reselected': False, 'new_inference': 0}
    target = HOME.with_name('chatgpt-repair-v3-postscore')
    immutable(target/'normalization.json', result)
    immutable(target/'normalization-manifest.json', {
        'core_delivery_manifest_sha256': sha((HOME/'delivery-manifest.json').read_bytes()),
        'script_sha256': sha(Path(__file__).read_bytes()),
        'normalization_sha256': sha((target/'normalization.json').read_bytes())})
    print(result)


if __name__ == '__main__':
    main()
