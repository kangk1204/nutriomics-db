"""Learned relation hypotheses plus separate source-anchored semantic validation."""

from collections import defaultdict
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from . import extraction_v4 as v4
from .candidates_v5 import collect
from .clinical_v4 import assertion_state, bind_conditions, summary
from .db import digest, dumps
from .extraction_v2 import _TRIGGERS, _scope
from .features_v5 import pair_candidates, pool_features
from .grammar_v3 import modifiers, roles
from .nlp_v2 import cpu_inference_lock, parser_for, validate_tokens
from .scope_v5 import predict as predict_scope
from .text import sentence_spans

ENGINE_VERSION = "heuristic-v5"
_ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "nutri_literature/" + n + ".py"
    for n in (
        "features_v5",
        "candidates_v5",
        "models_v5",
        "relations_v5",
        "scope_v5",
        "training_v5",
        "corpora_v5",
        "extraction_v5",
        "evaluation_v5",
        "final_v5",
        "resources_v5",
        "registry_v5",
        "operations_v5",
        "cli_v5",
    )
]


def source_manifest():
    old, _ = v4.source_manifest()
    hashes = {}
    h = hashlib.sha256()
    for name in sorted(set(FILES) | set(old)):
        raw = (_ROOT / name).read_bytes()
        hashes[name] = digest(raw)
        h.update(name.encode() + b"\0" + raw + b"\0")
    return hashes, "rules-sha256:" + h.hexdigest()


def endpoint_key(mention):
    # A key groups mentions; neither key nor ID is passed into learned features.
    return mention.get("normalized_id") or (mention["kind"] + ":" + mention["text"].casefold())


