from nutri_literature.supervision_v7 import bag_targets, gold_keys
from nutri_literature.splits_v7 import grouped_folds, audit_overlap
from nutri_literature.contract_v7 import counts, passes


def example():
    c = {'kind': 'component', 'start': 0, 'end': 1, 'text': 'A', 'native_ids': ['D1']}
    g = {'kind': 'gene', 'start': 2, 'end': 3, 'text': 'B', 'native_ids': ['2']}
    return {'pmid': '1', 'text': 'A B', 'entities': [c, g], 'relation_annotation_complete': True,
            'relations': [{'entity1': 'D1', 'entity2': '2', 'predicate': 'Bind', 'direction_annotated': True}]}, c, g


def test_wrong_ids_do_not_destroy_semantic_positive():
    doc, c, g = example()
    out = bag_targets(doc, [{'chemical': {**c, 'normalized_id': 'MESH:D9'}, 'gene': {**g, 'normalized_id': None}}])
    assert out['semantic_labels'] == ['Bind'] and out['strict_native_labels'] == []
    assert out['semantic_supervised'] and out['direction'] == 'undirected'
    assert not out['labels_copied_to_sentences']


def test_incomplete_annotations_are_masked():
    doc, c, g = example()
    doc['relation_annotation_complete'] = False
    assert not bag_targets(doc, [{'chemical': c, 'gene': g}])['semantic_supervised']


def test_conflicting_clusters_masked_but_alternative_ids_retained():
    doc, c, g = example()
    g2 = {**g, 'start': 4, 'end': 5, 'native_ids': ['3']}
    doc['entities'].append(g2)
    r = bag_targets(doc, [{'chemical': c, 'gene': g}, {'chemical': c, 'gene': g2}])
    assert r['identity_cluster_conflict'] and not r['semantic_supervised']
    g['native_ids'] = ['2', '3']
    r = bag_targets(doc, [{'chemical': c, 'gene': g}])
    assert not r['identity_cluster_conflict']


def test_full_relation_denominator_includes_missing_candidates():
    doc, _, _ = example()
    r = counts(gold_keys(doc), set())
    assert r['fn'] == 1 and not passes(r)


def test_duplicate_normalized_text_stays_in_same_fold():
    docs = [{'pmid': '1', 'text': 'Alpha  beta'}, {'pmid': '2', 'text': 'ALPHA beta'}, {'pmid': '3', 'text': 'other'}]
    a = grouped_folds(docs)
    assert a['1'] == a['2']
    kept, excluded = audit_overlap(docs[1:], docs[:1])
    assert [d['pmid'] for d in kept] == ['3'] and excluded[0]['reasons'] == ['normalized_text']
