"""Source-only v7 inference, then unchanged v6 native-ID scoring."""
import json
from pathlib import Path
from copy import deepcopy
from .contract_v7 import canonical, sha, immutable
from .learning_v7 import selected


def development(home, model_path, baseline, upstream, output, mode='end_to_end',identity_path=None):
    import os
    from .settings import Settings
    from .operations_v6 import components
    from .fast_frozen_v6 import SentenceMemoEncoder
    from .automatic_features_v6 import bags_for
    from .text import sentence_spans
    from . import evaluation_v6, benchmarks as scoring
    from .mapping_v7 import native_mapping
    os.environ['LITERATURE_V6_MODEL_HOME'] = str(upstream.resolve())
    home, model_path, baseline, output = map(Path, (home, model_path, baseline, output))
    settings = Settings(home, Path(__file__).parents[1] / 'config/topics.json')
    from .extraction_v7 import load_head,head_metadata
    model,manifest=None,head_metadata(model_path)
    index = json.loads((home / 'public_benchmarks/v2-measurement-index.json').read_bytes())
    prepared = index['steps']['prepare:development:bioredirect:abstract']
    inputs, _, input_sha = scoring._load_inputs(settings, prepared['inputs_path'])
    docs = sorted(inputs['documents'], key=lambda d: sha('v3-development-20261003:' + d['pmid']))[:100]
    if set(manifest['training_pmids']) & {d['pmid'] for d in docs}:
        raise ValueError('Training/development overlap')
    # A resumed score-only pass must not reload several GiB of encoders.
    # Every cached prediction is still bound to the exact model/input contract.
    values = None
    identity = {'model_manifest_sha256': sha((model_path / 'manifest.json').read_bytes()),
                'input_sha256': input_sha, 'evaluator_sha256': sha(Path(evaluation_v6.__file__).read_bytes()), 'mode': mode}
    if manifest.get('joint'):
        # The legacy separated baseline remains immutable. New joint profiles
        # additionally bind every inference implementation and cached endpoint.
        from .extraction_v6 import source_manifest
        identity['inference_sources']={n:sha(Path(__file__).with_name(n).read_bytes()) for n in (
            'evaluation_v7.py','extraction_v7.py','identity_artifact_v7.py','mapping_v7.py','learning_v7.py')}
        identity['upstream_rules_version']=source_manifest()[1]
        identity['baseline_endpoint_sha256']={d['pmid']:sha((baseline/('paper-'+d['pmid']+'.json')).read_bytes()) for d in docs}
    if identity_path is not None:
        if mode!='end_to_end':raise ValueError('ID revalidation belongs only to automatic evaluation')
        identity['identity_model_manifest_sha256']=sha((Path(identity_path)/'manifest.json').read_bytes())
        identity['id_stack_scope']='development_diagnostic_not_nested_joint_selection'
    immutable(output / 'prediction-contract.json', identity)
    predictions = {}
    for i, doc in enumerate(docs):
        target = output / ('paper-' + doc['pmid'] + '.json')
        if target.exists():
            pred = json.loads(target.read_bytes())
            if pred['identity'] != identity:
                raise ValueError('Prediction identity changed')
        else:
            if model is None:model,manifest=load_head(model_path)
            if values is None:
                values=components(settings,'union')
                values['_encoder']=SentenceMemoEncoder(values['_encoder'])
                if identity_path is not None:
                    from .identity_artifact_v7 import load_identity
                    loaded=load_identity(identity_path,values)
                    if set(loaded[1]['training_pmids']) & {d['pmid'] for d in docs}:
                        raise ValueError('ID training/development overlap')
                    values['_v7_identity']=loaded
            original = json.loads((baseline / target.name).read_bytes())
            if original['source_hash'] != sha(doc['text']):
                raise ValueError('Baseline/source mismatch')
            mentions = deepcopy(original['entities'])
            sentences = values['_clinical_models'].predict(sentence_spans(doc['text'], doc['sections']))
            normalization_candidates=[]
            if values.get('_v7_identity'):
                from .identity_artifact_v7 import apply_identity
                mentions,normalization_candidates=apply_identity(mentions,doc['text'],sentences,values,values['_v7_identity'])
            bags = bags_for(doc, mentions, sentences, values)
            if manifest.get('joint'):
                from .joint_data_v7 import runtime_document
                from .nlp_v2 import cpu_inference_lock
                source_pairs=runtime_document(doc['text'],doc['sections'],mentions)
                with cpu_inference_lock():
                    rows=model.predict(source_pairs)
            else:
                rows = model.predict(bags)
            if len(rows)!=len(bags):raise ValueError('Runtime bag layout differs')
            from .scope_v5 import predict as predict_scope
            from .extraction_v6 import native_event
            scopes=predict_scope(sentences,values['_learned_models'])
            relations, hypotheses = [], []
            for bag, row in zip(bags, rows):
                labels = selected(row, manifest['selection']['threshold'])
                directions = row['direction']
                direction = max(directions, key=directions.get) if directions else 'unknown'
                if directions.get(direction, 0) < .7:
                    direction = 'unknown'
                for label in labels:
                    checked=[(p,*native_event(p,label,direction,scopes,values,mentions)) for p in bag['pairs']]
                    pair,event,reasons=min(checked,key=lambda x:(x[1] is None,len(x[2]),x[0]['sentence']['start']))
                    sentence=pair['sentence']
                    resolved=mode=='gold_entities' or all(pair[k].get('normalized_id') for k in ('chemical','gene'))
                    h = {'chemical': pair['chemical'], 'gene': pair['gene'], 'benchmark_relation_type': label,
                         'role_direction': direction, 'type_selected': resolved, 'review_proposed': True,
                         'automatic_gate_reasons':[] if resolved else ['endpoint_identity_unresolved'],
                         'semantic_verified': event is not None and not reasons, 'priority_score': row[label] * row['valid'],
                         'reasons': reasons,
                         'evidence': {'start': sentence['start'], 'end': sentence['end'], 'quote': sentence['text']}}
                    hypotheses.append(h)
            relations, verified = evaluation_v6.project_hypotheses({'relation_hypotheses': hypotheses}, native_mapping(mentions), mode == 'gold_entities')
            pred = {'pmid': doc['pmid'], 'source_hash': sha(doc['text']), 'identity': identity,
                    'entities': mentions, 'relations': relations, 'verified_relations': verified,
                    'relation_hypotheses': hypotheses, 'candidate_pairs': [[p['chemical'], p['gene']] for b in bags for p in b['pairs']]}
            if identity_path is not None:pred['normalization_candidates']=normalization_candidates
            immutable(target, pred)
        predictions[doc['pmid']] = pred
        print(canonical({'stage': 'v7-development-inference', 'mode': mode, 'papers': i + 1}), flush=True)
    # Gold enters only after all source-only predictions are durably written.
    gold, _, gold_sha = scoring._load_gold(settings, prepared['gold_path'])
    labels = {d['pmid']: d for d in gold['documents']}
    rows = []
    for doc in docs:
        pred = predictions[doc['pmid']]
        metrics = evaluation_v6._metrics_for(labels[doc['pmid']], pred, native_mapping(pred['entities']), mode == 'gold_entities', 'bioredirect')
        rows.append({'pmid': doc['pmid'], **metrics})
    report = {'id': output.name, 'engine': 'heuristic-v7', 'mode': mode, 'papers': len(rows),
              'metrics': {k: scoring._aggregate(rows, k) for k in ('relation', 'relation_direction', 'verified_relation')},
              'per_paper': rows, 'input_sha256': input_sha, 'gold_sha256': gold_sha,
              'scientific_targets_met': False, 'nutrition_accuracy': 'unmeasured', 'sealed_test_opened': False,
              'source_only_predictions_before_gold': True, 'identity': identity}
    report['prepared_inputs_path']=str(Path(prepared['inputs_path']).resolve())
    report['prepared_gold_path']=str(Path(prepared['gold_path']).resolve())
    immutable(output / 'report.json', report)
    return report
