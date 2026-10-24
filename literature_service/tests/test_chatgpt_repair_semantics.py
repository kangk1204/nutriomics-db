import copy
import pytest

from nutri_literature.chatgpt_repair_semantics import parse, native_identity, context_features
from nutri_literature.chatgpt_repair_learning import make_rows, priority, project_rows, changed_identity_allowed
from nutri_literature.chatgpt_repair_knowledge import parse_promotions
from nutri_literature.contract_v7 import canonical


def fixture():
    text = 'Human aspirin decreases TP53 expression, while TP53 phosphorylation increases.'
    mentions = []
    for kind, value, identifier, taxon in [('chemical', 'aspirin', 'D001241', None), ('gene', 'TP53', '7157', 9606)]:
        start = text.index(value)
        mentions.append({'key': 'v'+str(len(mentions)), 'kind': kind, 'text': value, 'start': start, 'end': start+len(value),
                         'candidates': [{'native_id': identifier, 'taxon': taxon, 'names': [value]}]})
    payload = {'source': text, 'mentions': mentions, 'sentences': [{'key': 's0', 'start': 0, 'end': len(text), 'text': text}]}
    raw = {'schema_version': 'repair-evidence-v2', 'mentions': [
           {'key': m['key'], 'selected_id': m['candidates'][0]['native_id'], 'eligible': True,
            'role': 'administered_compound' if m['kind'] == 'chemical' else 'gene_product',
            'id_basis': 'alias', 'id_evidence': m['text']} for m in mentions],
           'events': []}
    for measurement, change, label in [('expression', 'decrease', 'Negative_Correlation'), ('phosphorylation', 'increase', 'Association')]:
        raw['events'].append({'chemical': 'v0', 'gene': 'v1', 'measurement': measurement, 'change': change,
             'benchmark_relation_type': label, 'role_direction': 'chemical_to_gene', 'decision': 'supported',
             'confidence': 'high', 'evidence': ['s0'], 'negated': False, 'speculative': False,
             'non_significant': False, 'observation': 'observed', 'condition': None})
    return payload, raw


def test_distinct_modification_and_expression_observations_are_preserved():
    payload, raw = fixture()
    parsed = parse(payload, canonical(raw))
    assert [(e['measurement'], e['change']) for e in parsed['events']] == [('expression', 'decrease'), ('phosphorylation', 'increase')]
    rows = make_rows(payload, {'gpt-6.1-sol': parsed}, {'relations': []})
    assert len(rows) == 4
    model = {'intercept': -10., 'weights': {'native_type=Negative_Correlation': 20.}}
    result = project_rows(rows, [priority(r, model) for r in rows], .5)
    assert [r['predicate'] for r in result['relations']] == ['Negative_Correlation']
    assert len(result['relations'][0]['scientific_events']) == 2
    assert '7157' not in canonical([r['features'] for r in rows])


@pytest.mark.parametrize('field,value', [('evidence', ['s99']), ('chemical', {}), ('negated', 'false'),
     ('measurement', 'invented'), ('confidence', 'certain'), ('confidence', []), ('decision', {}),
     ('observation', 'completed'), ('observation', {}), ('role_direction', 'left')])
def test_malformed_or_unaligned_events_cannot_become_grounded_relations(field, value):
    payload, raw = fixture()
    raw['events'] = [dict(raw['events'][0], **{field: value})]
    assert not parse(payload, canonical(raw))['events']


def test_invalid_ids_are_not_filled_or_selected_from_gold():
    payload, raw = fixture()
    raw['mentions'][1]['selected_id'] = 'not-a-menu-id'
    parsed = parse(payload, canonical(raw))
    assert parsed['events'] and not parsed['mentions']['v1']['id_valid']
    assert not make_rows(payload, {'gpt-6.1-sol': parsed}, {'relations': []})


def test_species_change_requires_actual_source_contradiction():
    payload, raw = fixture()
    payload['mentions'][1]['candidates'].append({'native_id': '22059', 'taxon': 10090, 'names': ['Trp53']})
    raw['mentions'][1]['id_basis'] = 'species'
    raw['mentions'][1]['id_evidence'] = payload['source']
    node = parse(payload, canonical(raw))['mentions']['v1']
    assert changed_identity_allowed(node, '22059', payload['source'])
    node['id_evidence'] = 'TP53'
    assert not changed_identity_allowed(node, '22059', payload['source'])
    node['id_basis'] = 'alias'
    assert not changed_identity_allowed(node, '22059', payload['source'])


def test_matching_label_does_not_prioritize_an_unsupported_witness():
    payload, raw = fixture()
    raw['events'][1]['decision'] = 'unsupported'
    rows = make_rows(payload, {'gpt-6.1-sol': parse(payload, canonical(raw))}, {'relations': []})
    association = next(r for r in rows if r['key'][2] == 'Association')
    assert association['relation']['reviewer_decision'] == 'supported'
    assert association['relation']['scientific_events'][0]['measurement'] == 'expression'


def test_duplicate_mention_resolutions_do_not_silently_choose_first():
    payload, raw = fixture()
    raw['mentions'].append(copy.deepcopy(raw['mentions'][1]))
    assert not parse(payload, canonical(raw))['events']


def test_residue_context_is_evidence_not_a_blanket_amino_acid_ban():
    source = 'Methionine supplementation lowered blood pressure.'
    m = {'start': 0, 'end': 10, 'text': 'Methionine'}
    assert not context_features(m, source)['sequence_context']
    assert context_features(m, 'Methionine to valine substitution at codon 404.')['sequence_context']


def test_historical_replacement_is_not_chemical_identity():
    parsed = parse_promotions('C476217    cinacalcet    Cinacalcet Hydrochloride\ninvalid record')
    assert len(parsed) == 1 and not parsed[0]['chemical_identity_proven']
    history = {'12': {'current_id': '13', 'identity_proven': True},
               '13': {'current_id': '14', 'identity_proven': True}}
    assert native_identity('12', history) == '14'
    assert native_identity('D001241', history) == 'D001241'


def test_unicode_evidence_positions_must_match_original_source():
    payload, raw = fixture()
    payload['sentences'][0]['text'] = payload['sentences'][0]['text'].replace('aspirin', 'αspirin')
    assert not parse(payload, canonical(raw))['events']
