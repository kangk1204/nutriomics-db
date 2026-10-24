"""Post-hoc native-ID error decomposition; never changes model selection."""
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import chatgpt_tuning as base, evaluation_v6, benchmarks
    from nutri_literature.chatgpt_evaluation import _read, _settings
    from nutri_literature.contract_v7 import immutable, sha

    choice = _read(base.HOME / 'overall-selection.json')
    locator = _read(base.PRIOR / 'gpt-6.1-sol-medium-0fd4d4e0/scoring-locator.json')
    gold, _, gold_hash = benchmarks._load_gold(
        _settings(ROOT / 'runtime/chatgpt-inputs'), locator['gold_path'])
    configurations = {
        'v6': (ROOT / 'runtime/chatgpt-inputs/baseline', 'recall_ensemble'),
        'sol_candidate': (base.directory('gpt-6.1-sol') / 'development/predictions', None),
        'luna_candidate': (base.directory('gpt-6-luna') / 'development/predictions', None),
        'joint': (base.HOME / 'joint-verifier/predictions', None),
    }
    for model in ('gpt-6.1-sol', 'gpt-6-luna'):
        for mode in ('fill_missing', 'revalidate_all'):
            configurations[model + ':' + mode] = (
                base.HOME / 'cached-id-ablations' / model / mode, None)
    all_gold, predicted, errors, types = set(), {}, {}, {}
    for name, (directory, nested) in configurations.items():
        observed = set()
        categories, by_type = Counter(), {}
        for document in gold['documents']:
            path = directory / ('paper-' + document['pmid'] + '.json')
            data = _read(path)
            if nested:
                data = data[nested]
            expected, actual, _, _ = evaluation_v6.relation_sets(document, data)
            all_gold.update(expected)
            observed.update(actual)
            gold_pairs = {r[1] for r in expected}
            known_genes = {i for e in document['entities'] if e['kind'] == 'gene'
                           for i in e.get('native_ids', [])}
            known_chemicals = {i for e in document['entities'] if e['kind'] in {'component', 'nutrient'}
                               for i in e.get('native_ids', [])}
            for relation in actual - expected:
                pair = set(relation[1])
                if relation[1] in gold_pairs:
                    category = 'annotated_pair_wrong_type'
                elif not pair & known_genes and not pair & known_chemicals:
                    category = 'both_endpoint_ids_absent_from_annotation'
                elif not pair & known_genes:
                    category = 'gene_id_absent_from_annotation'
                elif not pair & known_chemicals:
                    category = 'chemical_id_absent_from_annotation'
                else:
                    category = 'annotated_endpoints_unannotated_pair'
                categories[category] += 1
            for relation_type in {r[2] for r in expected | actual}:
                counts = benchmarks._metrics(
                    {r for r in expected if r[2] == relation_type},
                    {r for r in actual if r[2] == relation_type})
                target = by_type.setdefault(relation_type, Counter())
                target.update({k: counts[k] for k in ('tp', 'fp', 'fn')})
        predicted[name] = observed
        errors[name] = dict(categories)
        types[name] = {k: benchmarks._from_counts(v['tp'], v['fp'], v['fn'])
                       for k, v in by_type.items()}
    baseline_correct = predicted['v6'] & all_gold
    transitions = {}
    for name, observed in predicted.items():
        correct = observed & all_gold
        transitions[name] = {
            'retained_v6_true_positives': len(correct & baseline_correct),
            'new_true_positives_beyond_v6': len(correct - baseline_correct),
            'lost_v6_true_positives': len(baseline_correct - correct),
            'counts': benchmarks._metrics(all_gold, observed),
        }
    record = {
        'scope': 'post-hoc development diagnostics only; no policy or model changes',
        'papers': len(gold['documents']), 'gold_relations': len(all_gold),
        'gold_sha256': gold_hash, 'training_selected_family': choice['selected_family'],
        'selection_sha256': sha((base.HOME / 'overall-selection.json').read_bytes()),
        'transitions_vs_v6': transitions, 'false_positive_categories': errors,
        'by_native_relation_type': types,
        'caution': 'Absence from gold annotation is not independent proof of an incorrect ID or biologically false relation.',
        'source_sha256': sha(Path(__file__).read_bytes()),
    }
    immutable(base.HOME / 'posthoc-error-analysis.json', record)
    print(json.dumps({'transitions': transitions, 'false_positive_categories': errors}, ensure_ascii=False))


if __name__ == '__main__':
    main()
