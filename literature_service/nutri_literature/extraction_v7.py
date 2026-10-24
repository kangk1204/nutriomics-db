"""V7 relation hypotheses with direct-rule checks; v6 clinical output retained."""
from copy import deepcopy
import json
from pathlib import Path
from .contract_v7 import ENGINE, sources, sha, canonical
from .learning_v7 import selected


def head_metadata(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_bytes())
    if 'weights' in manifest:
        weights=directory/'selected/weights.pt'
        if sha(weights.read_bytes())!=manifest['weights']['weights_sha256']:
            raise ValueError('V7 joint weights changed')
        for name,checksum in manifest['sources'].items():
            if sha(Path(__file__).with_name(name).read_bytes())!=checksum:
                raise ValueError('V7 joint source changed')
        manifest={**manifest,'model_sha256':manifest['weights']['weights_sha256'],'joint':True}
        return manifest
    if sha((directory / 'relations.joblib').read_bytes()) != manifest['model_sha256']:
        raise ValueError('V7 relation weights changed')
    for name, checksum in manifest['sources'].items():
        if sha(Path(__file__).with_name(name).read_bytes()) != checksum:
            raise ValueError('V7 training source changed: ' + name)
    return manifest


def load_head(directory):
    directory=Path(directory);manifest=head_metadata(directory)
    if manifest.get('joint'):
        import torch
        from .joint_training_v7 import JointModel
        model=JointModel(manifest['base_path'],manifest['vocab'],manifest['adapted'])
        model.restore(torch.load(directory/'selected/weights.pt',map_location='cpu',weights_only=True))
        return model,manifest
    import joblib
    return joblib.load(directory/'relations.joblib'),manifest


