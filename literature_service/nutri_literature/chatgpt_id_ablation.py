"""Predeclared ID-only ablations of the original200 cached model responses.

No new inference or relation generation: compare missing-ID repair and full ID
revalidation separately. These diagnostic arms never select the new main policy.
"""
from copy import deepcopy
import json
from pathlib import Path

from .chatgpt_extraction import project, valid_id, exact_span
from .chatgpt_grounding import Catalog, select
from .chatgpt_evaluation import _contract, _read, _settings, _metric, _bootstrap
from .chatgpt_tuning import ROOT, HOME, PRIOR, MODELS
from .contract_v7 import canonical, immutable, sha
from . import benchmarks, evaluation_v6


def restore_ids(source, original, catalog, model, mode):
    result = deepcopy(original)
    changes = []
    for i, relation in enumerate(result['relations']):
        identities = []
        for kind in ('chemical','gene'):
            endpoint = relation['endpoints'][kind]
            previous = endpoint.get('native_id')
            selected = previous if valid_id(kind,previous) else None
            action = 'kept_existing'
            if mode == 'revalidate_all' or selected is None:
                rows = catalog.candidates(endpoint,source) if exact_span(source,endpoint) else []
                proposed = select(rows,model)
                selected = proposed['native_id'] if proposed else None
                action = ('filled_missing' if not valid_id(kind,previous) and selected else
                          'changed_existing' if selected and selected != previous else
                          'confirmed_existing' if selected else 'unresolved')
            old_identifier = relation['entity1' if kind == 'chemical' else 'entity2']
            placeholder = ('UNRESOLVED_'+kind.upper()+':'+str(endpoint.get('key', 'relation-'+str(i)))
                           if valid_id(kind,previous) else old_identifier)
            identifiers = selected or placeholder
            identities.append(identifiers)
            changes.append({'relation_index':i,'kind':kind,'previous_id':previous,'selected_id':selected,'action':action})
        relation['entity1'],relation['entity2'] = identities
        direction = relation.get('role_direction')
        if direction in {'chemical_to_gene','gene_to_chemical'}:
            relation['subject'] = identities[int(direction == 'gene_to_chemical')]
    result['id_changes'] = changes
    return result


def main():
    output = HOME/'cached-id-ablations'
    identity = _read(HOME/'identity.json')
    catalog = Catalog.load(ROOT/'runtime/chatgpt-inputs/official-id-dictionary.json.gz')
    contracts = {}
    for model, effort in MODELS:
        folder = PRIOR/('gpt-6.1-sol-medium-0fd4d4e0' if model == 'gpt-6.1-sol' else 'gpt-6-luna-max-ad90ee79')
        contract, checked_dictionary = _contract(folder)
        del checked_dictionary
        contracts[model] = {'folder':folder,'contract':contract}
    manifest = {'schema':'chatgpt-cached-id-ablation-v1','arms':['fill_missing','revalidate_all'],
                'identity_sha256':sha((HOME/'identity.json').read_bytes()),'dictionary_sha256':catalog.sha256,
                'source_sha256':sha(Path(__file__).read_bytes()),
                'original_contracts':{m:sha(canonical(r['contract'])) for m,r in contracts.items()},
                'model_calls':0,'policy_selection_uses_these_development_results':False,
                'scorer_sha256':benchmarks.evaluator_source_hashes()}
    immutable(output/'contract.json',manifest)
    all_predictions = {}
    for model,entry in contracts.items():
        all_predictions[model] = {name:{} for name in manifest['arms']}
        for pmid in entry['contract']['source_hashes']:
            name = 'paper-'+pmid+'.json'
            source = _read(entry['folder']/'sources'/name)
            raw = _read(entry['folder']/'responses'/name)
            reconstructed = project(source['text'],raw['text'])['chatgpt_only']
            expected_old = _read(entry['folder']/'predictions'/name)['chatgpt_only']
            if canonical(reconstructed['relations']) != canonical(expected_old['relations']):
                raise ValueError('Original standalone reprojection differs')
            old = expected_old
            for mode in manifest['arms']:
                prediction = restore_ids(source['text'],old,catalog,identity['model'],mode)
                immutable(output/model/mode/name,{'pmid':pmid,'source_sha256':source['source_hash'],
                          'original_response_sha256':sha(canonical(raw)),**prediction})
                all_predictions[model][mode][pmid] = prediction
    # All source-only projections are durable before re-opening development gold.
    locator = _read(PRIOR/'gpt-6.1-sol-medium-0fd4d4e0/scoring-locator.json')
    gold,_,gold_hash = benchmarks._load_gold(_settings(ROOT/'runtime/chatgpt-inputs'),locator['gold_path'])
    labels = {d['pmid']:d for d in gold['documents']}
    result = {'papers':100,'gold_relations':154,'gold_sha256':gold_hash,'arms':{},
              'contract_sha256':sha(canonical(manifest)),'new_model_calls':0,
              'evaluation_scope':'diagnostic exposed development only; not main policy selection'}
    for model,arms in all_predictions.items():
        prior = _read(contracts[model]['folder']/'comparison.json')
        for mode,predictions in arms.items():
            per_paper = {}
            for pmid,prediction in predictions.items():
                expected,actual,_,_ = evaluation_v6.relation_sets(labels[pmid],prediction)
                per_paper[pmid] = benchmarks._metrics(expected,actual)
            metric = _metric(list(per_paper.values()))
            result['arms'][model+':'+mode] = {'metrics':metric,'per_paper':per_paper,
                'vs_original_ci95':_bootstrap({p['pmid']:p['chatgpt_only']['relation'] for p in prior['per_paper']},per_paper,20261017)}
    immutable(output/'comparison.json',result)
    print(json.dumps({name:r['metrics'] for name,r in result['arms'].items()},ensure_ascii=False))


if __name__ == '__main__':
    main()
