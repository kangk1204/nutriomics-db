"""Listwise identity ranking with explicit NIL, all old IDs revalidated."""
from copy import deepcopy
from pathlib import Path
import re
from .contract_v7 import sha, canonical


def candidate_rows(mention, text, sentences, search):
    from .identity_v6 import taxon_evidence, definition_for
    from .extraction_v3 import abbreviations
    from .features_v5 import identity_features
    sentence = next((s for s in sentences if s['start'] <= mention['start'] < mention['end'] <= s['end']), None)
    if sentence is None:
        return [], [{'nil': 1.}], {'eligible': False, 'reason': 'sentence_boundary_unresolved'}
    definition = definition_for(abbreviations(text), mention['text'], mention['start'])
    surface = definition['long'] if definition else mention['text']
    taxon, model, evidence = taxon_evidence(mention, sentence, sentences, text, {})
    family = bool(re.search(r'\b(?:family|genes|proteins)\b', surface, re.I))
    eligible = taxon in {9606, 10090, 10116} and not family
    surfaces = list(dict.fromkeys([surface, re.sub(r'\s+(?:gene|protein)$', '', surface, flags=re.I)]))
    found = {}
    for value in surfaces:
        for record in search.retrieve(value, taxon, limit=50):
            old = found.get(record['id'])
            key = lambda r: (bool(r['retrieval_exact']), float(r['retrieval_similarity']))
            if old is None or key(record) > key(old):
                found[record['id']] = record
    records = sorted(found.values(), key=lambda r: (not r['retrieval_exact'], r['taxon_id'] != taxon,
                                                  -r['retrieval_similarity'], r['id']))[:50]
    rows = []
    for i, record in enumerate(records):
        f = identity_features(mention, record, sentence['text'], taxon, definition)
        rows.append({**f, 'nil': 0., 'retrieval_exact': float(record['retrieval_exact']),
                     'retrieval_similarity': record['retrieval_similarity'], 'reciprocal_rank': 1 / (i + 1),
                     'eligible': float(eligible), 'family': float(family),
                     'span_priority': float(mention.get('payload', {}).get('detection_priority_score', 1.)),
                     'source_taxon_known': float(taxon is not None)})
    rows.append({'nil': 1., 'taxon_unknown_nil': float(taxon is None), 'family_nil': float(family),
                 'no_exact_nil': float(not any(r['retrieval_exact'] for r in records))})
    return records, rows, {'eligible': eligible, 'taxon_id': taxon, 'study_model': model, 'source_taxon_evidence': evidence}


class ListwiseRanker:
    def fit(self, examples, c=1.):
        import numpy as np
        from scipy.optimize import minimize
        from scipy.special import logsumexp
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.preprocessing import MaxAbsScaler
        self.vectorizer = DictVectorizer(dtype=np.float64)
        matrix = self.vectorizer.fit_transform([f for e in examples for f in e['features']])
        self.scaler = MaxAbsScaler().fit(matrix)
        matrix = self.scaler.transform(matrix).tocsr()
        groups, offset = [], 0
        for e in examples:
            end = offset + len(e['features'])
            positives = [j for j, y in enumerate(e['labels']) if y]
            if not positives:
                positives = [len(e['features']) - 1]
            groups.append((offset, end, np.asarray(positives), float(e.get('weight', 1.))))
            offset = end
        total_weight = sum(g[3] for g in groups)
        starts=np.asarray([g[0] for g in groups],dtype=np.int64)
        sizes=np.asarray([g[1]-g[0] for g in groups],dtype=np.int64)
        group_id=np.repeat(np.arange(len(groups)),sizes)
        weights=np.asarray([g[3] for g in groups])
        positive_indices=np.concatenate([start+positive for start,end,positive,weight in groups])
        positive_sizes=np.asarray([len(g[2]) for g in groups],dtype=np.int64)
        positive_starts=np.concatenate(([0],np.cumsum(positive_sizes)[:-1]))
        positive_group=np.repeat(np.arange(len(groups)),positive_sizes)
        def objective(w):
            scores = np.asarray(matrix @ w).ravel()
            maximum=np.maximum.reduceat(scores,starts)
            exponent=np.exp(scores-maximum[group_id])
            normalizer=np.add.reduceat(exponent,starts)
            positives=scores[positive_indices]
            positive_maximum=np.maximum.reduceat(positives,positive_starts)
            positive_exponent=np.exp(positives-positive_maximum[positive_group])
            positive_normalizer=np.add.reduceat(positive_exponent,positive_starts)
            loss=float(weights @ (maximum+np.log(normalizer)-positive_maximum-np.log(positive_normalizer)))
            derivative=weights[group_id]*exponent/normalizer[group_id]
            np.add.at(derivative,positive_indices,-weights[positive_group]*positive_exponent/positive_normalizer[positive_group])
            loss = loss / total_weight + .5 * float(w @ w) / (c * total_weight)
            gradient = np.asarray(matrix.T @ derivative).ravel() / total_weight + w / (c * total_weight)
            return loss, gradient
        result = minimize(objective, np.zeros(matrix.shape[1]), jac=True, method='L-BFGS-B',
                          options={'maxiter': 150, 'ftol': 1e-8})
        if not np.isfinite(result.fun) or not np.isfinite(result.x).all():
            raise ValueError('Non-finite listwise ID objective')
        self.weights = result.x
        self.optimization = {'success': bool(result.success), 'iterations': int(result.nit), 'loss': float(result.fun)}
        return self

    def predict(self, features):
        import numpy as np
        from scipy.special import softmax
        matrix = self.scaler.transform(self.vectorizer.transform(features))
        return list(map(float, softmax(np.asarray(matrix @ self.weights).ravel())))


def choose(records, scores, eligibility, threshold=.9, margin=.1):
    if not records or not eligibility['eligible']:
        return None
    ranked = sorted(range(len(records)), key=lambda i: (-scores[i], records[i]['id']))
    top = ranked[0]
    other = max([scores[-1], *(scores[i] for i in ranked[1:])])
    if records[top]['taxon_id'] != eligibility['taxon_id'] or scores[top] < threshold or scores[top] - other < margin:
        return None
    return records[top]


def apply(mentions, text, sentences, search, model, manifest):
    output, hypotheses = deepcopy(mentions), []
    for m in output:
        if m['kind'] != 'gene':
            continue
        records, features, eligibility = candidate_rows(m, text, sentences, search)
        scores = model.predict(features)
        selection = manifest['selection']
        proposed = choose(records, scores, eligibility, selection['threshold'], selection['margin'])
        old = m.get('normalized_id')
        enabled = bool(selection['target_precision_met'])
        if enabled:
            m['normalized_id'] = proposed['id'] if proposed else None
            m.setdefault('payload', {}).update(native_id=proposed['native_id'] if proposed else None,
                identity_reason=None if proposed else 'v7_listwise_abstained', normalization_rule='v7_listwise_with_nil')
        hypotheses.append({'kind': 'gene', 'start': m['start'], 'end': m['end'], 'text': m['text'],
            'previous_id': old, 'selected_id': m.get('normalized_id'), 'proposed_id': proposed['id'] if proposed else None,
            'automatic_applied': enabled, 'eligibility': eligibility, 'nil_priority': scores[-1],
            'candidates': [{k: r[k] for k in ('id', 'native_id', 'taxon_id')} | {'priority_score': s}
                           for r, s in sorted(zip(records, scores[:-1]), key=lambda x: -x[1])],
            'scores_are_nutrition_probabilities': False})
    return output, hypotheses
