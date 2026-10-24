"""Inspect all56 previous errors; this diagnostic is never a model input."""
from collections import Counter
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import evaluation_v6, benchmarks, chatgpt_tuning as old
    from nutri_literature.chatgpt_repair_io import HOME
    from nutri_literature.chatgpt_evaluation import _read, _settings
    from nutri_literature.chatgpt_repair_semantics import context_features
    from nutri_literature.contract_v7 import immutable, sha
    metadata = _read(HOME / 'knowledge/mesh-metadata.json')
    histories = _read(HOME / 'knowledge/mesh-history.json')['records']
    historical_pairs = {(r['old_id'], r.get('replacement_id')) for r in histories if r.get('replacement_id')}
    locator = _read(old.PRIOR / 'gpt-6.1-sol-medium-0fd4d4e0/scoring-locator.json')
    gold, _, gold_sha = benchmarks._load_gold(_settings(ROOT / 'runtime/chatgpt-inputs'), locator['gold_path'])
    rows, tally = [], Counter()
    for d in gold['documents']:
        predicted = _read(old.HOME / 'joint-verifier/predictions' / ('paper-'+d['pmid']+'.json'))
        wanted, actual, _, _ = evaluation_v6.relation_sets(d, predicted)
        pair_gold = {r[1] for r in wanted}
        gene_ids = {i for e in d['entities'] if e['kind'] == 'gene' for i in e.get('native_ids', [])}
        chemical_ids = {i for e in d['entities'] if e['kind'] == 'component' for i in e.get('native_ids', [])}
        for key in sorted(actual-wanted):
            r = next(r for r in predicted['relations'] if tuple(sorted((r['entity1'], r['entity2']))) == key[1] and r['predicate'] == key[2])
            c, g = r['endpoints']
            if key[1] in pair_gold:
                category = 'annotated_pair_wrong_type'
            elif r['entity1'] not in chemical_ids:
                category = 'chemical_id_absent_from_annotation'
            elif r['entity2'] not in gene_ids:
                category = 'gene_id_absent_from_annotation'
            else:
                category = 'annotated_endpoints_unannotated_pair'
            quote = ' '.join(s['text'] for s in r['evidence'])
            expected_types = sorted(k[2] for k in wanted if k[1] == key[1])
            span_ids = []
            for m, kind in ((c, 'component'), (g, 'gene')):
                span_ids.append(sorted({i for e in d['entities'] if e['kind'] == kind and e['start'] < m['end'] and m['start'] < e['end']
                                        for i in e.get('native_ids', [])}))
            hypotheses = []
            flags = context_features(c, d['text'])
            if flags['sequence_context'] and re.search(r'methionine|valine|cytosine|thymidine|nucleotide|residue', c['text'], re.I):
                hypotheses.append('sequence_component_treated_as_chemical_entity')
            meta = metadata.get(r['entity1'], {})
            if any(t.startswith('D12.') for t in meta.get('trees') or []) and not flags['administration_context']:
                hypotheses.append('gene_product_or_complex_role_confusion')
            if category == 'annotated_pair_wrong_type':
                if re.search(r'phosphorylat|ubiquitinat|acetylat', quote, re.I):
                    hypotheses.append('modification_confused_with_expression')
                elif re.search(r'bind|receptor', quote, re.I) and 'Bind' in expected_types:
                    hypotheses.append('binding_loss_confused_with_signed_expression')
                elif re.search(r'knockout|deficien|mutan|colocaliz|positive.*cells|\(\+\)', quote, re.I):
                    hypotheses.append('genotype_marker_or_cell_count_confused_with_expression')
                else:
                    hypotheses.append('native_type_requires_full_document_and_role_review')
            if any((i, r['entity1']) in historical_pairs or (r['entity1'], i) in historical_pairs for i in span_ids[0]):
                hypotheses.append('official_identifier_replacement_requires_identity_audit')
            if category == 'gene_id_absent_from_annotation':
                hypotheses.append('species_or_cell_line_assignment_or_annotation_ambiguity')
            if re.search(r'purpose of this study|objective.*evaluat|we measured', quote, re.I):
                hypotheses.append('assay_or_planning_statement_requires_observation_check')
            if not hypotheses:
                hypotheses.append('unresolved_entity_eligibility_relation_or_annotation_scope')
            tally[category] += 1
            rows.append({'pmid': d['pmid'], 'key': key, 'annotation_comparison': category,
                         'chemical': {**c, 'selected_id': r['entity1'], 'official_metadata': meta},
                         'gene': {**g, 'selected_id': r['entity2']}, 'source_sha256': sha(d['text']),
                         'evidence': r['evidence'], 'predicted_native_type': r['predicate'],
                         'expected_native_types_same_pair': expected_types, 'annotated_ids_at_overlapping_spans': span_ids,
                         'correct_type_already_present': any(k in actual for k in wanted if k[1] == key[1]),
                         'root_cause_hypotheses': hypotheses, 'root_cause_status': 'source-backed hypothesis; no independent expert adjudication',
                         'not_annotated_does_not_mean_biologically_false': True})
    if len(rows) != 56 or dict(tally) != {'annotated_pair_wrong_type': 20, 'chemical_id_absent_from_annotation': 22,
                                         'gene_id_absent_from_annotation': 2, 'annotated_endpoints_unannotated_pair': 12}:
        raise ValueError('Frozen56-error surface changed')
    result = {'errors': rows, 'total': len(rows), 'categories': dict(tally), 'gold_sha256': gold_sha,
              'source_sha256': sha(Path(__file__).read_bytes()), 'used_as_training_labels': False,
              'confirmed_code_findings': [
                {'finding': 'Prior prompt grouped expression/activity/sensitivity into broad signed labels',
                 'source_sha256': sha((ROOT / 'nutri_literature/chatgpt_joint_verifier.py').read_bytes())},
                {'finding': 'Prior verifier had no explicit measured-quantity or chemical-mention-role output fields'}],
              'diagnostic_only': True}
    immutable(HOME / 'analysis/previous56-error-ledger.json', result)
    print({'errors': len(rows), 'categories': dict(tally), 'hypotheses_are_expert_gold': False})


if __name__ == '__main__':
    main()
