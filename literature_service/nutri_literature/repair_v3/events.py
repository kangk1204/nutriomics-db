"""Typed witnesses, independent mention identity, and permutation-invariant bags."""
from collections import defaultdict, Counter
import re

from ..chatgpt_extraction import exact_span
from ..chatgpt_grounding import species
from ..contract_v7 import canonical, sha
from .common import TYPES
from .identity import mention_key, choose, aligned_ids


def supported_types(event):
    """No unconditional four-way alternatives, no unknown-to-Association fallback."""
    result = set()
    label = event.get('native_type')
    if label in TYPES and (event['decision'] == 'supported' or event['origin'] == 'prior_same_type'):
        result.add(label)
    measurement, change = event.get('measurement'), event.get('change')
    quote = ' '.join(s['text'] for s in event['evidence'])
    if event['decision'] != 'unsupported' and event['observation'] != 'planned':
        if measurement == 'binding' and re.search(r'\bbind\w*|\baffinit\w*|\bligand\w*', quote, re.I):
            result.add('Bind')
        if measurement in {'phosphorylation', 'ubiquitination', 'other_modification'}:
            if re.search(r'phosphorylat|ubiquitin|acetylat|methylat', quote, re.I):
                result.add('Association')
        if (measurement in {'expression', 'activity'} and not event['negated']
                and change in {'increase', 'decrease'}):
            cue = (r'increas|enhanc|up.?regulat|activat|induc|augment' if change == 'increase'
                   else r'decreas|reduc|down.?regulat|inhibit|suppress')
            if re.search(cue, quote, re.I):
                result.add('Positive_Correlation' if change == 'increase' else 'Negative_Correlation')
    return sorted(result)


