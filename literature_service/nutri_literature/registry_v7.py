"""Additive relation engine; legacy scientific implementations are unchanged."""
import json
from . import registry_v6 as old
from .contract_v7 import ENGINE, sources, sha, canonical

ENGINE_CHOICES = old.ENGINE_CHOICES + (ENGINE,)


def validate_engine(engine):
    if engine not in ENGINE_CHOICES:
        raise ValueError('Unknown extraction engine: ' + str(engine))
    return engine


def engine_rules_version(engine):
    return sources()[1] if validate_engine(engine) == ENGINE else old.engine_rules_version(engine)


def validate_source_spans(result, text):
    old.validate_source_spans({**result, 'engine_version': 'heuristic-v6'} if result.get('engine_version') == ENGINE else result, text)


def engine_manifest_validation(result, engine, settings=None):
    if engine != ENGINE:
        return old.engine_manifest_validation(result, engine, settings)
    if not isinstance(result, dict):
        return ['malformed_extraction']
    reasons, manifest = [], result.get('manifest', {})
    if result.get('engine_version') != ENGINE:
        reasons.append('engine_changed')
    if result.get('rules_version') != sources()[1]:
        reasons.append('rules_changed')
    if manifest.get('oracle'):
        reasons.append('oracle_experiment')
    if manifest.get('profile') != 'local':
        reasons.append('unsupported_input_profile')
    if not manifest.get('relation_model_sha256') or not manifest.get('artifact_identity'):
        reasons.append('v7_model_identity_missing')
    if not manifest.get('upstream_manifest'):
        reasons.append('upstream_provenance_missing')
    if settings:
        path = settings.home / 'engine-artifacts/heuristic-v7.json'
        try:
            saved = json.loads(path.read_bytes())
            if saved != manifest:
                reasons.append('model_artifact_changed')
        except (OSError, ValueError):
            reasons.append('artifact_record_missing')
    return sorted(set(reasons))


def extract_engine(text, sections, dictionaries, metadata=None, engine='heuristic-v1'):
    validate_engine(engine)
    if engine != ENGINE:
        return old.extract_engine(text, sections, dictionaries, metadata, engine)
    from .extraction_v7 import extract
    result = extract(text, sections, dictionaries, metadata)
    validate_source_spans(result, text)
    return result


def default_engine(settings):
    return old.default_engine(settings)


def switch_engine(settings, engine, promotion=None):
    if engine == ENGINE:
        raise ValueError('V7 relation-only development cannot promote the whole clinical service')
    return old.switch_engine(settings, engine, promotion)
