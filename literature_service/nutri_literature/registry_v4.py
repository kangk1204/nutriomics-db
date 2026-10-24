"""Extension registry. Frozen engines.py and its scientific hashes stay intact."""
import json
from . import engines as legacy
from .db import digest, dumps

ENGINE_CHOICES = legacy.ENGINE_CHOICES + ("heuristic-v4",)
validate_source_spans = legacy.validate_source_spans


def validate_engine(engine):
    if engine not in ENGINE_CHOICES:
        raise ValueError("Unknown extraction engine: " + str(engine))
    return engine


def engine_rules_version(engine):
    validate_engine(engine)
    if engine == "heuristic-v4":
        from .extraction_v4 import source_manifest
        return source_manifest()[1]
    return legacy.engine_rules_version(engine)


def engine_manifest_validation(result, engine, settings=None):
    if engine != "heuristic-v4":
        return legacy.engine_manifest_validation(result, engine, settings)
    if not isinstance(result, dict):
        return ["malformed_extraction"]
    manifest = result.get("manifest", {})
    reasons = []
    if result.get("engine_version") != engine:
        reasons.append("engine_changed")
    if result.get("rules_version") != engine_rules_version(engine):
        reasons.append("rules_changed")
    parser = manifest.get("parser", {})
    if parser.get("mode") != "production" or not parser.get("available"):
        reasons.append("nonproduction_parser")
    if manifest.get("oracle"):
        reasons.append("oracle_experiment")
    if manifest.get("profile") not in {"local", "pubtator_assisted"}:
        reasons.append("invalid_input_profile")
    for name in ("candidate_provider", "clinical_models"):
        if not manifest.get(name, {}).get("available"):
            reasons.append(name + "_unavailable")
    if not manifest.get("gene_dictionary", {}).get("records_sha256"):
        reasons.append("native_gene_dictionary_missing")
    if settings is not None:
        path = settings.home / "engine-artifacts" / (engine + ".json")
        if not path.exists():
            reasons.append("artifact_record_missing")
        else:
            expected = json.loads(path.read_bytes())
            if expected.get("artifact_identity") != manifest.get("artifact_identity"):
                reasons.append("model_artifact_changed")
            if expected.get("dictionary_version") != result.get("dictionary_version"):
                reasons.append("dictionary_changed")
            for field in ("candidate_provider","clinical_models","gene_dictionary"):
                if field in expected and expected[field]!=manifest.get(field):
                    reasons.append(field+"_changed")
            if expected.get("model_sha256") and expected["model_sha256"]!=parser.get("model_sha256"):
                reasons.append("parser_artifact_changed")
    return sorted(set(reasons))


def extract_engine(text, sections, dictionaries, metadata=None, engine="heuristic-v1"):
    validate_engine(engine)
    if engine != "heuristic-v4":
        return legacy.extract_engine(text, sections, dictionaries, metadata, engine)
    from .extraction_v4 import extract
    result = extract(text, sections, dictionaries, metadata)
    validate_source_spans(result, text)
    return result


def default_engine(settings):
    # Promotion remains gated. Development results cannot change the live default.
    path = settings.home / "engine-default.json"
    try:
        record = json.loads(path.read_bytes())
        if record.get("engine") == "heuristic-v4":
            from .operations_v4 import promotion_valid
            return "heuristic-v4" if promotion_valid(settings, record) else "heuristic-v1"
    except (OSError, ValueError, TypeError):
        pass
    return legacy.default_engine(settings)


def switch_engine(settings, engine, promotion=None):
    if engine != "heuristic-v4":
        return legacy.switch_engine(settings, engine, promotion)
    from .operations_v4 import promotion_valid
    record = {"schema_version": "literature-engine-default-v4", "engine": engine,
              "rules_version": engine_rules_version(engine), "promotion": promotion,
              "promotion_sha256": digest(dumps(promotion))}
    if not promotion_valid(settings, record):
        raise ValueError("V4 requires passing automatic evaluation, immutable provenance and seven actual operation days")
    path = settings.home / "engine-default.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(dumps(record), encoding="utf-8")
    temporary.replace(path)
    return record
