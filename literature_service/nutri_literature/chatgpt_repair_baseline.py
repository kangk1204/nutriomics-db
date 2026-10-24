"""Reproduce the frozen four-call reference algorithm on official training only."""
import json
from pathlib import Path

from . import chatgpt_tuning as old, chatgpt_joint_verifier as reference
from .chatgpt_grounding import Catalog, source_payload
from .chatgpt_extraction import source_input, INSTRUCTIONS, project as original_project, exact_span, valid_id
from .chatgpt_evaluation import _read, _metric
from .contract_v7 import canonical, immutable, sha
from .chatgpt_repair_io import HOME, ROOT, MODELS, prepare_data, inference, phase_contract, score, log


class Builder(reference.ProposalBuilder):
    """Same merge semantics, independent filesystem; no mutation of old globals."""
    def build(self, source, split='train'):
        name = 'paper-' + source['pmid'] + '.json'
        payload = _read(HOME / 'reference/payloads' / name)
        model = self.identity['crossfit_models'][str(self.identity['folds'][source['pmid']])]
        raw = _read(HOME / 'reference/original/responses' / name)
        old_result = original_project(source['text'], raw['text'])['chatgpt_only']
        proposals, by_span = [], {}
        def mention(m, origin):
            if not m or m.get('kind') not in {'chemical', 'gene'} or not exact_span(source['text'], m):
                return None
            identity = m['kind'], m['start'], m['end']
            if identity not in by_span:
                from .chatgpt_grounding import rank
                by_span[identity] = {**{k: m[k] for k in ('kind', 'text', 'start', 'end')},
                                     'key': 'u' + str(len(by_span)),
                                     'candidates': rank(self.catalog.candidates(m, source['text']), model),
                                     'proposed_by': []}
            result = by_span[identity]
            result['proposed_by'].append(origin)
            proposed = m.get('native_id')
            if valid_id(m['kind'], proposed) and not any(r['native_id'] == proposed for r in result['candidates']):
                rows = self.by_id.get((m['kind'], proposed), [])
                if rows:
                    names = sorted({n for r in rows for n in r['names']}, key=lambda n: (-len(n.split()), -len(n), n))[:3]
                    taxa = {r.get('taxon') for r in rows}
                    result['candidates'].append({'native_id': proposed, 'taxon': next(iter(taxa)) if len(taxa) == 1 else None,
                                                 'names': names, 'priority': None, 'retrieval': 'model_proposed_official_record'})
            return result['key']
        for r in old_result['relations']:
            c, g = mention(r['endpoints']['chemical'], 'original_sol'), mention(r['endpoints']['gene'], 'original_sol')
            if c and g:
                proposals.append({'chemical': c, 'gene': g, 'type': r['predicate'], 'stream': 'original_sol'})
        for model_name, effort in MODELS:
            answer = _read(HOME / 'reference' / (model_name + '-' + effort) / 'responses' / name)
            try:
                value = json.loads(answer['text'])
                if not isinstance(value, dict) or not isinstance(value.get('relations'), list):
                    continue
            except (ValueError, TypeError):
                continue
            mentions = old._mentions(payload, value, self.catalog, model)
            for r in value['relations']:
                if not isinstance(r, dict):
                    continue
                left, right = mentions.get(r.get('chemical')), mentions.get(r.get('gene'))
                c = mention({**left, 'native_id': r.get('chemical_id')} if left else None, model_name)
                g = mention({**right, 'native_id': r.get('gene_id')} if right else None, model_name)
                if c and g:
                    proposals.append({'chemical': c, 'gene': g, 'type': r.get('benchmark_relation_type'), 'stream': model_name})
        mentions = sorted(by_span.values(), key=lambda m: int(m['key'][1:]))
        for m in mentions:
            m['candidates'] = [{k: r.get(k) for k in ('native_id', 'taxon', 'names', 'priority', 'retrieval')} for r in m['candidates']]
            m['proposed_by'] = sorted(set(m['proposed_by']))
        return {'source': source['text'], 'source_sha256': sha(source['text']), 'sentences': payload['sentences'],
                'mentions': mentions, 'proposals': proposals, 'candidates_are_automatic_not_gold': True}


def run(auth):
    docs, _ = prepare_data()
    catalog = Catalog.load(ROOT / 'runtime/chatgpt-inputs/official-id-dictionary.json.gz')
    identity = _read(old.HOME / 'identity.json')
    contract = phase_contract('reference', [Path(__file__), Path(__file__).with_name('chatgpt_repair_io.py')], {
        'original_reference_sources': old.source_hashes(),
        'reference_verifier_sha256': sha(Path(reference.__file__).read_bytes()),
        'identity_sha256': sha((old.HOME / 'identity.json').read_bytes()),
        'dictionary_sha256': catalog.sha256, 'reference_policy': _read(reference.HOME / 'selection.json')['selected']})
    for i, d in enumerate(docs):
        path = HOME / 'reference/payloads' / ('paper-' + d['pmid'] + '.json')
        if not path.exists():
            immutable(path, source_payload(d, catalog, identity, crossfit=True))
        if (i+1) % 50 == 0:
            log('reference-payloads', completed=i+1, total=len(docs))
    jobs = []
    for d in docs:
        name = 'paper-' + d['pmid'] + '.json'
        previous = reference.HOME / 'original-calibration'
        jobs.append((HOME / 'reference/original/responses' / name, 'gpt-6.1-sol', 'medium', INSTRUCTIONS,
                     source_input(d), {}, [(previous / 'responses' / name, previous / 'requests' / name)]))
        for model, effort in MODELS:
            prior = old.directory(model) / 'calibration'
            jobs.append((HOME / 'reference' / (model + '-' + effort) / 'responses' / name, model, effort,
                         old.INSTRUCTIONS, (HOME / 'reference/payloads' / name).read_text(encoding='utf-8'), {},
                         [(prior / 'responses' / name, prior / 'requests' / name)]))
    inference(jobs, 'reference-proposals', contract, auth)
    builder = Builder(catalog, identity)
    jobs = []
    for d in docs:
        name = 'paper-' + d['pmid'] + '.json'
        payload = builder.build(d)
        immutable(HOME / 'reference/joint-payloads' / name, payload)
        prior = reference.HOME / 'review-calibration'
        jobs.append((HOME / 'reference/review/responses' / name, 'gpt-6.1-sol', 'medium', reference.PROMPT,
                     canonical(payload), {}, [(prior / 'responses' / name, prior / 'requests' / name)]))
    inference(jobs, 'reference-review', contract, auth)
    predictions = {}
    policy = contract['reference_policy']
    for d in docs:
        name = 'paper-' + d['pmid'] + '.json'
        response = _read(HOME / 'reference/review/responses' / name)
        value = reference.project(_read(HOME / 'reference/joint-payloads' / name), response['text'], policy)
        immutable(HOME / 'reference/predictions' / name, value)
        predictions[d['pmid']] = value
    per_paper = score(docs, predictions)
    result = {'papers': len(docs), 'gold_relations': 549, 'metrics': _metric([r['relation'] for r in per_paper.values()]),
              'per_paper': per_paper, 'fixed_upstream_training_exposure': True}
    immutable(HOME / 'reference/comparison.json', result)
    log('reference-complete', metrics=result['metrics'])


if __name__ == '__main__':
    import os
    import psutil
    from .chatgpt_auth import ChatGPTAuth
    from .chatgpt_batch import batch_lock
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10)
    with batch_lock(old.PRIOR):
        run(ChatGPTAuth())
