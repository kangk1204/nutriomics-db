"""Fold-local ID ranking, hierarchical relation learning, full-denominator choice."""
from collections import defaultdict
import math

from .common import C_GRID, THRESHOLDS, FLOOR, SEED, TYPES, log
from .identity import select_identity, aligned_ids, validity_features
from .events import bags, semantic_truth, project
from ..splits_v7 import grouped_folds
from ..chatgpt_evaluation import _metric
from ..chatgpt_repair_io import score


def fit_binary(features, target, c):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    if not target or len(set(target)) < 2:
        return {'intercept': 20. if target and target[0] else -20., 'weights': {}}
    vectorizer = DictVectorizer()
    x = vectorizer.fit_transform(features)
    model = LogisticRegression(C=c, max_iter=1000, class_weight='balanced', random_state=SEED).fit(x, target)
    return {'intercept': float(model.intercept_[0]),
            'weights': dict(zip(vectorizer.get_feature_names_out(), map(float, model.coef_[0])))}


def predict_binary(model, features):
    z = model['intercept']
    for k, v in features.items():
        token = k+'='+v if isinstance(v, str) else k
        z += model['weights'].get(token, 0.)*(1. if isinstance(v, str) else v)
    return 1/(1+math.exp(-max(-60., min(60., z))))


def type_scores(model, bag):
    # A single remaining hypothesis must not be normalized to certainty (1.0).
    # These are OVR conditional priorities, not a calibrated nutrition probability.
    return {t: predict_binary(model[t], f) for t, f in bag['types'].items()}


def fit(papers, c, identity=None):
    identity = identity or select_identity(papers, c)
    validity_x, validity_y = [], []
    for paper in papers:
        for m in paper['mentions']:
            if paper['document'].get('entities') is not None:
                validity_x.append(validity_features(m, paper['document']['text']))
                validity_y.append(int(bool(aligned_ids(m, paper['document']))))
    # These negatives are from the completely entity-annotated relation corpus,
    # not from unannotated/entity-only documents used as relation negatives.
    identity = {**identity, 'validity_model': fit_binary(validity_x, validity_y, c)}
    presence_features, presence_targets = [], []
    type_features, type_targets = defaultdict(list), defaultdict(list)
    for paper in papers:
        for bag in bags(paper, identity):
            truth = semantic_truth(bag, paper['document'])
            if truth is None:
                continue
            presence_features.append(bag['features'])
            presence_targets.append(int(bool(truth)))
            if truth:
                for t, features in bag['types'].items():
                    type_features[t].append(features)
                    type_targets[t].append(int(t in truth))
    return {'identity': identity, 'presence': fit_binary(presence_features, presence_targets, c),
            'type': {t: fit_binary(type_features[t], type_targets[t], c) for t in TYPES},
            'C': c, 'semantic_positive_bags': sum(presence_targets),
            'semantic_training_bags': len(presence_targets)}


def infer(papers, model, threshold, lock_ids=False):
    return {p['pmid']: project(p, bags(p, model['identity'], lock_ids),
                              model['presence'], model['type'], threshold) for p in papers}


def measure(papers, predictions):
    measured = score([p['document'] for p in papers], predictions)
    return _metric([v['relation'] for v in measured.values()]), measured


def identity_diagnostics(papers, policy):
    from .identity import choose, ranked
    from collections import Counter
    counts = defaultdict(Counter)
    for p in papers:
        for m in p['mentions']:
            truth = aligned_ids(m, p['document'])
            k = m['kind']
            counts[k]['all_automatic_mentions'] += 1
            selected = choose(m, policy['model'], policy['threshold'], policy['margin'], policy['enabled'])
            counts[k]['selected'] += int(selected['selected_id'] is not None)
            counts[k]['nil'] += int(selected['selected_id'] is None)
            if not truth:
                counts[k]['unmatched_or_ambiguous'] += 1
                continue
            counts[k]['aligned_mentions'] += 1
            ids = [r['native_id'] for r in ranked(m, policy['model']) if r['native_id']]
            counts[k]['recall10_hits'] += int(bool(set(ids[:10]) & truth))
            counts[k]['recall50_hits'] += int(bool(set(ids[:50]) & truth))
            old, new = set(m['old_ids']), selected['selected_id']
            counts[k]['selected_correct'] += int(new in truth)
            counts[k]['old_correct_preserved'] += int(bool(old & truth) and new in truth)
            counts[k]['old_correct_damaged'] += int(bool(old & truth) and new not in truth)
            counts[k]['wrong_old_corrected'] += int(bool(old) and not old & truth and new in truth)
            changed = new is not None and new not in old
            counts[k]['new_or_changed'] += int(changed)
            counts[k]['new_or_changed_correct'] += int(changed and new in truth)
            counts[k]['new_link'] += int(not old and new is not None)
    return {k: dict(v) for k, v in counts.items()}


