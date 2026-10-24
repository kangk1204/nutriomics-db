"""Additive registry; frozen v1-v4 code and reviews remain reproducible."""

import json

from . import registry_v4 as old
from .db import digest, dumps

ENGINE_CHOICES = old.ENGINE_CHOICES + ("heuristic-v5",)
validate_source_spans = old.validate_source_spans


def validate_engine(engine):
    if engine not in ENGINE_CHOICES:
        raise ValueError("Unknown extraction engine: " + str(engine))
    return engine


def engine_rules_version(engine):
    validate_engine(engine)
    if engine == "heuristic-v5":
        from .extraction_v5 import source_manifest

        return source_manifest()[1]
    return old.engine_rules_version(engine)


def engine_manifest_validation(result, engine, settings=None):
    if engine != "heuristic-v5":
        return old.engine_manifest_validation(result, engine, settings)
    if not isinstance(result, dict):
        return ["malformed_extraction"]
    manifest = result.get("manifest", {})
    reasons = []
    if result.get("engine_version") != engine:
        reasons.append("engine_changed")
    if result.get("rules_version") != engine_rules_version(engine):
        reasons.append("rules_changed")
    if manifest.get("oracle"):
        reasons.append("oracle_experiment")
    if manifest.get("ablation") != "context":
        reasons.append("ablation_experiment")
    if manifest.get("profile") != "local":
        reasons.append("unsupported_input_profile")
    parser = manifest.get("parser", {})
    if parser.get("mode") != "production" or not parser.get("available"):
        reasons.append("nonproduction_parser")
    for name in ("candidate_provider", "clinical_models", "learned_models"):
        if not manifest.get(name, {}).get("available"):
            reasons.append(name + "_unavailable")
    if not manifest.get("gene_dictionary", {}).get("records_sha256"):
        reasons.append("native_gene_dictionary_missing")
    if not manifest.get("learned_models", {}).get("feature_sha256"):
        reasons.append("feature_identity_missing")
    if settings:
        path = settings.home / "engine-artifacts" / (engine + ".json")
        try:
            record = json.loads(path.read_bytes())
            if record.get("artifact_identity") != manifest.get("artifact_identity"):
                reasons.append("model_artifact_changed")
            if record.get("rules_version") != result.get("rules_version") or record.get(
                "rule_source_hashes"
            ) != manifest.get("rule_source_hashes"):
                reasons.append("artifact_rule_sources_changed")
            for field in ("candidate_provider", "clinical_models", "learned_models", "gene_dictionary"):
                if record.get(field) != manifest.get(field):
                    reasons.append(field + "_changed")
            if record.get("dictionary_version") != result.get("dictionary_version"):
                reasons.append("dictionary_changed")
            if record.get("model_sha256") != parser.get("model_sha256"):
                reasons.append("parser_artifact_changed")
        except (OSError, ValueError, TypeError):
            reasons.append("artifact_record_missing")
    return sorted(set(reasons))


def extract_engine(text, sections, dictionaries, metadata=None, engine="heuristic-v1"):
    validate_engine(engine)
    if engine != "heuristic-v5":
        return old.extract_engine(text, sections, dictionaries, metadata, engine)
    from .extraction_v5 import extract

    result = extract(text, sections, dictionaries, metadata)
    validate_source_spans(result, text)
    return result


def default_engine(settings):
    try:
        record = json.loads((settings.home / "engine-default.json").read_bytes())
        if record.get("engine") == "heuristic-v5":
            from .operations_v5 import promotion_valid

            return "heuristic-v5" if promotion_valid(settings, record) else "heuristic-v1"
    except (OSError, ValueError, TypeError):
        pass
    return old.default_engine(settings)


def switch_engine(settings, engine, promotion=None):
    if engine != "heuristic-v5":
        return old.switch_engine(settings, engine, promotion)
    from .operations_v5 import promotion_valid

    record = {
        "schema_version": "literature-engine-default-v5",
        "engine": engine,
        "rules_version": engine_rules_version(engine),
        "promotion": promotion,
        "promotion_sha256": digest(dumps(promotion)),
    }
    if not promotion_valid(settings, record):
        raise ValueError(
            "V5 requires independent automatic evaluation, immutable provenance and seven actual operation days"
        )
    path = settings.home / "engine-default.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(dumps(record), encoding="utf-8")
    temporary.replace(path)
    return record
