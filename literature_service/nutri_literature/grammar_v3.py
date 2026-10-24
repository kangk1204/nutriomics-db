"""Explicit noun/verb dependency roles and cue-anchored contextual scope."""
import re

from .extraction_v2 import (
    _children, _descendants, _modifiers, _preposition_objects,
    _roles, _subject_roots,
)

NEGATION = re.compile(r"\b(?:not|no|never|neither|without|cannot|absence|lack|lacks|lacking|unable|failed|fails|failure)\b", re.I)
SPECULATION = re.compile(r"\b(?:may|might|could|possible|possibly|perhaps|potential|potentially|likely|unlikely|suggest|suggests|suggested|suggesting|seem|seems|appear|appears|hypothesis|hypothesize|hypothesized|speculate|presumably)\b", re.I)
BOUNDARY = re.compile(r";|\b(?:but|whereas|however|nevertheless)\b", re.I)


def resolve_gene_context(text, sentences, mentions):
    for mention in mentions:
        payload=mention.get('payload',{})
        symbol=payload.get('symbol')
        identifiers=payload.get('candidate_hgnc_ids',[])
        if (mention['kind']!='gene' or not payload.get('ambiguous') or payload.get('abbreviation_conflict')
                or not payload.get('hgnc_verified') or symbol!=mention['text']
                or len(payload.get('candidate_symbols',[]))!=1 or len(identifiers)!=1):
            continue
        line=next((s['text'] for s in sentences if s['start']<=mention['start'] and mention['end']<=s['end']),None)
        if not line:
            continue
        pattern=r'\b(?:inhibition|activation|expression|activity|induction)\s+of\s+'+re.escape(symbol)+r'\b|\b'+re.escape(symbol)+r'\s+(?:inhibitor|antagonist|expression|activity|activation)\b'
        if re.search(pattern,line,re.I):
            payload.update({'ambiguous':False,'context_resolution_rule':'exact-approved-symbol-biological-noun-v3'})
            if payload.get('species')=='human':
                mention['normalized_id']=identifiers[0]
                mention['status']='candidate'
            else:
                mention['status']='candidate_species_unresolved'
    return mentions


def _anchor(tokens, mention):
    covered = {t['i'] for t in tokens if t['start'] < mention['end'] and mention['start'] < t['end']}
    roots = [tokens[i] for i in covered if tokens[i]['head'] not in covered or tokens[i]['head'] == i]
    return roots[0] if len(roots) == 1 else None


def argument(tokens, root, mentions, exclude_conj=False):
    indexes = _descendants(tokens, root['i'], {'ccomp', 'advcl', 'relcl', 'acl', 'parataxis'} | ({'conj'} if exclude_conj else set()))
    covered = [m for m in mentions if (anchor := _anchor(tokens, m)) and anchor['i'] in indexes
               and all(t['i'] in indexes for t in tokens if t['start'] < m['end'] and m['start'] < t['end'])]
    # Nested ontology/gene mentions resolve to the maximal exact span.
    covered = [m for m in covered if not any(n is not m and n['start'] <= m['start'] and m['end'] <= n['end']
                                            and n['end']-n['start'] > m['end']-m['start'] for n in covered)]
    if len(covered) <= 1:
        return covered, None, covered
    anchors = [_anchor(tokens, m) for m in covered]
    first = anchors[0]
    family = _descendants(tokens, first['i'], {'prep', 'acl', 'relcl'})
    if all(a['i'] == first['i'] or (a['dep'] == 'conj' and a['i'] in family) for a in anchors):
        return covered, None, covered
    return [], 'ambiguous_argument_subtree', covered


