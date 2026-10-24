"""Official train-only mention relations and event annotations, separate tasks."""
from collections import defaultdict
import json
from pathlib import Path
import re
import zipfile
from .contract_v7 import immutable, sha
from .splits_v7 import text_key


DRUGPROT_SHA = '679ca89698ca471a7fedc9af95acc8722c127c04eba19660fead406b5b9b52c9'


def read_drugprot(archive):
    raw = Path(archive).read_bytes()
    if sha(raw) != DRUGPROT_SHA:
        raise ValueError('DrugProt official archive hash differs')
    with zipfile.ZipFile(archive) as z:
        members = [n for n in z.namelist() if '/training/' in n and n.endswith('.tsv')]
        def lines(term):
            matches = [n for n in members if term in n]
            if len(matches) != 1:
                raise ValueError('Official DrugProt train member missing: ' + term)
            return z.read(matches[0]).decode('utf-8').splitlines()
        docs = {}
        for line in lines('abstract') if any('abstract' in n for n in members) else lines('abstrac'):
            pmid, title, abstract = line.split('\t')
            text = title + '\n' + abstract
            docs[pmid] = {'pmid': pmid, 'text': text, 'entities': [], 'relations': [],
                          'sections': [{'start': 0, 'end': len(text), 'label': 'abstract'}],
                          'task': 'drugprot', 'split': 'train', 'relation_annotation_complete': True}
        for line in lines('entities'):
            pmid, entity_id, entity_type, start, end, text = line.split('\t')
            start, end = int(start), int(end)
            if docs[pmid]['text'][start:end] != text:
                raise ValueError('DrugProt mention/source mismatch')
            docs[pmid]['entities'].append({'annotation_id': entity_id, 'kind': 'component' if entity_type == 'CHEMICAL' else 'gene',
                                         'start': start, 'end': end, 'text': text, 'native_type': entity_type})
        for line in lines('relations'):
            pmid, relation, a, b = line.split('\t')
            docs[pmid]['relations'].append({'type': relation, 'arg1': a.split(':')[1], 'arg2': b.split(':')[1],
                                          'argument_order_is_causal': False})
    return list(docs.values())


def parse_standoff(text, a1, a2):
    spans, events, modifications = {}, {}, []
    for line in (a1 + '\n' + a2).splitlines():
        if not line.strip():
            continue
        identifier, body, *literal = line.split('\t')
        if identifier.startswith('T'):
            label, offsets = body.split(' ', 1)
            parts = [tuple(map(int, s.split())) for s in offsets.split(';')]
            value = ' '.join(text[start:end] for start, end in parts)
            if literal and value != literal[0]:
                raise ValueError('CG standoff quotation differs from source')
            spans[identifier] = {'annotation_id': identifier, 'native_type': label, 'text': value,
                                 'spans': [{'start': start, 'end': end, 'text': text[start:end]} for start, end in parts]}
        elif identifier.startswith('E'):
            first, *args = body.split()
            label, trigger = first.split(':', 1)
            events[identifier] = {'annotation_id': identifier, 'type': label, 'trigger': trigger,
                                  'arguments': [{'role': x.split(':', 1)[0], 'target': x.split(':', 1)[1]} for x in args]}
        elif identifier.startswith(('M', 'A')):
            values = body.split()
            modifications.append({'type': values[0], 'target': values[1]})
    for event in events.values():
        if event['trigger'] not in spans:
            raise ValueError('CG trigger reference missing')
        for argument in event['arguments']:
            if argument['target'] not in spans and argument['target'] not in events:
                raise ValueError('CG argument reference missing')
    return {'spans': spans, 'events': events, 'modifications': modifications}


def read_cg(directory):
    directory = Path(directory)
    docs = []
    for f in sorted(directory.rglob('*.txt')):
        if not any('train' in part.casefold() for part in f.relative_to(directory).parts[:-1]):
            continue
        text = f.read_text(encoding='utf-8')
        annotations = parse_standoff(text, f.with_suffix('.a1').read_text(encoding='utf-8'), f.with_suffix('.a2').read_text(encoding='utf-8'))
        match = re.fullmatch(r'(?:PMID-)?(\d+)', f.stem)
        if not match:
            raise ValueError('CG PMID mapping unresolved: ' + f.stem)
        docs.append({'pmid': match[1], 'text': text, 'task': 'cg', 'split': 'train', **annotations})
    if not docs:
        raise ValueError('No official CG training source found')
    return docs


def prepare(drugprot, cg, prior_audit, protected_inputs, out):
    audit = json.loads(Path(prior_audit).read_bytes())
    if not audit.get('passed') or audit.get('protected_overlap') != 0:
        raise ValueError('Prior protected-cohort audit unavailable')
    protected_ids = set(map(str, audit['protected_pmids']))
    protected_hashes = set(audit.get('protected_text_hashes', []))
    for p in protected_inputs:
        value = json.loads(Path(p).read_bytes())
        for doc in value['documents']:
            protected_ids.add(str(doc['pmid']))
            protected_hashes.add(text_key(doc['text']))
    out = Path(out)
    result = {'split': 'train', 'test_labels_read': False, 'nutrition_accuracy': 'unmeasured', 'tasks': {}, 'excluded': []}
    for name, docs in [('drugprot', read_drugprot(drugprot)), ('cg', read_cg(cg))]:
        kept = []
        for doc in docs:
            reason = 'protected_pmid' if doc['pmid'] in protected_ids else 'protected_text' if text_key(doc['text']) in protected_hashes else None
            if reason:
                result['excluded'].append({'task': name, 'pmid': doc['pmid'], 'reason': reason})
            else:
                kept.append(doc)
        target = immutable(out / (name + '-train.json'), {'split': 'train', 'task': name, 'documents': kept})
        result['tasks'][name] = {'papers': len(kept), 'sha256': sha(target.read_bytes()), 'path': target.name}
    result['protected_pmids'] = sorted(protected_ids)
    result['prior_audit_sha256'] = sha(Path(prior_audit).read_bytes())
    result['pmcid_audit'] = 'absent PMCID remains unknown; PMID and normalized-source comparisons applied'
    immutable(out / 'manifest.json', result)
    return result
