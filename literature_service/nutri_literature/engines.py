"""Engine dispatch and read-only provenance checks. No parser inference on reads."""
import importlib
import json

from .db import digest, dumps, now, connect, uid

ENGINE_CHOICES = ("heuristic-v1", "heuristic-v2")


def validate_engine(engine):
    if engine not in ENGINE_CHOICES:
        raise ValueError(f"Unknown extraction engine: {engine}")
    return engine


def _module(engine):
    validate_engine(engine)
    return importlib.import_module(".extraction" if engine == "heuristic-v1" else ".extraction_v2", __package__)


def engine_rules_version(engine):
    return _module(engine).RULES_VERSION


def engine_manifest_validation(result, engine, settings=None):
    reasons = []
    if not isinstance(result,dict):
        return ["malformed_extraction"]
    validate_engine(engine)
    if result.get("engine_version") not in ((None, engine) if engine == "heuristic-v1" else (engine,)):
        reasons.append("engine_changed")
    if result.get("rules_version") != engine_rules_version(engine):
        reasons.append("rules_changed")
    if settings is not None:
        with connect(settings) as con:
            profile = con.execute("SELECT dictionary_version FROM engine_profiles WHERE engine_version=?",(engine,)).fetchone()
        if profile and result.get("dictionary_version") != profile['dictionary_version']:
            reasons.append("dictionary_changed")
    if engine == "heuristic-v2":
        manifest = result.get("manifest") or {}
        parser = manifest.get("parser",{}) if isinstance(manifest,dict) else {}
        parser = parser if isinstance(parser,dict) else {}
        if isinstance(manifest,dict) and manifest.get("ablation_stage"):
            reasons.append("development_ablation")
        if not parser.get("available") or result.get("status") == "blocked":
            reasons.append("parser_unavailable")
        if parser.get("mode") != "production":
            reasons.append("nonproduction_parser")
        expected = {"name": "en_core_sci_sm", "version": "0.5.4", "spacy_version": "3.7.5", "scispacy_version": "0.6.2"}
        if any(parser.get(k) != v for k, v in expected.items()):
            reasons.append("parser_version_changed")
        sha = parser.get("model_sha256", "")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            reasons.append("model_hash_missing")
        if settings is not None:
            artifact = settings.home / "engine-artifacts" / (engine + ".json")
            if artifact.exists():
                try:
                    expected_parser = json.loads(artifact.read_text(encoding="utf-8"))
                    if expected_parser.get("model_sha256") != sha:
                        reasons.append("model_artifact_changed")
                except (OSError,ValueError,TypeError,AttributeError):
                    reasons.append("invalid_model_artifact_record")
    return reasons


def extract_engine(text, sections, dictionaries, metadata=None, engine="heuristic-v1"):
    module = _module(engine)
    if engine == "heuristic-v1":
        result = module.extract(text, sections, metadata or {}, dictionaries)
        if metadata and "_gold_entities" in metadata:
            from .text import sentence_spans
            mentions = []
            for entity in metadata["_gold_entities"]:
                validate_source_spans(entity,text)
                mentions.append({**entity,"status":"oracle_supplied","payload":{
                    **entity.get("payload",{}),"oracle":True,"ambiguous":False}})
            sentences = sentence_spans(text,sections)
            result["mentions"] = mentions
            result["assertions"] = module._relations(sentences,mentions,metadata)
            result["summary"] = module._summary(sentences,result["assertions"])
            result["oracle_entities_used"] = True
        return result
    return module.extract(text, sections, dictionaries, metadata=metadata or {})


def default_engine(settings):
    path = settings.home / "engine-default.json"
    if not path.exists():
        return "heuristic-v1"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("engine") == "heuristic-v1":
            return "heuristic-v1"
        if (record.get("schema_version") != "literature-engine-default-v1"
                or record.get("rules_version") != engine_rules_version("heuristic-v2")):
            return "heuristic-v1"
        receipt = record.get("promotion", {})
        if receipt.get("passed") is not True or digest(dumps(receipt)) != record.get("promotion_sha256"):
            return "heuristic-v1"
        from .benchmarks import verify_receipt
        if not verify_receipt(settings,"engine_promotion",receipt):
            return "heuristic-v1"
        from .dictionaries import load_dictionaries
        if receipt.get("provenance",{}).get("dictionary_version") != load_dictionaries(settings,"heuristic-v2").version:
            return "heuristic-v1"
        artifact = json.loads((settings.home / "engine-artifacts" / "heuristic-v2.json").read_text(encoding="utf-8"))
        if artifact != receipt.get("provenance",{}).get("parser"):
            return "heuristic-v1"
        return "heuristic-v2"
    except (OSError, ValueError, TypeError, KeyError):
        return "heuristic-v1"


def switch_engine(settings, engine, promotion=None):
    validate_engine(engine)
    if engine == "heuristic-v2":
        from .benchmarks import verify_receipt
        if not promotion or promotion.get("passed") is not True or not verify_receipt(settings,"engine_promotion",promotion):
            raise ValueError("v2 requires durably verified public evaluation and seven-day engine-specific operation gates")
    record = {"schema_version": "literature-engine-default-v1", "engine": engine, "switched_at": now()}
    if engine == "heuristic-v2":
        record.update(rules_version=engine_rules_version(engine), promotion=promotion,
                      promotion_sha256=digest(dumps(promotion)))
    settings.ensure()
    path = settings.home / "engine-default.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(dumps(record), encoding="utf-8")
    temporary.replace(path)
    with connect(settings) as con:
        con.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (uid(), "engine_switched", None, dumps(record), now()))
    return record


def validate_source_spans(value, text):
    """Validate every nested quotation/field against immutable Unicode codepoints."""
    if isinstance(value, dict):
        literal = value.get("quote", value.get("text"))
        if literal is not None and "start" in value and "end" in value:
            start, end = value["start"], value["end"]
            if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int)
                    or isinstance(end, bool) or not isinstance(literal, str)
                    or not 0 <= start < end <= len(text) or text[start:end] != literal):
                raise ValueError("Nested event quotation/field offsets do not match source")
        for child in value.values():
            validate_source_spans(child, text)
    elif isinstance(value, list):
        for child in value:
            validate_source_spans(child, text)