def build_events(payload, mentions, reviews, baseline):
    source = payload['source']
    by_span = {mention_key(m): m for m in mentions}
    source_mentions = {m['key']: by_span[mention_key(m)] for m in payload['mentions'] if mention_key(m) in by_span}
    sentences = {s['key']: s for s in payload['sentences']}
    events, rejected = [], []

    def add(c, g, evidence, values, origin):
        if not c or not g or c['kind'] != 'chemical' or g['kind'] != 'gene':
            rejected.append('invalid_endpoint')
            return
        if not evidence or any(not exact_span(source, s) for s in evidence):
            rejected.append('invalid_source_span')
            return
        if any(not any(s['start'] <= m['start'] < m['end'] <= s['end'] for s in evidence) for m in (c, g)):
            rejected.append('endpoints_outside_evidence')
            return
        positions = [i for i, s in enumerate(payload['sentences'])
                     if any(s['start'] == e['start'] and s['end'] == e['end'] for e in evidence)]
        if positions and max(positions)-min(positions) > 1:
            rejected.append('non_adjacent_evidence')
            return
        e = {'chemical': c['key'], 'gene': g['key'], 'origin': origin,
             'native_type': values.get('benchmark_relation_type', values.get('predicate')),
             'decision': values.get('decision', 'supported' if origin == 'prior_same_type' else 'uncertain'),
             'measurement': values.get('measurement', 'unspecified'),
             'change': values.get('change', 'unspecified'),
             'observation': values.get('observation') or 'unknown',
             'negated': values.get('negated') is True,
             'speculative': values.get('speculative') is True,
             'non_significant': values.get('non_significant') is True,
             'role_direction': values.get('role_direction') or 'unknown',
             'condition': values.get('condition'), 'evidence': sorted(evidence, key=lambda s: s['start']),
             'entity_roles': values.get('entity_roles', {}), 'source_sha256': sha(source)}
        e['supported_types'] = supported_types(e)
        e['key'] = 'e:'+sha(canonical(e))[:24]
        events.append(e)

    for model in sorted(reviews):
        if reviews[model].get('parse_failure'):
            rejected.append('review_parse_failure')
        raw = reviews[model]['raw']
        node_rows = raw.get('mentions', [])
        duplicates = {k for k, n in Counter(r.get('key') for r in node_rows if isinstance(r, dict)).items() if n > 1}
        nodes = {r['key']: r for r in node_rows if isinstance(r, dict) and isinstance(r.get('key'), str)
                 and r['key'] not in duplicates}
        for r in raw.get('events', []):
            if not isinstance(r, dict):
                continue
            ck, gk = r.get('chemical'), r.get('gene')
            if ck not in nodes or gk not in nodes:
                rejected.append('invalid_or_duplicate_endpoint')
                continue
            spans = [sentences[k] for k in r.get('evidence', []) if k in sentences]
            if len(spans) != len(r.get('evidence', [])):
                rejected.append('unknown_evidence')
                continue
            add(source_mentions.get(ck), source_mentions.get(gk), spans,
                {**r, 'entity_roles': {k: nodes[v].get('role') for k, v in [('chemical', ck), ('gene', gk)]}}, model)
    for r in baseline['relations']:
        endpoints = r.get('endpoints', [])
        if len(endpoints) == 2:
            add(by_span.get(mention_key(endpoints[0])), by_span.get(mention_key(endpoints[1])),
                r.get('evidence', []), r, 'prior_same_type')
    # Source-only recovery requires an explicit local syntactic pattern, not co-occurrence.
    for sentence in payload['sentences']:
        inside = [m for m in mentions if sentence['start'] <= m['start'] < m['end'] <= sentence['end']]
        chemicals = [m for m in inside if m['kind'] == 'chemical']
        genes = [m for m in inside if m['kind'] == 'gene']
        for c in chemicals:
            for g in genes:
                if max(c['start'], g['start']) < min(c['end'], g['end']):
                    continue
                lo, hi = min(c['end'], g['end']), max(c['start'], g['start'])
                if hi-lo > 180 or any(lo < m['start'] and m['end'] < hi for m in inside if m not in (c, g)):
                    continue
                quote = sentence['text']
                for m, token in sorted([(c, ' CHEM '), (g, ' GENE ')], key=lambda x: -x[0]['start']):
                    a, b = m['start']-sentence['start'], m['end']-sentence['start']
                    quote = quote[:a]+token+quote[b:]
                label, measurement, change = None, 'unspecified', 'unspecified'
                if re.search(r'CHEM\s+(?:directly\s+)?bind\w*\s+(?:to\s+)?GENE|GENE\s+bind\w*\s+(?:to\s+)?CHEM', quote, re.I):
                    label, measurement = 'Bind', 'binding'
                active = re.search(r'CHEM\s+(?:significantly\s+|directly\s+)?(increas\w*|enhanc\w*|reduc\w*|decreas\w*|inhibit\w*|suppress\w*)\s+(?:the\s+)?(?:expression\s+of\s+)?GENE', quote, re.I)
                passive = re.search(r'GENE\s+(?:expression\s+|activity\s+)?(?:was\s+|were\s+|is\s+)(increas\w*|enhanc\w*|reduc\w*|decreas\w*|inhibit\w*|suppress\w*)\s+by\s+CHEM', quote, re.I)
                match = active or passive
                if match:
                    change = 'increase' if re.match('increas|enhanc', match[1], re.I) else 'decrease'
                    measurement = 'expression' if re.search('express', quote, re.I) else 'activity'
                    label = 'Positive_Correlation' if change == 'increase' else 'Negative_Correlation'
                if re.search(r'association\s+between\s+(?:CHEM\s+and\s+GENE|GENE\s+and\s+CHEM)', quote, re.I):
                    label = 'Association'
                if label:
                    negated = bool(re.search(r'\b(?:not|no|neither|without)\b', quote, re.I))
                    planned = bool(re.search(r'\bwill\b|\b(?:aim|purpose|objective)\b', quote, re.I))
                    add(c, g, [sentence], {'benchmark_relation_type': label,
                        'measurement': measurement, 'change': change,
                        'decision': 'uncertain' if negated or planned else 'supported',
                        'negated': negated, 'observation': 'planned' if planned else 'unknown',
                        'role_direction': 'chemical_to_gene' if match else 'undirected'}, 'source_rule')
    unique = {e['key']: e for e in events}
    return sorted(unique.values(), key=lambda e: e['key']), dict(Counter(rejected))


