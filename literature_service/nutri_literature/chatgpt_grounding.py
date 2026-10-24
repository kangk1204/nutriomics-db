"""Official-catalog retrieval and paper-cross-fitted identity ranking.

IDs are outputs, never classifier features. No relation labels choose an ID.
Unicode matches always map back to the untouched source.
"""
from collections import defaultdict
from functools import lru_cache
import gzip
import math
from pathlib import Path
import re
import unicodedata

from .contract_v7 import immutable, sha
from .text import sentence_spans

TAXA = {9606: r"\b(?:humans?|patients?|participants?|women|men|HeLa|HEK.?293|HepG2|MCF.?7|HCT.?116)\b",
        10090: r"\b(?:mice|mouse|murine|C2C12|RAW.?264)\b", 10116: r"\b(?:rats?|rattus)\b"}
CS = (.1, 1., 10.)
THRESHOLDS = (0., .5, .7, .8, .9, .95)
MARGINS = (0., .05, .15)


def mapped_key(text):
    chars, offsets = [], []
    for i, char in enumerate(text):
        for value in unicodedata.normalize("NFKC", char).casefold():
            value = "-" if value in "‐‑‒–—−" else value
            value = " " if value.isspace() else value
            if value == " " and (not chars or chars[-1] == " "):
                continue
            chars.append(value)
            offsets.append((i, i + 1))
    if chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()
    return "".join(chars), offsets


def key(text):
    return mapped_key(text)[0]


@lru_cache(maxsize=2048)
def species(text):
    return {taxon for taxon, pattern in TAXA.items() if re.search(pattern, text, re.I)}


@lru_cache(maxsize=600)
def definitions(text):
    from .extraction_v2 import abbreviations
    return abbreviations(text)


