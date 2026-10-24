"""Mention-level official candidate retrieval and pairwise ID/NIL ranking."""
from array import array
from collections import defaultdict, Counter
from difflib import SequenceMatcher
from functools import lru_cache
import math
import re

from ..chatgpt_grounding import key, species, definitions
from ..chatgpt_extraction import exact_span
from .common import C_GRID, SEED


def mention_key(m):
    return m['kind'], m['start'], m['end']


def aligned_ids(mention, document):
    """No predicted ID participates in semantic supervision."""
    kind = 'component' if mention['kind'] == 'chemical' else 'gene'
    matches = [e for e in document.get('entities', []) if e['kind'] == kind
               and e['start'] == mention['start'] and e['end'] == mention['end']]
    if not matches:
        matches = [e for e in document.get('entities', []) if e['kind'] == kind
                   and max(e['start'], mention['start']) < min(e['end'], mention['end'])
                   and min(e['end'], mention['end'])-max(e['start'], mention['start'])
                   >= .8*max(e['end']-e['start'], mention['end']-mention['start'])]
    alternatives = {tuple(sorted(e.get('native_ids', []))) for e in matches}
    return set(next(iter(alternatives))) if len(alternatives) == 1 else set()


class Retriever:
    def __init__(self, catalog):
        self.catalog = catalog
        self.by_id = defaultdict(list)
        for r in catalog.records:
            self.by_id[r['kind'], r['native_id']].append(r)
        self.grams = None

    def _index(self):
        # Compact postings, four deterministic grams per official representative.
        self.grams = defaultdict(lambda: array('I'))
        for i, record in enumerate(self.catalog.records):
            names = [key(n) for n in record['names'] if 4 <= len(n) <= 64]
            if not names:
                continue
            name = min(names, key=lambda n: (len(n), n))
            for gram in sorted({name[j:j+3] for j in range(len(name)-2)})[:4]:
                self.grams[record['kind'], gram].append(i)

    @lru_cache(maxsize=12000)
    def fuzzy(self, kind, surface):
        if len(surface) < 4:
            return ()
        if self.grams is None:
            self._index()
        grams = {surface[j:j+3] for j in range(len(surface)-2)}
        postings = sorted((self.grams.get((kind, g), ()) for g in grams), key=len)
        counts = Counter()
        for values in postings[:4]:
            counts.update(values[:3000])
        result = []
        for i, _ in counts.most_common(300):
            r = self.catalog.records[i]
            similarity = max(SequenceMatcher(None, surface, key(n)).ratio()
                             for n in r['names'] if n)
            if similarity >= .72:
                result.append((similarity, i))
        return tuple(i for _, i in sorted(result, key=lambda x: (-x[0], x[1]))[:50])

    def retrieve(self, mention, source, old_ids):
        rows = {r['native_id']: r for r in self.catalog.candidates(mention, source, limit=50)}
        # Retain all official IDs previously supplied, including historical menu entries.
        for r in mention.get('candidates', []):
            if r.get('native_id'):
                rows.setdefault(r['native_id'], {**r, 'features': {}})
        if len(rows) < 10:
            for i in self.fuzzy(mention['kind'], key(mention['text'])):
                r = self.catalog.records[i]
                rows.setdefault(r['native_id'], {**r, 'features': {}})
        for identifier in old_ids:
            official = self.by_id.get((mention['kind'], identifier), [])
            if official:
                r = official[0]
                rows.setdefault(identifier, {**r, 'features': {}})
        expansion = {key(v['long']) for v in definitions(source).get(mention['text'], [])}
        local = source[max(0, mention['start']-140):mention['end']+140]
        local_taxa, document_taxa = species(local), species(source)
        context = set(re.findall(r'\w+', key(local))) - set(re.findall(r'\w+', key(mention['text'])))
        for identifier, r in rows.items():
            names = [n for n in r.get('names', []) if isinstance(n, str)]
            normalized = {key(n) for n in names}
            taxon = r.get('taxon')
            features = {
                'chemical': float(mention['kind'] == 'chemical'),
                'exact_alias': float(key(mention['text']) in normalized),
                'exact_case': float(mention['text'] in names),
                'expansion': float(bool(expansion & normalized)),
                'ngram_similarity': max((SequenceMatcher(None, key(mention['text']), n).ratio() for n in normalized), default=0.),
                'context_overlap': max((len(set(re.findall(r'\w+', n)) & context)/max(1, len(n.split())) for n in normalized), default=0.),
                'local_tax_match': float(taxon in local_taxa),
                'local_tax_conflict': float(mention['kind'] == 'gene' and bool(local_taxa) and taxon not in local_taxa),
                'doc_tax_match': float(taxon in document_taxa),
                'doc_tax_conflict': float(mention['kind'] == 'gene' and bool(document_taxa) and taxon not in document_taxa),
                'taxon_unknown': float(not document_taxa),
                'supported_species': float(mention['kind'] == 'chemical' or taxon in {9606, 10090, 10116}),
                'old_id': float(identifier in old_ids),
                'historical': float(bool(r.get('historical_record'))),
                'short': float(len(mention['text']) <= 3),
                'candidate_ambiguity': math.log1p(len(rows)), 'nil': 0.,
            }
            r['features'] = features
        ordered = sorted(rows.values(), key=lambda r: (
            -int(r['native_id'] in old_ids), -r['features']['expansion'],
            -r['features']['exact_alias'], -r['features']['local_tax_match'],
            r['features']['local_tax_conflict'], -r['features']['ngram_similarity'], r['native_id']))
        # The cap is explicit; old IDs are never silently truncated.
        keep = max(50, len(old_ids))
        return [{k: r.get(k) for k in ('native_id', 'taxon', 'names', 'features')}
                for r in ordered[:keep]]