def event_features(e, native_type=None):
    """Conjunctions are created inside one event before deterministic aggregation."""
    features = {'origin': e['origin'], 'measurement': e['measurement'], 'change': e['change'],
                'decision': e['decision'], 'observation': e['observation'],
                'direction': e['role_direction'], 'negated': float(e['negated']),
                'non_significant': float(e['non_significant']), 'speculative': float(e['speculative']),
                'has_condition': float(bool(e['condition'])),
                'measurement_change': e['measurement']+'|'+e['change'],
                'measurement_change_context': e['measurement']+'|'+e['change']+'|'+e['observation'],
                'measurement_change_negation': e['measurement']+'|'+e['change']+'|'+str(e['negated']),
                'chemical_role': e['entity_roles'].get('chemical') or 'unknown'}
    if native_type:
        features['matching_witness'] = float(native_type in e.get('supported_types', supported_types(e)))
        features['matching_model_label'] = float(e['native_type'] == native_type)
    quote = ' '.join(s['text'] for s in e['evidence'])
    patterns = {
        'sequence_context': r'\b(?:codon|substitution|residue|nucleotide|missense)\b|\b[A-Z]\d+[A-Z]\b',
        'no_effect': r'\bunaltered\b|\bunchanged\b|\bno (?:significant )?(?:effect|association|correlation)\b|did not affect',
        'planned': r'\b(?:aim|purpose|objective)\b|\bwill\b|\bto investigate\b',
        'binding': r'bind\w*|affinit\w*|ligand',
        'comparison': r'compar\w*|versus|\bvs\.?|whereas|\bbut\b',
        'administration': r'administer\w*|treated|treatment|supplement\w*|dose|injection',
        'measured': r'\b(?:measured|observed|demonstrated|showed|found)\b',
    }
    for name, pattern in patterns.items():
        features[name] = float(bool(re.search(pattern, quote, re.I)))
    return features


def aggregate(events, native_type=None):
    features = {'event_count': min(len(events), 20), 'type': native_type or 'presence'}
    for event in sorted(events, key=lambda e: e['key']):
        for k, value in event_features(event, native_type).items():
            token = k+'='+value if isinstance(value, str) else k
            value = 1. if isinstance(value, str) else value
            features['count:'+token] = features.get('count:'+token, 0.)+value
            features['max:'+token] = max(features.get('max:'+token, 0.), value)
    if native_type:
        features['matching_origins'] = len({e['origin'] for e in events if native_type in e.get('supported_types', supported_types(e))})
    return features


def bags(paper, identity_policy, lock_ids=False):
    from .learning import predict_binary
    from .identity import validity_features
    mentions = {}
    for m in paper['mentions']:
        if lock_ids:
            ids = paper.get('active_baseline_ids', {}).get(mention_key(m), set(m['old_ids']))
            selected = next(iter(ids)) if len(ids) == 1 else None
            mentions[m['key']] = {**m, 'selected_id': selected, 'selection_reason': 'locked_baseline'}
        else:
            mentions[m['key']] = choose(m, identity_policy['model'], identity_policy['threshold'],
                                        identity_policy['margin'], identity_policy['enabled'])
        mentions[m['key']]['validity_priority'] = predict_binary(
            identity_policy.get('validity_model', {'intercept': 20., 'weights': {}}),
            validity_features(m, paper['document']['text']))
    grouped = defaultdict(list)
    for e in paper['events']:
        c, g = mentions[e['chemical']], mentions[e['gene']]
        # Relation semantics remain learnable even when either ID is unresolved.
        token = c['cluster'], g['cluster']
        grouped[token].append(e)
    result = []
    for token, events in sorted(grouped.items()):
        ckeys, gkeys = sorted({e['chemical'] for e in events}), sorted({e['gene'] for e in events})
        cids = {mentions[k]['selected_id'] for k in ckeys if mentions[k]['selected_id']}
        gids = {mentions[k]['selected_id'] for k in gkeys if mentions[k]['selected_id']}
        # Conflicting species/identities are not resolved with a relationship score.
        identifiers = [next(iter(v)) if len(v) == 1 else None for v in (cids, gids)]
        witnesses = {t: sorted([e for e in events if t in e['supported_types']], key=lambda e: e['key']) for t in TYPES}
        result.append({'key': list(token), 'ids': identifiers, 'events': sorted(events, key=lambda e: e['key']),
            'chemical_mentions': [mentions[k] for k in ckeys], 'gene_mentions': [mentions[k] for k in gkeys],
            'features': aggregate(events), 'types': {t: aggregate(witnesses[t], t) for t in TYPES if witnesses[t]},
            'witnesses': witnesses, 'unresolved': not all(identifiers),
            'entity_validity': min(max(mentions[k]['validity_priority'] for k in ckeys),
                                   max(mentions[k]['validity_priority'] for k in gkeys))})
    return result