class Catalog:
    def __init__(self, records):
        self.records = records
        self.aliases = defaultdict(list)
        for i, row in enumerate(records):
            for name in {key(n) for n in row['names'] if isinstance(n, str)}:
                if len(name) >= 2:
                    self.aliases[name].append(i)
        self.automaton = None

    @classmethod
    def load(cls, path):
        import json
        raw = Path(path).read_bytes()
        value = json.loads(gzip.decompress(raw) if str(path).endswith('.gz') else raw)
        if value.get('schema_version') != 'literature-chatgpt-id-dictionary-v1' or not value.get('source_references'):
            raise ValueError('Official dictionary provenance is required')
        result = cls(value['records'])
        result.sha256 = sha(raw)
        return result

    def candidates(self, mention, text, limit=10):
        surface, kind = mention['text'], mention['kind']
        expansions = [r['long'] for r in definitions(text).get(surface, [])]
        expansion_keys = {key(e) for e in expansions}
        terms = [surface, *expansions]
        grouped = defaultdict(list)
        for term in terms:
            for i in self.aliases.get(key(term), []):
                row = self.records[i]
                if row['kind'] == kind:
                    grouped[(row['native_id'], row.get('taxon'))].append(row)
        start, end = mention['start'], mention['end']
        local = text[max(0, start-140):min(len(text), end+140)]
        local_words = set(re.findall(r'\w+', local.casefold()))
        surface_words = set(re.findall(r'\w+', surface.casefold()))
        stop = {'protein', 'gene', 'the', 'of', 'and', 'a', 'an', 'in', 'to'}
        context_words = local_words-surface_words-stop
        near_taxa, doc_taxa = species(local), species(text)
        rows = []
        for (identifier, taxon), records in grouped.items():
            names = {n for r in records for n in r['names']}
            exact = surface in names
            expanded = any(key(n) in expansion_keys for n in names)
            descriptions = [set(re.findall(r'\w+', n.casefold()))-surface_words-stop for n in names]
            overlap = max((len(words & context_words)/max(1, len(words)) for words in descriptions), default=0.)
            features = {'chemical': float(kind == 'chemical'), 'exact_case': float(exact),
                        'expansion': float(expanded), 'unique': float(len(grouped) == 1),
                        'short': float(len(surface) <= 3), 'uppercase': float(surface.isupper()),
                        'digits': float(any(c.isdigit() for c in surface)), 'name_overlap': overlap,
                        'local_tax_match': float(taxon in near_taxa),
                        'local_tax_conflict': float(bool(near_taxa) and taxon not in near_taxa and kind == 'gene'),
                        'doc_tax_match': float(taxon in doc_taxa),
                        'doc_tax_conflict': float(bool(doc_taxa) and taxon not in doc_taxa and kind == 'gene'),
                        'taxon_unknown': float(not doc_taxa), 'alias_ambiguity': math.log1p(len(grouped))}
            informative = sorted(names, key=lambda n: (-len(n.split()), -len(n), n))[:2]
            display_names = list(dict.fromkeys([*informative, *sorted(names, key=lambda n: (len(n), n))[:1]]))
            rows.append({'native_id': identifier, 'taxon': taxon, 'features': features, 'names': display_names})
        # Label-free retrieval order; no human default and no numeric-ID prior.
        rows.sort(key=lambda r: (-r['features']['expansion'], -r['features']['local_tax_match'],
                                r['features']['local_tax_conflict'], -r['features']['exact_case'],
                                -r['features']['doc_tax_match'], r['features']['doc_tax_conflict'],
                                -r['features']['name_overlap'], r['native_id']))
        return rows[:limit]

    def mentions(self, text, limit=160):
        import ahocorasick
        if self.automaton is None:
            self.automaton = ahocorasick.Automaton()
            for term in self.aliases:
                self.automaton.add_word(term, term)
            self.automaton.make_automaton()
        normalized, offsets = mapped_key(text)
        found = {}
        for right, term in self.automaton.iter(normalized):
            left, end = right-len(term)+1, right+1
            if (left and normalized[left-1].isalnum()) or (end < len(normalized) and normalized[end].isalnum()):
                continue
            start, stop = offsets[left][0], offsets[right][1]
            surface = text[start:stop]
            for kind in {self.records[i]['kind'] for i in self.aliases[term]}:
                if len(surface) <= 2 and not surface.isupper():
                    continue
                if surface.casefold() in {'and', 'the', 'with', 'from', 'this', 'that', 'were', 'was', 'not', 'may', 'are', 'can', 'but'}:
                    continue
                found[(kind, start, stop)] = {'kind': kind, 'text': surface, 'start': start, 'end': stop}
        # Longest spans of the same type; overlapping different types remain candidates.
        kept = []
        for mention in sorted(found.values(), key=lambda m: (-(m['end']-m['start']), m['start'], m['kind'])):
            if any(k['kind'] == mention['kind'] and k['start'] <= mention['start'] and mention['end'] <= k['end'] for k in kept):
                continue
            kept.append(mention)
        kept.sort(key=lambda m: (-len(m['text'].split()), -int(any(c.isdigit() for c in m['text'])),
                                 -int(m['text'].isupper()), -len(m['text']), m['start']))
        return sorted(kept[:limit], key=lambda m: (m['start'], m['end'], m['kind'])), len(kept) > limit


def priority(features, model):
    value = model['intercept'] + sum(model['weights'].get(k, 0.)*v for k, v in features.items())
    return 1./(1.+math.exp(-max(-60., min(60., value))))


def rank(rows, model):
    return sorted([{**r, 'priority': priority(r['features'], model)} for r in rows],
                  key=lambda r: (-r['priority'], r['native_id']))


def select(rows, model, threshold=None, margin=None):
    ranked = rank(rows, model)
    threshold = model['threshold'] if threshold is None else threshold
    margin = model['margin'] if margin is None else margin
    if not ranked or ranked[0]['priority'] < threshold:
        return None
    if len(ranked) > 1 and ranked[0]['priority']-ranked[1]['priority'] < margin:
        return None
    return ranked[0]