def semantic_event(pair, label, role_score, scopes, metadata, all_mentions):
    sentence, tokens = pair["sentence"], pair["tokens"]
    chemical, gene = pair["chemical"], pair["gene"]
    possibilities = []
    for token in tokens:
        kind = _TRIGGERS.get(token["lemma"])
        if not kind:
            continue
        left, right, reasons, _ = roles(tokens, token, kind, [chemical, gene])
        for a in left:
            for b in right:
                if a["kind"] != b["kind"] and not reasons:
                    possibilities.append((a, b, token, kind))
    unique = {(a["start"], b["start"], t["start"]): (a, b, t, k) for a, b, t, k in possibilities}
    reasons = []
    model_role = (
        "chemical_to_gene" if role_score >= 0.7 else "gene_to_chemical" if role_score <= 0.3 else "unknown"
    )
    syntax_directions = {
        "chemical_to_gene" if p[0]["kind"] != "gene" else "gene_to_chemical" for p in unique.values()
    }
    role_source = "model_and_syntax"
    if model_role == "unknown" and len(syntax_directions) == 1:
        model_role = next(iter(syntax_directions))
        role_source = "unambiguous_syntax"
    supported = [
        p
        for p in unique.values()
        if ("chemical_to_gene" if p[0]["kind"] != "gene" else "gene_to_chemical") == model_role
    ]
    if len(supported) == 1:
        controller, theme, trigger, kind = supported[0]
    else:
        return None, [
            "direction_unresolved"
            if model_role == "unknown"
            else "syntax_role_conflict"
            if unique
            else "syntax_role_unresolved"
        ]
    scope = _scope(sentence, tokens, trigger)
    direction, negation, speculation, significance = modifiers(sentence, tokens, trigger, scope, kind)
    linked = [s for s in scopes if s["start"] <= trigger["start"] < trigger["end"] <= s["end"]]
    crf_neg = any(s["kind"] == "negation" for s in linked)
    crf_spec = any(s["kind"] == "speculation" for s in linked)
    if metadata.get("_ablation") != "relations" and (
        bool(negation) != crf_neg or bool(speculation) != crf_spec
    ):
        reasons.append("scope_rule_conflict")
    # Negation, statistical non-significance and no-effect are independent meanings.
    state = assertion_state(
        {
            **sentence,
            "text": sentence["text"][scope["start"] - sentence["start"] : scope["end"] - sentence["start"]],
        },
        metadata,
    )
    if state == "observed" and speculation:
        state = "interpretation"
    for mention in (controller, theme):
        if not mention.get("normalized_id") and not mention.get("payload", {}).get("provided_native_ids"):
            reasons.append(
                "gene_identity_unresolved" if mention["kind"] == "gene" else "chemical_identity_unresolved"
            )
    outcome = (
        "expression"
        if re.search(r"\b(?:expression|transcription|mRNA)\b", sentence["text"], re.I)
        else (
            "methylation"
            if re.search(r"\b(?:methylation|methylated)\b", sentence["text"], re.I)
            else "activity"
            if re.search(r"\b(?:activity|activation|inhibition)\b", sentence["text"], re.I)
            else "concentration"
            if re.search(r"\b(?:concentration|level|levels|content)\b", sentence["text"], re.I)
            else "unknown"
        )
    )
    taxon = gene.get("payload", {}).get("taxon_id")
    quote = {"start": sentence["start"], "end": sentence["end"], "quote": sentence["text"]}
    event = {
        "controller": deepcopy(controller),
        "theme": deepcopy(theme),
        "subject": controller.get("normalized_id") or controller["text"],
        "object": theme.get("normalized_id") or theme["text"],
        "trigger": {k: trigger[k] for k in ("text", "start", "end", "lemma")},
        "relation_type": "binding"
        if kind == "binding"
        else "association"
        if kind == "association"
        else "effect",
        "benchmark_relation_type": label,
        "benchmark_type_is_scientific_effect": False,
        "role_direction": model_role,
        "role_source": role_source,
        "outcome_type": outcome,
        "direction": direction,
        "association_direction": None,
        "negation": bool(negation),
        "speculation": bool(speculation),
        "significance": significance,
        "statistical_significance": significance,
        "scope": {"start": sentence["start"], "end": sentence["end"], "text": sentence["text"]},
        "assertion_state": state,
        "evidence_quotes": [quote],
        "rule_id": "v5-learned-type-syntax-role",
        "taxon_id": taxon,
        "species": "human" if taxon == 9606 else "animal" if taxon else "unknown",
        "study_model": gene.get("payload", {}).get("study_model", "unknown"),
        "triage_reasons": sorted(set(reasons)),
        "polarity": "null" if direction == "no_effect" else "negated" if negation else "positive",
        "source_relation_candidate": True,
        "support_scope": "chemical_gene_same_sentence",
        "abstention_reasons": [],
        "measurement": {"kind": outcome, "target": theme.get("normalized_id") or theme["text"]},
    }
    event["organism"] = event["species"]
    local = [m for m in all_mentions if sentence["start"] <= m["start"] < m["end"] <= sentence["end"]]
    event["conditions"] = bind_conditions(
        event, sentence, local, unique_event=len({endpoint_key(m) for m in local if m["kind"] == "gene"}) == 1
    )
    event.update({k: event["conditions"][k] for k in ("arm_id", "comparator", "timepoint")})
    event["status"] = "candidate" if state == "observed" and not reasons else "needs_review"
    if state in {"planned", "background"}:
        event["status"] = "abstained" if state == "planned" else "background"
        event["abstention_reasons"] = ["assertion_" + state]
    event["event_id"] = "event-sha256:" + digest(
        dumps({"semantic_key": v4._event_key(event), "benchmark_type": label, "role": model_role})
    )
    return event, reasons