def build_mentions(payload, baseline, retriever):
    source = payload['source']
    originals = defaultdict(set)
    for r in baseline['relations']:
        for j, m in enumerate(r.get('endpoints', [])):
            originals[mention_key(m)].add(r['entity1' if j == 0 else 'entity2'])
    mentions = {mention_key(m): dict(m) for m in payload['mentions'] if exact_span(source, m)}
    # Existing standalone dictionary candidates, not just mentions used by relations.
    automatic, truncated = retriever.catalog.mentions(source, limit=320)
    for m in automatic:
        mentions.setdefault(mention_key(m), m)
    for r in baseline['relations']:
        for m in r.get('endpoints', []):
            if exact_span(source, m):
                mentions.setdefault(mention_key(m), dict(m))
    result = []
    for i, token in enumerate(sorted(mentions)):
        m = mentions[token]
        old = sorted(originals[token])
        expansions = definitions(source).get(m['text'], [])
        normalized = key(expansions[0]['long']) if len(expansions) == 1 else key(m['text'])
        rows = retriever.retrieve(m, source, old)
        result.append({**{k: m[k] for k in ('kind', 'text', 'start', 'end')},
            'key': 'm'+str(i), 'cluster': m['kind']+':'+normalized, 'old_ids': old,
            'candidates': rows, 'source': 'automatic_union', 'evidence_spans': [
                {k: m[k] for k in ('start', 'end', 'text')}],
            'candidate_truncated': len(rows) >= 50})
    return result, truncated


def options(m):
    rows = m['candidates']
    return rows + [{'native_id': None, 'features': {
        'nil': 1., 'chemical': float(m['kind'] == 'chemical'),
        'candidate_ambiguity': math.log1p(len(rows)),
        'best_alias': max((r['features'].get('exact_alias', 0.) for r in rows), default=0.),
        'best_expansion': max((r['features'].get('expansion', 0.) for r in rows), default=0.)}}]


def validity_features(m, source):
    """Entity validity is distinct from semantic presence and ID correctness."""
    local = source[max(0, m['start']-90):m['end']+90]
    features = {'kind': m['kind'], 'short': float(len(m['text']) <= 3),
        'uppercase': float(m['text'].isupper()), 'has_old_id': float(bool(m['old_ids'])),
        'has_candidates': float(bool(m['candidates'])), 'surface': key(m['text']),
        'sequence_context': float(bool(re.search(r'codon|substitution|residue|missense|\b[A-Z]\d+[A-Z]\b', local))),
        'administered': float(bool(re.search(r'administer|treated|dose|supplement|injection', local, re.I)))}
    for side, text in [('left', source[max(0, m['start']-90):m['start']]),
                       ('right', source[m['end']:m['end']+90])]:
        words = re.findall(r'[a-z]+', text.casefold())
        words = words[-8:] if side == 'left' else words[:8]
        for w in words:
            features[side+':'+w] = 1.
    return features