def objective(m, floor):
    p, r = m['precision'] or 0., m['recall'] or 0.
    if p >= .85 and r >= floor:
        return 2, r, m['f1'] or 0., p
    if r >= floor:
        return 1, p, r, m['f1'] or 0.
    return 0, r, p, m['f1'] or 0.


def select(papers, salt, floor=FLOOR):
    folds = grouped_folds([p['document'] for p in papers], folds=3, salt=salt)
    grid, cache = [], {}
    for c in C_GRID:
        predictions = {}
        for fold in range(3):
            training = [p for p in papers if folds[p['pmid']] != fold]
            held = [p for p in papers if folds[p['pmid']] == fold]
            model = fit(training, c)
            # Materialize zero-cutoff outputs once; thresholding doesn't change a winner.
            predictions.update(infer(held, model, 0.))
        cache[c] = predictions
        for threshold in THRESHOLDS:
            selected = {}
            for pmid, value in predictions.items():
                selected[pmid] = {**value, 'relations': [r for r in value['relations'] if r['priority'] >= threshold]}
            metrics, _ = measure(papers, selected)
            grid.append({'C': c, 'threshold': threshold, 'metrics': metrics})
        log('inner-grid', papers=len(papers), C=c, best=max(
            (r for r in grid if r['C'] == c), key=lambda r: objective(r['metrics'], floor)))
    best = max(grid, key=lambda r: (objective(r['metrics'], floor), -r['C'], -r['threshold']))
    return best, grid


def train(papers):
    folds = grouped_folds([p['document'] for p in papers], folds=5, salt='repair-v3-outer')
    outer, per_paper = [], {}
    for fold in range(5):
        learning = [p for p in papers if folds[p['pmid']] != fold]
        held = [p for p in papers if folds[p['pmid']] == fold]
        chosen, _ = select(learning, 'repair-v3-inner-'+str(fold))
        model = fit(learning, chosen['C'])
        predictions = infer(held, model, chosen['threshold'])
        metrics, measured = measure(held, predictions)
        per_paper.update(measured)
        outer.append({'fold': fold, 'training_pmids': [p['pmid'] for p in learning],
                      'held_pmids': [p['pmid'] for p in held], 'selected': chosen, 'metrics': metrics,
                      'identity_diagnostics': identity_diagnostics(held, model['identity'])})
        log('outer-fold', fold=fold, metrics=metrics)
    selected, grid = select(papers, 'repair-v3-final-inner')
    model = fit(papers, selected['C'])
    outer_metrics = _metric([v['relation'] for v in per_paper.values()])
    qualifies = selected['metrics']['precision'] >= .85 and selected['metrics']['recall'] >= FLOOR
    return {'schema': 'repair-v3-nested-hierarchical', 'selected': selected, 'grid': grid,
            'model': model, 'outer_folds': outer, 'outer_metrics': outer_metrics,
            'outer_per_paper': per_paper, 'recall_floor': FLOOR,
            'training_gate_passed': qualifies, 'diagnostic_only': not qualifies,
            'dev_labels_used_for_selection': False, 'id_numbers_or_pmid_are_features': False,
            'semantic_labels_independent_of_predicted_ids': True,
            'entity_validity_negatives': 'automatic mentions unmatched in fully entity-annotated BioREDirect training only',
            'fixed_upstream_exposure': 'v2 upstream models had prior train exposure; conditional CV, not independent',
            'training_papers': len(papers)}
