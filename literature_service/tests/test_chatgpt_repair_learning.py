import pytest

from nutri_literature.chatgpt_repair_learning import fit, priority, counts, objective, train
from nutri_literature import benchmarks


def samples():
    docs, rows, baselines = [], {}, {}
    for i in range(15):
        pmid = str(i+1)
        d = {'pmid': pmid, 'text': 'drug gene paper '+pmid, 'entities': [
             {'kind': 'component', 'native_ids': ['D000001'], 'start': 0, 'end': 4, 'text': 'drug'},
             {'kind': 'gene', 'native_ids': ['1'], 'start': 5, 'end': 9, 'text': 'gene'}],
             'relations': [{'entity1': 'D000001', 'entity2': '1', 'predicate': 'Association'}] if i % 3 else []}
        docs.append(d)
        rows[pmid] = [{'features': {'supported': float(i % 3 != 0), 'type': t},
                       'key': ['D000001', '1', t], 'old_label': t == 'Association', 'old_pair': True,
                       'relation': {'entity1': 'D000001', 'entity2': '1', 'predicate': t}}
                      for t in ('Association', 'Bind', 'Positive_Correlation', 'Negative_Correlation')]
        baselines[pmid] = {'relation': benchmarks._metrics(
            {(pmid, ('1', 'D000001'), 'Association')} if i % 3 else set(),
            {(pmid, ('1', 'D000001'), 'Association')})}
    return docs, rows, baselines


def test_serialized_logistic_priorities_distinguish_type_and_observed_support():
    docs, rows, _ = samples()
    training = [r for d in docs for r in rows[d['pmid']]]
    labels = [int(r['features']['supported'] and r['key'][2] == 'Association') for r in training]
    model = fit(training, labels, 10.)
    assert priority(rows['2'][0], model) > priority(rows['2'][1], model)
    assert priority(rows['2'][0], model) > priority(rows['1'][0], model)


def test_empty_candidate_paper_stays_in_full_gold_denominator():
    docs, rows, _ = samples()
    positive = docs[1]
    measured = counts([positive], {positive['pmid']: []}, {positive['pmid']: []}, .5)
    assert measured[positive['pmid']]['tp'] == 0
    assert measured[positive['pmid']]['fn'] == 1
    assert objective({'precision': 1., 'recall': .1, 'f1': .18}, .36) < objective(
        {'precision': .7, 'recall': .4, 'f1': .5}, .36)


def test_nested_training_holds_out_every_paper_and_never_uses_dev_labels():
    docs, rows, baselines = samples()
    result = train(docs, rows, baselines)
    held = [p for f in result['outer_folds'] for p in f['held_pmids']]
    assert sorted(held) == sorted(d['pmid'] for d in docs)
    assert len(held) == len(set(held))
    assert len(result['grid']) == 60
    assert result['gold_relations'] == 10
    assert result['candidate_recall'] == pytest.approx(1.)
    assert result['dev_labels_used_for_selection'] is False
    assert result['features_include_ids_or_pmid'] is False
