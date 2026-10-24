"""Export every frozen type hypothesis, including discarded alternatives."""
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.chatgpt_repair_io import HOME
    from nutri_literature.chatgpt_evaluation import _read
    from nutri_literature.chatgpt_repair_learning import make_rows, priority, project_rows
    from nutri_literature.chatgpt_repair_runner import inputs_for
    from nutri_literature.contract_v7 import canonical, immutable, sha
    if not (HOME / 'completion-audit.json').exists():
        raise ValueError('Finish frozen evaluation before exporting post-hoc traces')
    selected = _read(HOME / 'selection.json')
    checksum = sha((HOME / 'selection.json').read_bytes())
    documents = _read(HOME / 'inputs/development-sources.json')['documents']
    totals, files = Counter(), {}
    for doc in documents:
        payload, parsed, previous = inputs_for(doc, 'development')
        rows = make_rows(payload, parsed, previous)
        scores = [priority(row, selected['model']) for row in rows]
        projection = project_rows(rows, scores, selected['selected']['threshold'])
        name = 'paper-' + doc['pmid'] + '.json'
        if canonical(projection) != canonical(_read(HOME / 'predictions/combined' / name)):
            raise ValueError('Candidate reconstruction differs from frozen prediction')
        chosen = {tuple(d['key']): d for d in projection['decisions']}
        hypotheses = []
        for row, score in zip(rows, scores):
            decision = chosen.get(tuple(row['key']))
            reason = ('not_primary_document_type' if decision is None else
                      'accepted' if decision['accepted'] else 'below_training_selected_cutoff')
            totals[reason] += 1
            hypotheses.append({'key': row['key'], 'priority': score, 'reason': reason,
                               'priority_is_accuracy_probability': False,
                               'relation_with_source': row['relation'],
                               'features': row['features'], 'previous_label': row['old_label']})
        path = HOME / 'analysis/hypotheses' / name
        immutable(path, {'pmid': doc['pmid'], 'source_sha256': sha(doc['text']),
                         'selection_sha256': checksum, 'previous_relations': previous['relations'],
                         'hypotheses': hypotheses, 'human_approved': False,
                         'model_or_threshold_changed': False})
        files[str(path.relative_to(ROOT))] = sha(path.read_bytes())
    if len(files) != 100:
        raise ValueError('All100 source documents are required')
    result = {'papers': 100, 'counts': dict(totals), 'artifacts': files,
              'selection_sha256': checksum, 'source_sha256': sha(Path(__file__).read_bytes()),
              'development_labels_read': False, 'projection_identical': True,
              'policy_reselected': False, 'default_engine': 'heuristic-v1'}
    immutable(HOME / 'analysis/hypotheses-manifest.json', result)
    print({'papers': 100, 'counts': dict(totals), 'projection_identical': True})


if __name__ == '__main__':
    main()