def train_ranker(papers, c):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    examples, labels = [], []
    for paper in papers:
        if '_ranking_examples' not in paper:
            px, py = [], []
            for m in paper['mentions']:
                truth = aligned_ids(m, paper['document'])
                if not truth:
                    continue  # incomplete/unmatched annotations do not create negatives
                rows = options(m)
                positive = [r for r in rows if r['native_id'] in truth]
                if not positive:
                    positive = [rows[-1]]
                negative = [r for r in rows if r not in positive]
                for yes in positive:
                    for no in negative:
                        names = yes['features'].keys() | no['features'].keys()
                        delta = {k: yes['features'].get(k, 0.)-no['features'].get(k, 0.) for k in names}
                        px.extend((delta, {k: -v for k, v in delta.items()}))
                        py.extend((1, 0))
            paper['_ranking_examples'] = px, py
        px, py = paper['_ranking_examples']
        examples.extend(px)
        labels.extend(py)
    if not examples:
        return {'weights': {}, 'C': c, 'training_pairs': 0}
    vectorizer = DictVectorizer()
    x = vectorizer.fit_transform(examples)
    model = LogisticRegression(C=c, fit_intercept=False, max_iter=600, random_state=SEED).fit(x, labels)
    return {'weights': dict(zip(vectorizer.get_feature_names_out(), map(float, model.coef_[0]))),
            'C': c, 'training_pairs': len(labels)//2}


def ranked(m, model):
    rows = []
    for r in options(m):
        score = sum(model['weights'].get(k, 0.)*v for k, v in r['features'].items())
        rows.append({**r, 'ranking_score': score})
    return sorted(rows, key=lambda r: (-r['ranking_score'], str(r['native_id'])))


def choose(m, model, threshold, margin, enabled=True):
    rows = ranked(m, model)
    top = rows[0]
    probability = 1/(1+math.exp(-max(-60., min(60., top['ranking_score']))))
    gap = top['ranking_score']-rows[1]['ranking_score'] if len(rows) > 1 else 99.
    candidate = top['native_id'] if probability >= threshold and gap >= margin else None
    selected = candidate if enabled else None
    reason = 'ranked_selection' if selected else 'nil_or_training_precision_gate'
    old = m['old_ids']
    if selected is None and len(old) == 1:
        selected, reason = old[0], 'preserve_existing_unresolved'
    return {**m, 'selected_id': selected, 'ranked_candidate': candidate,
            'selection_reason': reason, 'ranking_priority': probability,
            'ranking_margin': gap, 'top10': [r['native_id'] for r in rows if r['native_id']][:10],
            'priority_is_accuracy_probability': False}


def select_identity(papers, c):
    """OOF policy; callers pass only their current training fold."""
    from ..splits_v7 import grouped_folds
    folds = grouped_folds([p['document'] for p in papers], folds=3, salt='repair-v3-identity-inner')
    held = []
    for fold in range(3):
        train = [p for p in papers if folds[p['pmid']] != fold]
        model = train_ranker(train, c)
        for p in papers:
            if folds[p['pmid']] == fold:
                for m in p['mentions']:
                    truth = aligned_ids(m, p['document'])
                    if truth:
                        rows = ranked(m, model)
                        top = rows[0]
                        probability = 1/(1+math.exp(-max(-60., min(60., top['ranking_score']))))
                        gap = top['ranking_score']-rows[1]['ranking_score'] if len(rows) > 1 else 99.
                        held.append((m, truth, top['native_id'], probability, gap))
    choices = []
    for threshold in (.05, .25, .5, .7, .9, .95, .99):
        for margin in (0., .25, .5, 1., 2.):
            tp = fp = changes = 0
            for m, truth, proposed, probability, gap in held:
                new = proposed if probability >= threshold and gap >= margin else None
                if new is not None and new not in m['old_ids']:
                    changes += 1
                    tp += int(new in truth)
                    fp += int(new not in truth)
            precision = tp/(tp+fp) if tp+fp else None
            choices.append({'threshold': threshold, 'margin': margin, 'tp': tp, 'fp': fp,
                            'changes': changes, 'precision': precision,
                            'enabled': bool(changes and precision >= .90)})
    best = max(choices, key=lambda x: (x['enabled'], x['tp'] if x['enabled'] else 0,
                                      x['precision'] or 0., x['threshold'], x['margin']))
    return {**best, 'model': train_ranker(papers, c), 'grid': choices,
            'supervision': 'matched entity annotations; pairwise candidates including NIL'}
