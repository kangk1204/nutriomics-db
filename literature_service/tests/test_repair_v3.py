from copy import deepcopy

import pytest

from nutri_literature.repair_v3.identity import aligned_ids, choose, train_ranker
from nutri_literature.repair_v3.events import supported_types, aggregate, semantic_truth, project
from nutri_literature.repair_v3.learning import fit_binary, objective, type_scores
from nutri_literature.repair_v3.common import FLOOR


def event(**fields):
    return {'key': 'e0', 'chemical': 'c', 'gene': 'g', 'origin': 'gpt-6-luna',
            'native_type': None, 'decision': 'supported', 'measurement': 'unspecified',
            'change': 'unspecified', 'observation': 'observed', 'negated': False,
            'speculative': False, 'non_significant': False, 'role_direction': 'unknown',
            'condition': None, 'entity_roles': {}, 'evidence': [
                {'start': 0, 'end': 31, 'text': 'Drug increased Gene expression.'}], **fields}


def mention(**fields):
    return {'kind': 'gene', 'text': 'Gene', 'start': 15, 'end': 19,
            'old_ids': ['1'], 'candidates': [
                {'native_id': '1', 'features': {'exact_alias': 0.}},
                {'native_id': '2', 'features': {'exact_alias': 1.}}], **fields}


def test_unknown_type_never_becomes_association():
    assert supported_types(event()) == []


def test_signed_expression_and_modification_keep_separate_witnesses():
    assert supported_types(event(measurement='expression', change='increase')) == ['Positive_Correlation']
    e = event(measurement='ubiquitination', change='increase', evidence=[
        {'start': 0, 'end': 22, 'text': 'Drug ubiquitinated Gene'}])
    assert supported_types(e) == ['Association']


def test_negated_increase_not_invented_as_positive():
    assert supported_types(event(measurement='expression', change='increase', negated=True)) == []


def test_existing_same_type_does_not_generate_other_types():
    assert supported_types(event(origin='prior_same_type', native_type='Bind')) == ['Bind']


def test_aggregation_is_permutation_invariant_and_does_not_cross_conditions():
    a = event(measurement='expression', change='decrease', condition='week 1')
    b = event(key='e1', measurement='ubiquitination', change='increase', condition='week 2')
    assert aggregate([a, b], 'Association') == aggregate([b, a], 'Association')
    f = aggregate([a, b])
    assert 'count:measurement_change=expression|increase' not in f
    assert f['count:measurement_change=expression|decrease'] == 1


def test_semantic_positive_survives_wrong_predicted_ids():
    document = {'pmid': 'x', 'text': 'Drug Gene', 'entities': [
        {'kind': 'component', 'start': 0, 'end': 4, 'native_ids': ['D1']},
        {'kind': 'gene', 'start': 5, 'end': 9, 'native_ids': ['1']}],
        'relations': [{'entity1': 'D1', 'entity2': '1', 'predicate': 'Bind'}]}
    bag = {'ids': ['WRONG', '99'], 'chemical_mentions': [
        {'kind': 'chemical', 'start': 0, 'end': 4}], 'gene_mentions': [
        {'kind': 'gene', 'start': 5, 'end': 9}]}
    assert semantic_truth(bag, document) == ['Bind']
    bag['gene_mentions'][0]['start'] = 6
    bag['gene_mentions'][0]['end'] = 7
    assert semantic_truth(bag, document) is None


def test_alignment_is_unique_and_preserves_unicode_positions():
    doc = {'entities': [{'kind': 'gene', 'start': 3, 'end': 7, 'native_ids': ['1']}]}
    assert aligned_ids(mention(start=3, end=7), doc) == {'1'}
    assert aligned_ids(mention(start=2, end=7), doc) == {'1'}
    assert aligned_ids(mention(start=2, end=6), doc) == set()


def test_precision_gate_protects_old_id_without_relation_score():
    m = mention()
    model = {'weights': {'exact_alias': 10., 'nil': -5.}}
    assert choose(m, model, .5, 0., enabled=False)['selected_id'] == '1'
    assert choose(m, model, .5, 0., enabled=True)['selected_id'] == '2'
    assert choose(m, model, .99, 20., enabled=True)['selected_id'] == '1'


def test_id_ranker_skips_unannotated_mentions():
    model = train_ranker([{'mentions': [mention()], 'document': {'entities': []}}], 1.)
    assert model['training_pairs'] == 0