def fit_identity(catalog, documents, output):
    """5-fold paper grouping; selection sees only official training labels."""
    import numpy as np
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    from .splits_v7 import grouped_folds
    folds = grouped_folds(documents, salt='chatgpt-grounding-identity')
    examples, features, labels, row_folds = [], [], [], []
    for document in documents:
        for entity in document['entities']:
            if entity['kind'] not in {'gene', 'component'}:
                continue
            mention = {**entity, 'kind': 'chemical' if entity['kind'] == 'component' else 'gene'}
            rows = catalog.candidates(mention, document['text'])
            truth = set(entity.get('native_ids', []))
            if not truth:
                continue
            lo = len(features)
            for row in rows:
                features.append(row['features'])
                labels.append(int(row['native_id'] in truth))
                row_folds.append(folds[document['pmid']])
            examples.append({'pmid': document['pmid'], 'rows': rows, 'truth': truth, 'lo': lo, 'hi': len(features)})
    vectorizer = DictVectorizer(sparse=False)
    matrix = vectorizer.fit_transform(features)
    target, groups = np.asarray(labels), np.asarray(row_folds)
    best, reports, models = None, [], {}
    for c in CS:
        oof = np.zeros(len(features))
        fold_models = {}
        for fold in range(5):
            fitted = LogisticRegression(C=c, max_iter=500, class_weight='balanced', random_state=20261017)
            fitted.fit(matrix[groups != fold], target[groups != fold])
            oof[groups == fold] = fitted.predict_proba(matrix[groups == fold])[:, 1]
            fold_models[str(fold)] = {'intercept': float(fitted.intercept_[0]),
                                     'weights': dict(zip(vectorizer.get_feature_names_out(), map(float, fitted.coef_[0])))}
        for threshold in THRESHOLDS:
            for margin in MARGINS:
                correct = wrong = 0
                for e in examples:
                    scores = list(oof[e['lo']:e['hi']])
                    order = sorted(range(len(scores)), key=lambda i: -scores[i])
                    if not order or scores[order[0]] < threshold or (len(order) > 1 and scores[order[0]]-scores[order[1]] < margin):
                        continue
                    if e['rows'][order[0]]['native_id'] in e['truth']:
                        correct += 1
                    else:
                        wrong += 1
                missed = len(examples)-correct
                precision = correct/max(1, correct+wrong)
                recall = correct/len(examples)
                f1 = 2*correct/max(1, 2*correct+wrong+missed)
                report = {'C': c, 'threshold': threshold, 'margin': margin, 'tp': correct, 'fp': wrong,
                          'fn': missed, 'precision': precision, 'recall': recall, 'f1': f1}
                reports.append(report)
                criterion = (precision >= .90, recall if precision >= .90 else f1, f1, -c, -threshold, -margin)
                if best is None or criterion > best[0]:
                    best = (criterion, report, fold_models)
        models[c] = None
    chosen = best[1]
    fitted = LogisticRegression(C=chosen['C'], max_iter=500, class_weight='balanced', random_state=20261017).fit(matrix, target)
    model = {'intercept': float(fitted.intercept_[0]),
             'weights': dict(zip(vectorizer.get_feature_names_out(), map(float, fitted.coef_[0]))),
             'threshold': chosen['threshold'], 'margin': chosen['margin']}
    report = {'schema': 'chatgpt-identity-ranking-v1', 'training_papers': len(documents),
              'training_pmids': sorted(folds), 'folds': folds, 'selection': chosen,
              'candidate_recall_at_10': sum(any(r['native_id'] in e['truth'] for r in e['rows']) for e in examples)/len(examples),
              'selection_is_training_internal': True, 'nutrition_probabilities': False,
              'model': model, 'crossfit_models': {k: {**v, 'threshold': model['threshold'], 'margin': model['margin']}
                                                for k, v in best[2].items()}, 'grid': reports}
    immutable(output, report)
    return report


def source_payload(document, catalog, identity, *, crossfit=False):
    model = identity['crossfit_models'][str(identity['folds'][document['pmid']])] if crossfit else identity['model']
    text = document['text']
    sentences = [{'key': 's'+str(i), **s} for i, s in enumerate(sentence_spans(text))]
    mentions, truncated = catalog.mentions(text)
    result = []
    for i, mention in enumerate(mentions):
        rows = rank(catalog.candidates(mention, text), model)
        chosen = select(rows, model)
        result.append({**mention, 'key': 'm'+str(i),
                       'sentence': next((s['key'] for s in sentences if s['start'] <= mention['start'] < s['end']), None),
                       'selected_id': chosen['native_id'] if chosen else None,
                       'candidates': [{k: r[k] for k in ('native_id', 'taxon', 'names', 'priority')} for r in rows]})
    return {'source': text, 'source_sha256': sha(text), 'sentences': sentences, 'mentions': result,
            'candidate_truncation': truncated, 'scores_mean': 'identity ranking priority, not nutrition accuracy'}
