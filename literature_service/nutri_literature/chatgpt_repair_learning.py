"""Paper-nested CPU correction and constrained full-denominator selection."""
from collections import defaultdict
import math

from . import chatgpt_tuning as old, evaluation_v6, benchmarks
from .chatgpt_evaluation import _metric
from .chatgpt_repair_semantics import evidence_features
from .chatgpt_grounding import species, definitions, key
from .splits_v7 import grouped_folds

CS = (.1, 1., 10.)
THRESHOLDS = tuple(i/100 for i in range(5, 100, 5)) + (.99,)
RECALL_FLOOR = 56/154


def slim(m):
    return {k: m[k] for k in ('kind', 'text', 'start', 'end')}


def pair_key(relation):
    return relation['entity1'], relation['entity2']


def row_key(relation):
    return (*pair_key(relation), relation['predicate'])


def source_key(c, g):
    return c['start'], c['end'], g['start'], g['end']


def changed_identity_allowed(node, original, source):
    if not node['id_valid'] or not node['id_quote_valid'] or node.get('eligible') is False:
        return False
    if node['selected_id'] == original:
        return True
    mention, selected = node['source_mention'], node['selected_record']
    previous = next((c for c in mention['candidates'] if c['native_id'] == original), None)
    if previous is None:
        return False
    basis = node.get('id_basis')
    if basis == 'species' and mention['kind'] == 'gene':
        quote = node['id_evidence']
        if key(mention['text']) not in key(quote):
            return False
        taxa = species(quote)
        return len(taxa) == 1 and selected.get('taxon') in taxa and previous.get('taxon') not in taxa
    if basis == 'official_history' and mention['kind'] == 'gene':
        return ((selected.get('official_same_gene_merge') and selected.get('current_id') == original)
                or (previous.get('gene_history', {}).get('identity_proven')
                    and previous['gene_history']['current_id'] == node['selected_id']))
    previous_names, names = {key(n) for n in previous['names']}, {key(n) for n in selected['names']}
    if basis == 'abbreviation':
        expansions = {key(r['long']) for r in definitions(source).get(mention['text'], [])}
        return bool(expansions & names) and not bool(expansions & previous_names)
    if basis == 'long_name':
        phrase = key(node['id_evidence'])
        return len(phrase.split()) > 1 and phrase in names and phrase not in previous_names
    return False


def make_rows(payload, parsed_models, baseline):
    """Generate four type alternatives per grounded ID pair; no annotations used."""
    grouped = defaultdict(list)
    selected = defaultdict(set)
    originals_by_span = defaultdict(list)
    for r in baseline['relations']:
        selected[pair_key(r)].add(r['predicate'])
        if isinstance(r.get('endpoints'), list) and len(r['endpoints']) == 2:
            c, g = r['endpoints']
            originals_by_span[source_key(c, g)].append(r)
    for model, parsed in parsed_models.items():
        for e in parsed['events']:
            c, g = e['chemical_mention'], e['gene_mention']
            if not c['id_valid'] or not g['id_valid']:
                continue
            cm, gm = c['source_mention'], g['source_mention']
            original = originals_by_span.get(source_key(cm, gm), [])
            cids, gids = {c['selected_id']}, {g['selected_id']}
            if original:
                old_c, old_g = {r['entity1'] for r in original}, {r['entity2'] for r in original}
                if c['selected_id'] not in old_c and not all(changed_identity_allowed(c, i, payload['source']) for i in old_c):
                    cids = old_c
                if g['selected_id'] not in old_g and not all(changed_identity_allowed(g, i, payload['source']) for i in old_g):
                    gids = old_g
            for cid in cids:
                for gid in gids:
                    grouped[cid, gid].append({**e, 'model': model, 'selected_chemical_id': cid, 'selected_gene_id': gid})
    rows = []
    for pair in sorted(set(grouped) | set(selected)):
        events = grouped[pair]
        originals = [r for r in baseline['relations'] if pair_key(r) == pair]
        for native_type in old.TYPES:
            features = evidence_features(payload, events, native_type, native_type in selected[pair])
            features['old_pair_selected'] = float(bool(originals))
            features['both_models_matching_type'] = float(
                len({e['model'] for e in events if e['benchmark_relation_type'] == native_type and e['decision'] == 'supported'}) == 2)
            if events:
                primary = max(events, key=lambda e: (e['decision'] == 'supported',
                                  e['benchmark_relation_type'] == native_type, e.get('confidence') == 'high',
                                  e['model'] == 'gpt-6.1-sol'))
                c, g = primary['chemical_mention']['source_mention'], primary['gene_mention']['source_mention']
                evidence = primary['evidence_spans']
                relation = {'entity1': pair[0], 'entity2': pair[1], 'predicate': native_type,
                            'endpoints': [slim(c), slim(g)], 'evidence': evidence,
                            'role_direction': primary.get('role_direction'), 'negated': primary['negated'],
                            'reviewer_decision': primary['decision'],
                            'speculative': primary['speculative'], 'non_significant': primary['non_significant'],
                            'observation': primary.get('observation'), 'review_status': 'unreviewed',
                            'identity_validation': [
                                {'kind': kind, 'selected_id': pair[j], 'model_selected_id': node['selected_id'],
                                 'preserved_existing_id': pair[j] != node['selected_id'],
                                 'id_basis': node.get('id_basis'), 'id_evidence': node.get('id_evidence'),
                                 'role': node.get('role'), 'eligible': node.get('eligible')}
                                for j, (kind, node) in enumerate((('chemical', primary['chemical_mention']),
                                                                 ('gene', primary['gene_mention'])))],
                            'scientific_events': [{k: e.get(k) for k in ('measurement', 'change', 'condition',
                              'negated', 'speculative', 'non_significant', 'observation', 'role_direction', 'model')}
                              for e in events]}
                if primary.get('role_direction') in {'chemical_to_gene', 'gene_to_chemical'}:
                    relation['subject'] = pair[int(primary['role_direction'] == 'gene_to_chemical')]
            else:
                relation = {**originals[0], 'predicate': native_type, 'scientific_events': []}
            # ID numbers and PMID stay in metadata, never in the feature dictionary.
            rows.append({'features': features, 'relation': relation, 'old_pair': bool(originals),
                         'old_label': native_type in selected[pair], 'key': [*pair, native_type]})
    return rows