def test_recall_floor_is_exact_fraction_not_rounded_percentage():
    assert FLOOR == 64/154
    assert objective({'precision': .99, 'recall': 63/154, 'f1': .6}, FLOOR)[0] == 0
    assert objective({'precision': .85, 'recall': 64/154, 'f1': .6}, FLOOR)[0] == 2


def test_projection_cannot_accept_type_without_witness():
    negative = {'intercept': 10., 'weights': {}}
    bag = {'key': ['chemical:drug', 'gene:gene'], 'ids': ['D1', '1'],
           'features': {}, 'types': {}, 'witnesses': {}, 'unresolved': False}
    result = project({'mentions': []}, [bag], negative, {}, 0.)
    assert result['relations'] == []
    assert result['decisions'][0]['reason'] == 'type_unresolved'


def test_fit_binary_empty_and_one_class_are_total():
    assert fit_binary([], [], 1.)['intercept'] < 0
    assert fit_binary([{}], [1], 1.)['intercept'] > 0


def test_single_type_hypothesis_does_not_become_certainty():
    model = {'Association': {'intercept': -4., 'weights': {}}}
    score = type_scores(model, {'types': {'Association': {}}})['Association']
    assert 0 < score < .05


def test_optional_review_payload_strips_prior_labels_and_id_scores():
    from nutri_literature.repair_v3.review import payload
    m = mention(key='m0')
    m['candidates'][0].update(taxon=9606, names=['Gene'], priority=.99)
    p = {'document': {'text': 'Drug Gene.'}, 'source_sha256': 'source', 'mentions': [m]}
    supplied = payload(p)
    assert 'old_ids' not in supplied['mentions'][0]
    assert 'features' not in supplied['mentions'][0]['candidates'][0]
    assert 'priority' not in supplied['mentions'][0]['candidates'][0]
    assert supplied['gold_annotations_provided'] is False


def test_multiple_id_quotes_preserve_separate_source_spans():
    from nutri_literature.repair_v3.review import add_id_evidence
    m = mention(key='m0')
    source = 'Human Gene. Drug altered expression.'
    reviews = {'sol': {'raw': {'mentions': [{'key': 'm0', 'selected_id': '2',
                                            'id_evidence': ['Human Gene', 'expression']}]}}}
    result = add_id_evidence([m], {}, reviews, source)
    assert result[0]['evidence_valid']
    assert len(result[0]['evidence_spans']) == 2
    assert m['candidates'][1]['features']['independent_quoted_id_support'] == 1
    reviews['sol']['raw']['mentions'][0]['id_evidence'] = ['Human Gene; expression']
    assert not add_id_evidence([m], {}, reviews, source)[0]['evidence_valid']


def test_shared_attempt_budget_counts_retries_in_original_global_limit(tmp_path):
    from nutri_literature.repair_v3.review import SharedBudget
    from nutri_literature.chatgpt_repair_io import BudgetExceeded
    budget = SharedBudget(tmp_path/'attempts.sqlite3', limit=3)
    first = budget.reserve('a', 'repair-v3-train', 'sol')
    budget.finish(first, error=ValueError())
    budget.reserve('a', 'repair-v3-train', 'sol')
    budget.reserve('b', 'repair-v3-development', 'luna')
    with pytest.raises(BudgetExceeded):
        budget.reserve('c', 'repair-v3-development', 'luna')
    with budget.connect() as db:
        phases = dict(db.execute('SELECT phase,count(*) FROM attempts GROUP BY phase'))
    assert phases == {'repair-v3-train': 1, 'repair-v3-retry': 1, 'repair-v3-development': 1}


def test_training_review_cap_cannot_use_development_reserve(tmp_path):
    from nutri_literature.repair_v3.review import SharedBudget
    from nutri_literature.chatgpt_repair_io import BudgetExceeded
    budget = SharedBudget(tmp_path/'attempts.sqlite3')
    with budget.connect() as db:
        db.executemany('INSERT INTO attempts(request_hash,phase,model,state,started) VALUES(?,?,?,?,?)',
                       [(str(i), 'repair-v3-train', 'sol', 'completed', 't') for i in range(400)])
    with pytest.raises(BudgetExceeded):
        budget.reserve('new', 'repair-v3-train', 'sol')
    assert budget.reserve('dev', 'repair-v3-development', 'sol')