def semantic_truth(bag, document):
    from ..evaluation_v6 import relation_sets
    gold = relation_sets(document, {'relations': []})[0]
    chemical = set().union(*(aligned_ids(m, document) for m in bag['chemical_mentions']))
    gene = set().union(*(aligned_ids(m, document) for m in bag['gene_mentions']))
    if not chemical or not gene:
        return None  # no reliable alignment, not an arbitrary relation negative
    return sorted({t for pmid, pair, t in gold if set(pair) & chemical and set(pair) & gene})


def project(paper, document_bags, presence_model, type_model, threshold):
    from .learning import predict_binary, type_scores
    relations, decisions = {}, []
    for bag in document_bags:
        presence = predict_binary(presence_model, bag['features'])
        scores = type_scores(type_model, bag)
        if scores:
            native_type, value = max(scores.items(), key=lambda kv: (kv[1], kv[0]))
            priority = presence*value*bag.get('entity_validity', 1.)
        else:
            native_type, priority = None, 0.
        accepted = bool(native_type and not bag['unresolved'] and priority >= threshold)
        reason = ('supported' if accepted else 'id_unresolved' if bag['unresolved'] else
                  'type_unresolved' if native_type is None else 'below_training_threshold')
        decisions.append({'mention_pair': bag['key'], 'ids': bag['ids'], 'native_type': native_type,
                          'priority': priority, 'accepted': accepted, 'reason': reason})
        if not accepted:
            continue
        # Citation chosen ONLY among witnesses of the actual selected type.
        event = max(bag['witnesses'][native_type], key=lambda e: (
            e['decision'] == 'supported', e['observation'] == 'observed',
            e['origin'] != 'prior_same_type', not e['speculative'], e['key']))
        by_key = {m['key']: m for m in paper['mentions']}
        endpoints = [by_key[event[k]] for k in ('chemical', 'gene')]
        relation = {'entity1': bag['ids'][0], 'entity2': bag['ids'][1], 'predicate': native_type,
            'endpoints': [{k: m[k] for k in ('kind', 'start', 'end', 'text')} for m in endpoints],
            'evidence': event['evidence'], 'evidence_event_key': event['key'],
            'scientific_events': bag['events'], 'priority': priority,
            'priority_is_accuracy_probability': False, 'review_status': 'unreviewed',
            **{k: event[k] for k in ('negated', 'speculative', 'non_significant', 'observation', 'role_direction')}}
        if event['role_direction'] in {'chemical_to_gene', 'gene_to_chemical'}:
            relation['subject'] = bag['ids'][int(event['role_direction'] == 'gene_to_chemical')]
        pair = tuple(bag['ids'])
        if pair not in relations or priority > relations[pair]['priority']:
            relations[pair] = relation
    return {'relations': sorted(relations.values(), key=lambda r: (r['entity1'], r['entity2'])),
            'decisions': decisions, 'unresolved_bags': sum(b['unresolved'] for b in document_bags),
            'abstained_bags': sum(not d['accepted'] for d in decisions)}