def fit(rows, labels, c):
    import numpy as np
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    if not rows:
        return {'intercept': -30., 'weights': {}}
    y = np.asarray(labels, dtype=int)
    if len(set(y)) < 2:
        return {'intercept': 30. if y[0] else -30., 'weights': {}}
    vectorizer = DictVectorizer()
    matrix = vectorizer.fit_transform([r['features'] for r in rows])
    model = LogisticRegression(C=c, max_iter=1500, class_weight='balanced', random_state=20261017)
    model.fit(matrix, y)
    return {'intercept': float(model.intercept_[0]),
            'weights': dict(zip(vectorizer.get_feature_names_out(), map(float, model.coef_[0])))}


def feature_items(features):
    for k, v in features.items():
        yield ((k+'='+v, 1.) if isinstance(v, str) else (k, float(v)))


def priority(row, model):
    z = model['intercept'] + sum(model['weights'].get(k, 0.)*v for k, v in feature_items(row['features']))
    return 1/(1+math.exp(-max(-60., min(60., z))))


def project_rows(rows, scores, threshold):
    """One primary document label per ID pair; distinct scientific events retained."""
    best = {}
    for row, value in zip(rows, scores):
        pair = tuple(row['key'][:2])
        criterion = value, row['old_label'], row['key'][2]
        if pair not in best or criterion > best[pair][0]:
            best[pair] = (criterion, row)
    relations, decisions = [], []
    for criterion, row in best.values():
        keep = criterion[0] >= threshold
        decisions.append({'key': row['key'], 'accepted': keep, 'priority': criterion[0],
                          'reason': 'training_selected_cutoff' if keep else 'below_training_selected_cutoff'})
        if keep:
            relations.append({**row['relation'], 'priority': criterion[0], 'priority_is_accuracy_probability': False})
    return {'relations': relations, 'decisions': decisions, 'abstained_pairs': sum(not r['accepted'] for r in decisions)}


def counts(docs, rows_by_paper, scores_by_paper, threshold):
    result = {}
    for d in docs:
        expected, actual, _, _ = evaluation_v6.relation_sets(d, project_rows(rows_by_paper[d['pmid']], scores_by_paper[d['pmid']], threshold))
        result[d['pmid']] = benchmarks._metrics(expected, actual)
    return result


def objective(metric, floor):
    precision, recall = metric['precision'] or 0., metric['recall'] or 0.
    if recall >= floor and precision >= .85:
        return 2, recall, metric['f1'] or 0., precision
    if recall >= floor:
        return 1, precision, recall, metric['f1'] or 0.
    return 0, recall, precision, metric['f1'] or 0.


def select_grid(docs, rows, target, floor, salt):
    folds = grouped_folds(docs, folds=3, salt=salt)
    grid = []
    for c in CS:
        scores = {}
        for fold in range(3):
            training_rows, labels = [], []
            for d in docs:
                if folds[d['pmid']] != fold:
                    training_rows.extend(rows[d['pmid']])
                    labels.extend(target[d['pmid']])
            model = fit(training_rows, labels, c)
            for d in docs:
                if folds[d['pmid']] == fold:
                    scores[d['pmid']] = [priority(r, model) for r in rows[d['pmid']]]
        for threshold in THRESHOLDS:
            per_paper = counts(docs, rows, scores, threshold)
            m = _metric(list(per_paper.values()))
            grid.append({'C': c, 'threshold': threshold, 'metrics': m})
    best = max(grid, key=lambda g: (objective(g['metrics'], floor), -g['C'], -g['threshold']))
    return best, grid


