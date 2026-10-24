"""Post-score explanation only. Never changes frozen policy or predictions."""
from pathlib import Path
import sys
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nutri_literature.repair_v3.common import HOME, _read, sha, verify_source_freeze, immutable
from nutri_literature.repair_v3.runner import gold, load_papers
from nutri_literature.repair_v3.identity import aligned_ids, choose, ranked
from nutri_literature import evaluation_v6


def main():
    verify_source_freeze()
    if not (HOME/'completion-audit.json').exists():
        raise ValueError('개발 평가 완료 후에만 진단 가능')
    policy = _read(HOME/'selection.json')['model']['identity']
    documents, _ = gold()
    by_id = {d['pmid']: d for d in documents}
    counts, changed, missing, lost = Counter(), Counter(), [], []
    for p in load_papers('development'):
        d = by_id[p['pmid']]
        for m in p['mentions']:
            truth, old = aligned_ids(m, d), set(m['old_ids'])
            chosen = choose(m, policy['model'], policy['threshold'], policy['margin'], policy['enabled'])
            new = chosen['selected_id']
            changed['all_new_links'] += int(not old and new is not None)
            changed['all_existing_id_changes'] += int(bool(old) and new is not None and new not in old)
            if not truth or not old or old & truth:
                continue
            rows = ranked(m, policy['model'])
            menu = {r['native_id'] for r in rows}
            top = rows[0]['native_id']
            stage = ('gold_id_not_retrieved' if not menu & truth else 'ranked_nil' if top is None else
                     'gold_id_not_top' if top not in truth else 'selection_gate')
            counts[m['kind']+':'+stage] += 1
            missing.append({'pmid': p['pmid'], 'mention': {k: m[k] for k in ('kind','text','start','end')},
                'previous_ids': sorted(old), 'annotated_ids': sorted(truth), 'first_loss': stage,
                'selected_id': new, 'top10': [{'native_id': r['native_id'], 'score': r['ranking_score']} for r in rows[:10]],
                'annotation_disagreement_not_expert_adjudication': True})
        baseline = _read(HOME/'predictions/baseline'/('paper-'+p['pmid']+'.json'))
        current = _read(HOME/'predictions/combined'/('paper-'+p['pmid']+'.json'))
        expected, before, _, _ = evaluation_v6.relation_sets(d, baseline)
        _, after, _, _ = evaluation_v6.relation_sets(d, current)
        for k in sorted((expected & before)-after):
            same = [r for r in current['relations'] if tuple(sorted([r['entity1'],r['entity2']])) == k[1]]
            decisions = [r for r in current['decisions'] if tuple(sorted(r['ids'], key=str)) == tuple(sorted(k[1], key=str))]
            lost.append({'key': list(k), 'replacement_types': [r['predicate'] for r in same],
                         'reason': 'type_changed' if same else 'not_accepted', 'decisions': decisions})
    value = {'schema': 'repair-v3-postscore-diagnostics', 'policy_reselected': False,
        'predictions_modified': False, 'old_id_feature_weight': policy['model']['weights'].get('old_id'),
        'existing_id_annotation_disagreement_first_loss': dict(counts), 'id_change_counts': dict(changed),
        'id_records': missing, 'lost_true_positive_records': lost,
        'lost_true_positive_causes': dict(Counter(r['reason'] for r in lost))}
    post_home = HOME.with_name('chatgpt-repair-v3-postscore')
    immutable(post_home/'diagnostics.json', value)
    immutable(post_home/'manifest.json', {
        'core_delivery_manifest_sha256': sha((HOME/'delivery-manifest.json').read_bytes()),
        'analysis_sha256': sha((post_home/'diagnostics.json').read_bytes()),
        'analysis_script_sha256': sha(Path(__file__).read_bytes()),
        'policy_reselected': False, 'additional_inference': 0})
    print(value['existing_id_annotation_disagreement_first_loss'])


if __name__ == '__main__':
    main()