def roles(tokens, trigger, kind, mentions):
    if trigger.get('pos') == 'NOUN' and kind not in {'association', 'no_effect'}:
        themes = _preposition_objects(tokens, trigger, {'of', 'on', 'with', 'to'})
        controllers = _preposition_objects(tokens, trigger, {'by'})
        # "A inhibition of B" has an explicit nominal compound/possessive A.
        if not controllers:
            controllers = [t for t in _children(tokens, trigger['i']) if t['dep'] in {'poss', 'compound'}]
        if not themes and trigger['lemma'] in {'inhibitor', 'antagonist', 'agonist'}:
            themes = [t for t in _children(tokens, trigger['i']) if t['dep'] == 'compound']
            parent = tokens[trigger['head']]
            controllers = [parent] if trigger['dep'] in {'appos', 'attr', 'dobj', 'compound'} else controllers
        if controllers and themes:
            values, reasons, possible = [], [], []
            for roots in (controllers, themes):
                participants = []
                for root in roots:
                    found, reason, candidates = argument(tokens, root, mentions, exclude_conj=root['i'] == trigger['head'])
                    participants += found
                    possible += candidates
                    if reason:
                        reasons.append(reason)
                values.append(participants)
            if not all(values):
                reasons.append('role_has_no_supported_entity')
            return *values, sorted(set(reasons)), possible
    left, right, reasons, candidates = _roles(tokens, trigger, kind, mentions)
    if not reasons:
        return left, right, reasons, candidates
    if trigger.get('pos') not in {'VERB', 'AUX'}:
        return left, right, reasons, candidates
    subjects = _subject_roots(tokens, trigger)
    if not subjects and trigger['dep']=='xcomp':
        parent=tokens[trigger['head']]
        if parent['lemma'] in {'show','find','demonstrate','observe'}:
            subjects=_subject_roots(tokens,parent)
    objects = [t for t in _children(tokens, trigger['i']) if t['dep'] in {'dobj', 'obj', 'attr'}]
    passive = any(t['dep'].endswith('pass') for t in subjects) or any(t['dep']=='auxpass' for t in _children(tokens, trigger['i']))
    if passive and kind != 'association':
        controllers, themes = _preposition_objects(tokens, trigger, {'by'}), subjects
    else:
        controllers, themes = subjects, objects or _preposition_objects(tokens, trigger, {'with','to','on'})
    # Shared object in "A induces and activates B" needs an explicit conjunct edge.
    if not themes:
        for child in _children(tokens, trigger['i']):
            if child['dep']=='conj' and child.get('pos')=='VERB' and not any(t['dep'] in {'nsubj','nsubjpass','csubj'} for t in _children(tokens,child['i'])):
                themes += [t for t in _children(tokens, child['i']) if t['dep'] in {'dobj','obj'}]
    values, failures, possible = [], [], []
    for roots in (controllers, themes):
        participants=[]
        for root in roots:
            found, reason, source=argument(tokens, root, mentions)
            participants += found
            possible += source
            if reason:
                failures.append(reason)
        values.append(participants)
    if not controllers or not themes:
        failures.append('missing_explicit_dependency_role')
    if not all(values):
        failures.append('role_has_no_supported_entity')
    return *values, sorted(set(failures)), possible


def scopes(sentence, tokens):
    values=[]
    for pattern, kind in [(NEGATION,'negation'),(SPECULATION,'speculation')]:
        for match in pattern.finditer(sentence['text']):
            start, end=sentence['start']+match.start(), sentence['start']+match.end()
            cue_tokens=[t for t in tokens if t['start'] < end and start < t['end']]
            if not cue_tokens:
                continue
            cue=cue_tokens[0]
            head=tokens[cue['head']] if cue['dep'] in {'neg','aux','advmod','det','amod','mark'} else cue
            indexes=_descendants(tokens,head['i'],{'parataxis','conj'} if kind=='negation' else {'parataxis'})
            right=max(end,max(tokens[i]['end'] for i in indexes))
            for boundary in BOUNDARY.finditer(sentence['text']):
                if boundary.start() >= match.end():
                    right=min(right,sentence['start']+boundary.start())
                    break
            while right > end and sentence['text'][right-sentence['start']-1] in ' \t\r\n.,;:':
                right-=1
            values.append({'kind':kind,'cue_start':start,'cue_end':end,'start':start,'end':right,
                           'text':sentence['text'][match.start():right-sentence['start']],
                           'rule_id':'cue-root-subtree-v3'})
    return values


def modifiers(sentence, tokens, trigger, scope, kind):
    direction, negation, speculation, significance=_modifiers(sentence,tokens,trigger,scope,kind)
    linked=[s for s in scopes(sentence,tokens) if s['start'] <= trigger['start'] and trigger['end'] <= s['end']]
    if significance != 'not_significant' and kind != 'no_effect':
        negation |= any(s['kind']=='negation' for s in linked)
    # Sentence-wide unrelated hedge words no longer modify every event.
    speculation=any(s['kind']=='speculation' for s in linked)
    return direction,negation,speculation,significance