def extract(text, sections, dictionaries, metadata=None):
    from .extraction_v6 import extract as legacy, native_event, group_events
    from .automatic_features_v6 import bags_for
    from .scope_v5 import predict as scope
    metadata = dict(metadata or {})
    if '_v7_model_path' not in metadata:
        raise ValueError('V7 relation artifact is missing; no fallback extraction is marked successful')
    head, model_manifest = metadata.get('_v7_loaded_head') or load_head(metadata['_v7_model_path'])
    base = legacy(text, sections, dictionaries, metadata)
    if metadata.get('_gold_entities') is None:
        from .registry_v6 import engine_manifest_validation as upstream_validation
        reasons=upstream_validation(base,'heuristic-v6')
        if reasons:raise ValueError('V7 upstream validation failed: '+','.join(reasons))
    mentions = deepcopy(base['mentions'])
    normalizations = []
    if metadata.get('_v7_identity') and not metadata.get('_gold_entities'):
        from .identity_artifact_v7 import apply_identity
        mentions, normalizations = apply_identity(mentions,text,base['sentence_roles'],metadata,metadata['_v7_identity'])
        by_span={(h['start'],h['end']):h for h in normalizations}
        for mention in mentions:
            h=by_span.get((mention['start'],mention['end']))
            if h is not None:
                mention.setdefault('payload',{}).update(candidate_ids=[c['id'] for c in h['candidates']],
                    taxon_compatible_candidate_ids=[c['id'] for c in h['candidates'] if c['taxon_id']==h['eligibility'].get('taxon_id')])
    document = {'text': text, 'sections': sections}
    bags = bags_for(document, mentions, base['sentence_roles'], metadata)
    if model_manifest.get('joint'):
        from .joint_data_v7 import runtime_document
        from .nlp_v2 import cpu_inference_lock
        document=runtime_document(text,sections,mentions)
        with cpu_inference_lock():
            scores=head.predict(document)
        if len(scores)!=len(bags):raise ValueError('Train/runtime mention bag layout mismatch')
    else:
        scores = head.predict(bags)
    scopes = scope(base['sentence_roles'], metadata['_learned_models'])
    hypotheses, events = [], []
    for bag, row in zip(bags, scores):
        direction = max(row['direction'], key=row['direction'].get) if row['direction'] else 'unknown'
        if row['direction'].get(direction, 0) < .7:
            direction = 'unknown'
        for label in selected(row, model_manifest['selection']['threshold']):
            checked = [(p, *native_event(p, label, direction, scopes, metadata, mentions)) for p in bag['pairs']]
            pair, event, reasons = min(checked, key=lambda x: (x[1] is None, len(x[2]), x[0]['sentence']['start']))
            resolved=all(pair[k].get('normalized_id') for k in ('chemical','gene'))
            h = {'chemical': pair['chemical'], 'gene': pair['gene'], 'benchmark_relation_type': label,
                 'role_direction': direction, 'type_selected': resolved, 'review_proposed': True,
                 'automatic_gate_reasons':[] if resolved else ['endpoint_identity_unresolved'],
                 'priority_score': row[label] * row['valid'], 'existence_priority_score': row['exists'],
                 'semantic_verified': event is not None and not reasons, 'reasons': reasons,
                 'evidence': {'start': pair['sentence']['start'], 'end': pair['sentence']['end'], 'quote': pair['sentence']['text']},
                 'identity_support': 'resolved' if all(pair[k].get('normalized_id') for k in ('chemical', 'gene')) else 'unresolved',
                 'source_support': 'legacy_rule_passed' if event is not None and not reasons else 'document_hypothesis',
                 'scores_are_nutrition_probabilities': False, 'human_approved': False}
            hypotheses.append(h)
            if event is not None:
                event['engine_version'] = ENGINE
                events.append(event)
    events.extend(e for e in base['events'] if e.get('support_scope') in {
        'rule_only_outside_learned_scope','adjacent_explicit_reference_review_only'})
    unique = list(group_events(events).values())
    assertions = [a for a in base['assertions'] if a.get('payload', {}).get('observation')]
    for e in unique:
        if e.get('assertion_state') != 'observed':
            continue
        predicate = {'increase': 'increases', 'decrease': 'decreases', 'association': 'associated_with'}.get(e['direction'],
                    'binds' if e['relation_type'] == 'binding' else 'affects')
        assertions.append({'subject': e['subject'], 'object': e['object'], 'predicate': predicate,
            **e['evidence_quotes'][0], 'rule_id': e['rule_id'], 'payload': {'event': e, 'explicit_relation': True,
             'subject_mention': e['controller'], 'object_mention': e['theme'], 'rule_score': 1,
             'triage_reasons': e.get('triage_reasons', [])}})
    hashes, rules = sources()
    manifest = {'engine_version': ENGINE, 'rules_version': rules, 'rule_source_hashes': hashes,
                'relation_model_sha256': model_manifest['model_sha256'], 'upstream_manifest': base['manifest'],
                'dictionary_version':base['dictionary_version'],
                'profile': 'local', 'oracle': metadata.get('_gold_entities') is not None,
                'clinical_engine_version': 'heuristic-v6', 'nutrition_accuracy': 'unmeasured'}
    manifest['artifact_identity'] = sha(canonical(manifest))
    if metadata.get('_v7_identity'):
        manifest['identity_model_manifest_sha256']=metadata['_v7_identity'][1]['manifest_sha256']
        manifest['identity_adoption']=metadata['_v7_identity'][1]['adoption']
        manifest['artifact_identity']=sha(canonical({k:v for k,v in manifest.items() if k!='artifact_identity'}))
    return {**base, 'engine_version': ENGINE, 'rules_version': rules, 'manifest': manifest, 'mentions': mentions,
            'events': unique, 'assertions': assertions, 'relation_hypotheses': hypotheses,
            'normalization_candidates': normalizations or base['normalization_candidates'],
            'module_validation': {**base['module_validation'], 'relations': 'v7_candidate_beta', 'clinical_engine': 'heuristic-v6'},
            'decision_trace': [{'meaning': h['source_support'], 'identity': h['identity_support'], 'reasons': h['reasons']} for h in hypotheses]}