def extract(text, sections, dictionaries, metadata=None):
    metadata = dict(metadata or {})
    required = {"_candidate_provider", "_gene_index", "_clinical_models", "_learned_models"}
    if not required <= set(metadata):
        raise ValueError("V5 CPU models missing: " + str(sorted(required - set(metadata))))
    learned = metadata["_learned_models"]
    learned.load()
    sentences = sentence_spans(text, sections)
    mentions, definitions, hypotheses, rejected = collect(text, sentences, dictionaries, metadata)
    base = v4.extract(text, sections, dictionaries, {**metadata, "_gold_entities": mentions})
    parser = parser_for(metadata)
    pairs = pair_candidates(base["sentence_roles"], mentions, lambda s: validate_tokens(parser.parse(s), s))
    grouped = defaultdict(list)
    for pair in pairs:
        grouped[(endpoint_key(pair["chemical"]), endpoint_key(pair["gene"]))].append(pair)
    bags = list(grouped.values())
    with cpu_inference_lock():
        type_predictions = learned.models["relation"].predict([[p["features"] for p in b] for b in bags])
    role_scores = (
        learned.score("direction", [pool_features([p["features"] for p in b]) for b in bags]) if bags else []
    )
    scopes = (
        base["benchmark_scopes"]
        if metadata.get("_ablation") == "relations"
        else predict_scope(sentences, learned)
    )
    events = base["events"]
    for event in events:
        event["support_scope"] = (
            "rule_fallback_chemical_gene"
            if {event["controller"]["kind"], event["theme"]["kind"]} & {"gene"}
            else "rule_only_outside_learned_scope"
        )
    if metadata.get("_ablation") == "entities":
        events = base["events"]
    relation_hypotheses = []
    trace = []
    for bag, predicted, role_score in zip(bags, type_predictions, role_scores):
        for label, item in predicted.items():
            pair = bag[item["witness"]]
            passed = item["score"] >= learned.manifest["relation_threshold"]
            event, reasons = (
                semantic_event(pair, label, float(role_score), scopes, metadata, mentions)
                if passed
                else (None, ["type_threshold"])
            )
            record = {
                "chemical": deepcopy(pair["chemical"]),
                "gene": deepcopy(pair["gene"]),
                "benchmark_relation_type": label,
                "scientific_effect_direction": event.get("direction") if event else None,
                "priority_score": item["score"],
                "role_priority_score": float(role_score),
                "role_direction": event["role_direction"]
                if event
                else "chemical_to_gene"
                if role_score >= 0.7
                else "gene_to_chemical"
                if role_score <= 0.3
                else "unknown",
                "role_source": event.get("role_source") if event else None,
                "type_selected": passed,
                "semantic_verified": event is not None
                and not reasons
                and event["status"] == "candidate"
                and event["assertion_state"] == "observed",
                "event_id": event["event_id"] if event else None,
                "reasons": reasons,
                "evidence": {
                    "start": pair["sentence"]["start"],
                    "end": pair["sentence"]["end"],
                    "quote": pair["sentence"]["text"],
                },
                "witnesses": len(bag),
                "support_scope": "chemical_gene_same_sentence",
                "scores_are_nutrition_probabilities": False,
            }
            relation_hypotheses.append(record)
            trace.append(
                {
                    "pair": [endpoint_key(pair["chemical"]), endpoint_key(pair["gene"])],
                    "type": label,
                    "candidate_created": True,
                    "id_gate_before_candidate": False,
                    "type_selected": passed,
                    "role_verified": event is not None,
                    "reasons": reasons,
                }
            )
            if event and metadata.get("_ablation") != "entities":
                events.append(event)
    comparison_hypotheses = []
    if metadata.get("_compare_relations"):
        for configuration in learned.manifest.get("relation_configurations", []):
            with cpu_inference_lock():
                alternatives = learned.models[configuration].predict(
                    [[p["features"] for p in b] for b in bags]
                )
            for bag, predicted, role_score in zip(bags, alternatives, role_scores):
                for label, item in predicted.items():
                    pair = bag[item["witness"]]
                    event, reasons = (
                        semantic_event(pair, label, float(role_score), scopes, metadata, mentions)
                        if item["score"] >= 0.3
                        else (None, ["type_threshold"])
                    )
                    comparison_hypotheses.append(
                        {
                            "configuration": configuration,
                            "chemical": pair["chemical"],
                            "gene": pair["gene"],
                            "benchmark_relation_type": label,
                            "priority_score": item["score"],
                            "type_selected": False,
                            "role_direction": event["role_direction"]
                            if event
                            else "chemical_to_gene"
                            if role_score >= 0.7
                            else "gene_to_chemical"
                            if role_score <= 0.3
                            else "unknown",
                            "semantic_verified": event is not None
                            and not reasons
                            and event["status"] == "candidate"
                            and event["assertion_state"] == "observed",
                            "reasons": reasons,
                            "evidence": {
                                "start": pair["sentence"]["start"],
                                "end": pair["sentence"]["end"],
                                "quote": pair["sentence"]["text"],
                            },
                        }
                    )
    before_grouping = len(events)
    groups = {}
    for event in events:
        event.setdefault("support_scope", "rule_only_outside_learned_scope")
        key = event["event_id"]
        if key not in groups:
            groups[key] = event
        else:
            for quote in event["evidence_quotes"]:
                if quote not in groups[key]["evidence_quotes"]:
                    groups[key]["evidence_quotes"].append(quote)
    events = list(groups.values())
    assertions = []
    for event in events:
        if event["status"] != "candidate" and not (
            event["status"] == "needs_review"
            and event.get("assertion_state") == "observed"
            and not event.get("abstention_reasons")
        ):
            continue
        predicate = {"increase": "increases", "decrease": "decreases", "association": "associated_with"}.get(
            event["direction"], "binds" if event["relation_type"] == "binding" else "affects"
        )
        assertions.append(
            {
                "subject": event["subject"],
                "object": event["object"],
                "predicate": predicate,
                **event["evidence_quotes"][0],
                "rule_id": event["rule_id"],
                "payload": {
                    "event": event,
                    "explicit_relation": True,
                    "subject_mention": event["controller"],
                    "object_mention": event["theme"],
                    "triage_reasons": event["triage_reasons"],
                    "rule_score": 1,
                },
            }
        )
    # Only grounded observed events can supply summary result slots.
    anchors = {
        (q["start"], q["end"])
        for e in events
        if e["assertion_state"] == "observed" and e["status"] == "candidate"
        for q in e["evidence_quotes"]
    }
    summary_sentences = [
        {
            **s,
            "assertion_state": s["assertion_state"]
            if (s["start"], s["end"]) in anchors or s["assertion_state"] != "observed"
            else "unknown",
        }
        for s in base["sentence_roles"]
    ]
    hashes, rules = source_manifest()
    manifest = {
        **base["manifest"],
        "engine_version": ENGINE_VERSION,
        "rules_version": rules,
        "rule_source_hashes": hashes,
        "learned_models": learned.manifest,
        "oracle": metadata.get("_gold_entities") is not None,
        "artifact_identity": metadata.get("_artifact_identity"),
        "ablation": metadata.get("_ablation", "context"),
    }
    return {
        **base,
        "engine_version": ENGINE_VERSION,
        "rules_version": rules,
        "manifest": manifest,
        "mentions": mentions,
        "events": events,
        "assertions": assertions,
        "entity_hypotheses": hypotheses,
        "relation_hypotheses": relation_hypotheses,
        "decision_trace": trace,
        "rejected_entity_candidates": rejected,
        "abbreviations": definitions,
        "relation_comparison_hypotheses": comparison_hypotheses,
        "benchmark_scopes": scopes,
        "summary": summary(summary_sentences, events),
        "candidate_pairs": [[p["chemical"], p["gene"]] for p in pairs],
        "duplicate_events_grouped": before_grouping - len(groups),
        "diagnostics": {
            **base["diagnostics"],
            "candidate_pairs": len(pairs),
            "relation_bags": len(bags),
            "id_unresolved_candidates_kept": sum(not p["gene"].get("normalized_id") for p in pairs),
            "span_filter_rejections": len(rejected),
            "type_selected": sum(r["type_selected"] for r in relation_hypotheses),
            "semantic_verified": sum(r["semantic_verified"] for r in relation_hypotheses),
        },
    }
