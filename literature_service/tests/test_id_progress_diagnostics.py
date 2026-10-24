import pytest
from scripts import diagnose_relation_id_v6_gpu as diagnostic


def fixture_values():
    document = {'pmid': 'train-1', 'text': 'β GENE changes.',
                'entities': [{'kind': 'gene', 'start': 2, 'end': 6, 'native_ids': ['1']}]}
    mention = {'kind': 'gene', 'start': 2, 'end': 6, 'text': 'GENE',
               'candidate_ids': ['2', '1'], 'source': 'official-alias'}
    record = {'pmid': 'train-1', 'key': 'm', 'kind': 'gene', 'previous_id': '1',
              'proposed_id': '2', 'selected_id': '1', 'reason': 'retained',
              'strict_correct': False, 'previous_correct': True, 'selected_correct': True,
              'priority': .999, 'margin': 8., 'annotation_scope_complete': True,
              'species_evidence_compatible': True}
    return document, mention, record


def test_retrieval_failure_and_ranking_failure_are_separate():
    d, m, r = fixture_values()
    assert diagnostic.reason(d, m, r) == 'gold_ID_in_menu_but_ranked_below_wrong_ID_or_NIL'
    assert diagnostic.reason(d, {**m, 'candidate_ids': ['2']}, r) == 'gold_ID_absent_from_inference_menu_50'


def test_annotation_absence_is_not_called_biological_error():
    d, m, _ = fixture_values()
    assert diagnostic.alignment(d, {**m, 'start': 3})[0] == 'overlapping_annotation_different_boundary'
    assert diagnostic.alignment(d, {**m, 'start': 7, 'end': 14})[0] == 'no_annotation_at_position_not_a_biological_error_verdict'


def test_annotated_span_without_reference_ID_has_its_own_category():
    d, m, r = fixture_values()
    d['entities'][0]['native_ids'] = []
    assert diagnostic.reason(d, m, r) == 'exact_annotation_without_reference_ID'


def test_disabled_policy_damage_prevention_is_not_counted_as_ID_repair():
    d, m, r = fixture_values()
    result = diagnostic.summarize_automatic([d], [r], {'train-1': {'m': m}})
    group = result['groups']['gene:change']
    assert group['correct_previous_damage_averted'] == 1
    assert group['actual_new_or_changed'] == 0
    assert group['high_priority_099_correct'] == 0


def test_unannotated_kind_is_excluded_without_requiring_gold():
    _, _, r = fixture_values()
    result = diagnostic.summarize_automatic([], [{**r, 'annotation_scope_complete': False}], {})
    assert result['counts']['unannotated_kind_excluded'] == 1
    assert result['groups'] == {}


def test_point_precision_90_is_not_a_wilson_lower_bound_of_90():
    _, _, r = fixture_values()
    rows = [{**r, 'strict_correct': i < 45} for i in range(50)]
    candidate = diagnostic.diagnostic_grid(rows, 'change')['best_supported_lower_bound']
    assert candidate['precision'] == .9 and candidate['wilson_lower'] < .9


def test_source_mismatch_stops_diagnostic_instead_of_fabricating_quote():
    d, m, r = fixture_values()
    with pytest.raises(ValueError, match='saved source'):
        diagnostic.summarize_automatic([d], [r], {'train-1': {'m': {**m, 'text': 'WRONG'}}})
