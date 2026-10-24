"""Post-freeze error analysis only; never select a model from development labels."""
from collections import Counter
from pathlib import Path
import csv
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def key_for(pmid, relation):
    return pmid, tuple(sorted((relation['entity1'], relation['entity2']))), relation['predicate']


def main():
    from nutri_literature import benchmarks, evaluation_v6, chatgpt_tuning as old
    from nutri_literature.chatgpt_evaluation import _read, _settings
    from nutri_literature.chatgpt_repair_io import HOME
    from nutri_literature.chatgpt_repair_learning import make_rows, priority, project_rows
    from nutri_literature.chatgpt_repair_runner import inputs_for
    from nutri_literature.contract_v7 import canonical, immutable, sha

    if not (HOME / 'completion-audit.json').exists():
        raise ValueError('Complete the frozen evaluation and offline audit before post-hoc analysis')
    report, selected = _read(HOME / 'comparison.json'), _read(HOME / 'selection.json')
    if report['selection_sha256'] != sha((HOME / 'selection.json').read_bytes()):
        raise ValueError('The frozen selection changed')
    locator = _read(old.PRIOR / 'gpt-6.1-sol-medium-0fd4d4e0/scoring-locator.json')
    gold, _, gold_sha = benchmarks._load_gold(_settings(ROOT / 'runtime/chatgpt-inputs'), locator['gold_path'])
    if gold_sha != report['gold_sha256']:
        raise ValueError('The native gold changed')
    wanted_all, actuals = set(), {name: set() for name in report['arms']}
    wrong, transitions, candidate_decisions = [], [], []
    decisions = Counter()
    for doc in gold['documents']:
        pmid = doc['pmid']
        predictions = {
            name: _read((old.HOME / 'joint-verifier/predictions' if name == 'baseline'
                         else HOME / 'predictions' / name) / ('paper-' + pmid + '.json'))
            for name in report['arms']
        }
        wanted, baseline, _, _ = evaluation_v6.relation_sets(doc, predictions['baseline'])
        wanted_all.update(wanted)
        per = {}
        for name, prediction in predictions.items():
            expected, actual, _, _ = evaluation_v6.relation_sets(doc, prediction)
            if expected != wanted:
                raise ValueError('Native scorer denominator differs across arms')
            actuals[name].update(actual)
            per[name] = actual
            chemical_ids = {identifier for e in doc['entities'] if e['kind'] == 'component'
                            for identifier in e.get('native_ids', [])}
            gene_ids = {identifier for e in doc['entities'] if e['kind'] == 'gene'
                        for identifier in e.get('native_ids', [])}
            for key in sorted(actual - wanted):
                relation = next(r for r in prediction['relations'] if key_for(pmid, r) == key)
                types = sorted(k[2] for k in wanted if k[1] == key[1])
                category = ('annotated_pair_wrong_type' if types
                            else 'chemical_id_absent_from_annotation' if relation['entity1'] not in chemical_ids
                            else 'gene_id_absent_from_annotation' if relation['entity2'] not in gene_ids
                            else 'annotated_endpoints_unannotated_pair')
                wrong.append({'arm': name, 'pmid': pmid, 'key': key, 'annotation_comparison': category,
                              'expected_types_same_pair': types, 'relation': relation,
                              'source_sha256': sha(doc['text']), 'expert_adjudication': False})
        combined = per['combined']
        for key in sorted(baseline & wanted):
            transitions.append({'kind': 'previous_true_positive', 'key': key,
                                'status': 'retained' if key in combined else 'lost',
                                'new_types_same_pair': sorted(k[2] for k in combined if k[1] == key[1])})
        for key in sorted((combined & wanted) - baseline):
            transitions.append({'kind': 'new_true_positive', 'key': key, 'status': 'restored_or_added'})
        for key in sorted(baseline - wanted):
            corrected = sorted(k for k in combined & wanted if k[1] == key[1])
            status = ('persists' if key in combined
                      else 'same_pair_correct_type_present' if corrected else 'removed_or_identity_changed')
            transitions.append({'kind': 'previous_false_positive', 'key': key, 'status': status,
                                'new_correct_types_same_pair': corrected,
                                'interpretation': 'Endpoint/type set transition; not proof of biological cause'})
        # Reconstruct every accepted/withheld pair with citations, without fitting or choosing a cutoff.
        payload, parsed, prior = inputs_for(doc, 'development')
        rows = make_rows(payload, parsed, prior)
        scores = [priority(row, selected['model']) for row in rows]
        projection = project_rows(rows, scores, selected['selected']['threshold'])
        if canonical(projection) != canonical(predictions['combined']):
            raise ValueError('Reconstructed decisions differ from frozen predictions')
        indexed = {tuple(row['key']): row for row in rows}
        old_by_span = {}
        for relation in prior['relations']:
            span = tuple((m['start'], m['end']) for m in relation.get('endpoints', []))
            old_by_span.setdefault(span, []).append(relation)
        for decision in projection['decisions']:
            row = indexed[tuple(decision['key'])]
            relation = row['relation']
            span = tuple((m['start'], m['end']) for m in relation.get('endpoints', []))
            previous = old_by_span.get(span, [])
            if not decision['accepted']:
                action = 'withhold'
            elif key_for(pmid, relation) in baseline:
                action = 'keep'
            elif previous:
                same_ids = any(tuple(sorted((r['entity1'], r['entity2'])))
                               == tuple(sorted((relation['entity1'], relation['entity2']))) for r in previous)
                action = 'type_change' if same_ids else 'identity_or_identity_and_type_change'
            else:
                action = 'add'
            decisions[action] += 1
            candidate_decisions.append({'pmid': pmid, 'action': action, 'decision': decision,
                                        'relation_with_source': relation,
                                        'source_sha256': sha(doc['text']),
                                        'human_approved': False})
    if len(wanted_all) != 154:
        raise ValueError('The complete154-relation denominator is required')
    per_type, error_categories = {}, {}
    for name, actual in actuals.items():
        measured = benchmarks._metrics(wanted_all, actual)
        for field in ('tp', 'fp', 'fn', 'precision', 'recall', 'f1'):
            if measured[field] != report['arms'][name]['metrics'][field]:
                raise ValueError('Post-hoc scores disagree with frozen native evaluation')
        labels = sorted({k[2] for k in wanted_all | actual})
        per_type[name] = {
            label: benchmarks._metrics({k for k in wanted_all if k[2] == label},
                                       {k for k in actual if k[2] == label}) for label in labels
        }
        if sum(m['tp'] for m in per_type[name].values()) != measured['tp']:
            raise ValueError('Type counts must sum to the native micro score')
        error_categories[name] = dict(Counter(r['annotation_comparison'] for r in wrong if r['arm'] == name))
    result = {'schema': 'repair-v2-posthoc-native-diagnostics', 'papers': 100, 'gold_relations': 154,
              'per_type': per_type, 'error_categories': error_categories,
              'transitions': transitions, 'false_positive_details': wrong,
              'actions_by_source_span': dict(decisions), 'candidate_decisions': candidate_decisions,
              'policy_or_threshold_reselected': False, 'training_labels_written': False,
              'scope': 'repeated exposed development; annotation comparison, not expert biological adjudication',
              'selection_sha256': report['selection_sha256'], 'gold_sha256': gold_sha,
              'comparison_sha256': sha((HOME / 'comparison.json').read_bytes()),
              'source_sha256': sha(Path(__file__).read_bytes())}
    folder = HOME / 'analysis'
    immutable(folder / 'outcomes.json', result)
    lines = ['# 교정 후 유형별 오류·정탐 추적', '',
             '동결한 동일100편 평가를 사후 분해했다. 이 분석으로 모델·임계값을 다시 선택하지 않았다. '
             '주석 부재와 생물학적 오류는 동일하지 않으며 전문가 판정은 수행하지 않았다.', '',
             '| 구성 | 관계 유형 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |',
             '|---|---|---:|---:|---:|---:|---:|---:|']
    csv_rows = []
    for name, labels in per_type.items():
        for label, metric in labels.items():
            cells = [name, label, *[metric[k] for k in ('tp', 'fp', 'fn')],
                     *['미정의' if metric[k] is None else f'{metric[k]*100:.2f}%'
                       for k in ('precision', 'recall', 'f1')]]
            lines.append('| ' + ' | '.join(map(str, cells)) + ' |')
            csv_rows.append({'arm': name, 'type': label, **metric})
    lines.extend(['', '원문·보류 사유·기존 정탐56건의 건별 유지/상실과 신규 정탐은 `outcomes.json`에 보존했다. '
                  'ID·유형 변경 행동은 같은 원문 언급 위치를 기준으로 비교하며, 생물학적 원인 판정은 아니다.', ''])
    (folder / 'outcomes.md').write_text('\n'.join(lines), encoding='utf-8')
    with (folder / 'outcomes.csv').open('w', encoding='utf-8-sig', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    print(json.dumps({'status': 'complete', 'per_type': per_type, 'actions': dict(decisions)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