def train(docs, rows, baseline_rows):
    expected = {d['pmid']: evaluation_v6.relation_sets(d, {'relations': []})[0] for d in docs}
    target = {d['pmid']: [int((d['pmid'], tuple(sorted(r['key'][:2])), r['key'][2]) in expected[d['pmid']])
                             for r in rows[d['pmid']]] for d in docs}
    outer = grouped_folds(docs, folds=5, salt='repair-v2-outer')
    oof, reports = {}, []
    for fold in range(5):
        learning = [d for d in docs if outer[d['pmid']] != fold]
        floor = max(RECALL_FLOOR, _metric([baseline_rows[d['pmid']]['relation'] for d in learning])['recall'] or 0.)
        chosen, _ = select_grid(learning, rows, target, floor, 'repair-v2-inner-'+str(fold))
        training_rows = [r for d in learning for r in rows[d['pmid']]]
        labels = [v for d in learning for v in target[d['pmid']]]
        model = fit(training_rows, labels, chosen['C'])
        held = [d for d in docs if outer[d['pmid']] == fold]
        scores = {d['pmid']: [priority(r, model) for r in rows[d['pmid']]] for d in held}
        measured = counts(held, rows, scores, chosen['threshold'])
        oof.update(measured)
        reports.append({'fold': fold, 'held_pmids': [d['pmid'] for d in held], 'chosen': chosen,
                        'recall_floor': floor, 'metrics': _metric(list(measured.values()))})
    floor = max(RECALL_FLOOR, _metric([r['relation'] for r in baseline_rows.values()])['recall'] or 0.)
    selected, grid = select_grid(docs, rows, target, floor, 'repair-v2-final-inner')
    all_rows = [r for d in docs for r in rows[d['pmid']]]
    labels = [y for d in docs for y in target[d['pmid']]]
    model = fit(all_rows, labels, selected['C'])
    coverage = sum(sum(target[d['pmid']]) for d in docs)
    return {'schema': 'repair-v2-nested-cpu-correction', 'selected': selected,
            'recall_floor': floor, 'model': model, 'grid': grid, 'outer_folds': reports,
            'outer_metrics': _metric(list(oof.values())), 'outer_per_paper': oof,
            'training_papers': len(docs), 'gold_relations': sum(len(e) for e in expected.values()),
            'candidate_true_relations': coverage, 'candidate_recall': coverage/max(1, sum(len(e) for e in expected.values())),
            'fixed_upstream_exposure_notice': 'Nested evaluation applies to the new CPU corrector, conditional on frozen upstream models and their known training history.',
            'features_include_ids_or_pmid': False, 'dev_labels_used_for_selection': False,
            'nutrition_accuracy_probability': False}


def ablations(payload, parsed_models, baseline, rows, model, threshold):
    corrected = []
    index = defaultdict(list)
    for parsed in parsed_models.values():
        for e in parsed['events']:
            c, g = e['chemical_mention'], e['gene_mention']
            index[source_key(c['source_mention'], g['source_mention'])].append(e)
    for r in baseline['relations']:
        c, g = r['endpoints']
        events = index[source_key(c, g)]
        identifiers = []
        trace = []
        for kind, original in [('chemical', r['entity1']), ('gene', r['entity2'])]:
            choices = {e[kind+'_mention']['selected_id'] for e in events
                       if changed_identity_allowed(e[kind+'_mention'], original, payload['source'])}
            identifier = next(iter(choices)) if len(choices) == 1 else original
            identifiers.append(identifier)
            trace.append({'kind': kind, 'previous_id': original, 'selected_id': identifier,
                          'changed': original != identifier, 'basis': 'unique_explicit_source_or_history' if original != identifier else 'preserve_existing'})
        value = {**r, 'entity1': identifiers[0], 'entity2': identifiers[1], 'identity_trace': trace}
        if r.get('subject'):
            value['subject'] = identifiers[int(r['subject'] == r['entity2'])]
        corrected.append(value)
    scores = [priority(r, model) for r in rows]
    alternatives = project_rows(rows, scores, 0.)['relations']
    by_pair = {pair_key(r): r for r in alternatives}
    typed = [{**r, 'predicate': by_pair.get(pair_key(r), r)['predicate']} for r in corrected]
    combined = project_rows(rows, scores, threshold)
    return {'identity_only': {'relations': corrected}, 'identity_and_type': {'relations': typed}, 'combined': combined}
