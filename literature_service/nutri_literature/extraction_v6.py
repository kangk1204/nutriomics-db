"""Retained candidates, shared bags, frozen-vector heads and independent observations."""

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from . import extraction_v4 as v4
from .candidates_v6 import collect
from .clinical_v6 import observations, summary, state, clauses
from .db import digest, dumps
from .embeddings_v6 import pooled_pair_features
from .extraction_v5 import semantic_event
from .features_v5 import pair_candidates
from .nlp_v2 import cpu_inference_lock, parser_for, validate_tokens
from .scope_v5 import predict as predict_scope
from .text import sentence_spans

ENGINE_VERSION = "heuristic-v6"
FILES = [
    "nutri_literature/" + name + ".py"
    for name in (
        "chemical_v6",
        "identity_v6",
        "embeddings_v6",
        "models_v6",
        "training_v6",
        "corpora_v6",
        "candidates_v6",
        "clinical_v6",
        "extraction_v6",
        "registry_v6",
        "operations_v6",
        "evaluation_v6",
        "cli_v6",
        "review_v6",
        "final_v6",
        "clinical_training_v6",
        "clinical_evaluation_v6",
        "audit_v6",
        "provider_v6",
        "resources_v6",
        "holdout_v6",
        "features_v6",
        "scope_recovery_v6",
        "clinical_identity_v6",
    )
]


def source_manifest():
    from .extraction_v5 import source_manifest as previous

    root = Path(__file__).resolve().parents[1]
    old, _ = previous()
    files = sorted(set(old) | set(FILES))
    hashes, h = {}, hashlib.sha256()
    for name in files:
        raw = (root / name).read_bytes()
        hashes[name] = digest(raw)
        h.update(name.encode() + b"\0" + raw + b"\0")
    return hashes, "rules-sha256:" + h.hexdigest()


def observation_assertion(observation):
    intervention = observation.get("intervention")
    if not intervention:
        return None
    return {
        "subject": intervention.get("normalized_id") or intervention["text"],
        "object": observation["outcome"]["text"],
        "predicate": "has_observation",
        **observation["evidence_quotes"][0],
        "rule_id": "v6-independent-observation",
        "payload": {
            "observation": observation,
            "endpoint_order_is_causal": False,
            "explicit_relation": True,
            "subject_mention": intervention,
            "object_mention": observation["outcome"],
            "triage_reasons": observation["reasons"],
            "rule_score": 1,
        },
    }


def native_event(pair, label, direction, scopes, metadata, mentions):
    if direction in {"chemical_to_gene", "gene_to_chemical"}:
        event, reasons = semantic_event(
            pair, label, 1.0 if direction == "chemical_to_gene" else 0.0, scopes, metadata, mentions
        )
        if event is None:
            possible = v4.nested_events(pair["sentence"], [pair["chemical"], pair["gene"]], pair["tokens"])
            possible = [
                e
                for e in possible
                if ("chemical_to_gene" if e["controller"]["kind"] != "gene" else "gene_to_chemical")
                == direction
            ]
            if len(possible) == 1:
                event = possible[0]
                event.update(
                    benchmark_relation_type=label,
                    role_direction=direction,
                    role_source="nested_dependency_rule",
                    assertion_state="unknown",
                    support_scope="chemical_gene_same_sentence",
                    statistical_significance=event["significance"],
                )
                from .clinical_v4 import bind_conditions

                event["conditions"] = bind_conditions(event, pair["sentence"], mentions)
                gene = pair["gene"]
                taxon = gene.get("payload", {}).get("taxon_id")
                event.update(
                    taxon_id=taxon,
                    species="human" if taxon == 9606 else "animal" if taxon else "unknown",
                    study_model=gene.get("payload", {}).get("study_model", "unknown"),
                    source_relation_candidate=True,
                    rule_id="v6-nested-dependency",
                )
                event["triage_reasons"] = []
                reasons = []
    elif direction == "undirected":
        # Require an explicit relational predicate with both endpoints on its
        # dependency component. Mere presence in a sentence is never evidence.
        tokens = pair["tokens"]
        from .features_v5 import _head
        from collections import deque

        a_head, b_head = _head(tokens, pair["chemical"]), _head(tokens, pair["gene"])
        graph = defaultdict(set)
        for token in tokens:
            if token["head"] != token["i"]:
                graph[token["i"]].add(token["head"])
                graph[token["head"]].add(token["i"])
        queue = deque([(a_head["i"], [a_head["i"]])]) if a_head else deque()
        visited = set()
        path_nodes = []
        while queue and b_head:
            node, nodes = queue.popleft()
            if node == b_head["i"]:
                path_nodes = nodes
                break
            if node in visited:
                continue
            visited.add(node)
            queue.extend((other, nodes + [other]) for other in graph[node] if other not in visited)
        triggers = [
            t
            for t in tokens
            if t["i"] in path_nodes
            and re.fullmatch(
                r"associat\w*|correlat\w*|relationship|association|correlation|bind\w*|binding",
                t["lemma"],
                re.I,
            )
        ]
        path = pair["features"].get("path_length", 0)
        if len(triggers) != 1 or path <= 0 or path > 8:
            return None, ["direct_symmetric_evidence_unresolved"]
        trigger = triggers[0]
        # A negated association is still an event, with its negation preserved.
        from .grammar_v3 import modifiers
        from .extraction_v2 import _scope

        scope = _scope(pair["sentence"], tokens, trigger)
        effect, neg, spec, sig = modifiers(pair["sentence"], tokens, trigger, scope, "association")
        a, b = deepcopy(pair["chemical"]), deepcopy(pair["gene"])
        event = {
            "controller": a,
            "theme": b,
            "left_entity": a,
            "right_entity": b,
            "subject": a.get("normalized_id") or a["text"],
            "object": b.get("normalized_id") or b["text"],
            "endpoint_order_is_causal": False,
            "role_direction": "undirected",
            "role_source": "symmetric_dependency_rule",
            "trigger": {k: trigger[k] for k in ("text", "start", "end", "lemma")},
            "relation_type": "association",
            "benchmark_relation_type": label,
            "benchmark_type_is_scientific_effect": False,
            "direction": "association",
            "outcome_type": "unknown",
            "negation": bool(neg),
            "speculation": bool(spec),
            "significance": sig,
            "statistical_significance": sig,
            "polarity": "negated" if neg else "positive",
            "taxon_id": b.get("payload", {}).get("taxon_id"),
            "species": "human"
            if b.get("payload", {}).get("taxon_id") == 9606
            else "animal"
            if b.get("payload", {}).get("taxon_id")
            else "unknown",
            "study_model": b.get("payload", {}).get("study_model", "unknown"),
            "conditions": {
                "arm_id": None,
                "comparator": None,
                "timepoint": None,
                "quantities": [],
                "statistics": [],
                "unassigned_quantities": [],
            },
            "evidence_quotes": [
                {
                    "start": pair["sentence"]["start"],
                    "end": pair["sentence"]["end"],
                    "quote": pair["sentence"]["text"],
                }
            ],
            "scope": scope,
            "abstention_reasons": [],
            "triage_reasons": [],
            "rule_id": "v6-symmetric-dependency",
            "support_scope": "chemical_gene_same_sentence",
            "source_relation_candidate": True,
        }
        reasons = []
    else:
        return None, ["role_direction_unresolved"]
    if event is None:
        return None, reasons
    sentence = next(
        (c for c in clauses(pair["sentence"]) if c["start"] <= event["trigger"]["start"] < c["end"]),
        pair["sentence"],
    )
    assertion, _ = state(sentence, metadata)
    event["assertion_state"] = assertion
    if event.get("speculation") and assertion == "observed":
        event["assertion_state"] = "interpretation"
    for m in (pair["chemical"], pair["gene"]):
        if not m.get("normalized_id") and not m.get("payload", {}).get("provided_native_ids"):
            reasons.append(
                "gene_identity_unresolved" if m["kind"] == "gene" else "chemical_identity_unresolved"
            )
    if event["assertion_state"] != "observed":
        reasons.append("assertion_" + event["assertion_state"])
    event["triage_reasons"] = sorted(set(event.get("triage_reasons", []) + reasons))
    event["status"] = "candidate" if not event["triage_reasons"] else "needs_review"
    event["rule_id"] = event.get("rule_id", "v6-dependency")
    semantic = {"semantic": v4._event_key(event), "type": label, "role": direction}
    if not any(q.get("quantity_role") == "dose" for q in event.get("conditions", {}).get("quantities", [])):
        semantic["source_quotes"] = [(q["start"], q["end"]) for q in event["evidence_quotes"]]
    event["event_id"] = "event-sha256:" + digest(dumps(semantic))
    return event, event["triage_reasons"]


def extract(text, sections, dictionaries, metadata=None):
    metadata = dict(metadata or {})
    required = {
        "_candidate_provider",
        "_gene_index",
        "_clinical_models",
        "_learned_models",
        "_id_search",
        "_v6_models",
        "_encoder",
    }
    if not required <= set(metadata):
        raise ValueError("Missing v6 dependencies: " + str(sorted(required - set(metadata))))
    models, encoder = metadata["_v6_models"], metadata["_encoder"]
    models.load()
    encoder.load()
    if models.manifest.get("encoder") != encoder.manifest:
        raise ValueError("Training/inference frozen encoder mismatch")
    sentences = sentence_spans(text, sections)
    mentions, definitions, hypotheses, clusters = collect(text, sentences, dictionaries, metadata)
    base = v4.extract(text, sections, dictionaries, {**metadata, "_gold_entities": mentions})
    parser = parser_for(metadata)

    def parse(sentence):
        return validate_tokens(parser.parse(sentence), sentence)

    pairs = pair_candidates(base["sentence_roles"], mentions, parse)
    grouped = defaultdict(list)
    for pair in pairs:
        grouped[
            (pair["chemical"]["payload"]["mention_cluster_id"], pair["gene"]["payload"]["mention_cluster_id"])
        ].append(pair)
    bags = list(grouped.values())
    features = [pooled_pair_features(bag, text, encoder) for bag in bags]
    with cpu_inference_lock():
        predictions = models.predict(features)
    scopes = predict_scope(base["sentence_roles"], metadata["_learned_models"])
    events, relations, trace = [], [], []
    for bag, predicted in zip(bags, predictions):
        directions = predicted["direction"]
        role = max(directions, key=directions.get) if directions else "unknown"
        if directions.get(role, 0) < 0.7:
            role = "unknown"
        existence = predicted["existence"] >= models.manifest["selections"]["existence"]["threshold"]
        for label, score in predicted["types"].items():
            selected = existence and score >= models.manifest["selections"]["type_" + label]["threshold"]
            event, reasons, pair = None, ["type_or_existence_threshold"], bag[0]
            if selected:
                checked = [(p, *native_event(p, label, role, scopes, metadata, mentions)) for p in bag]
                pair, event, reasons = min(
                    checked, key=lambda x: (x[1] is None, len(x[2]), x[0]["sentence"]["start"])
                )
            relation = {
                "chemical": pair["chemical"],
                "gene": pair["gene"],
                "benchmark_relation_type": label,
                "role_direction": role,
                "type_selected": selected,
                "existence_selected": existence,
                "priority_score": float(score),
                "existence_priority_score": float(predicted["existence"]),
                "direction_priority_scores": directions,
                "semantic_verified": event is not None and not reasons,
                "scientific_effect_direction": event.get("direction") if event else None,
                "reasons": reasons,
                "event_id": event.get("event_id") if event else None,
                "evidence": {
                    "start": pair["sentence"]["start"],
                    "end": pair["sentence"]["end"],
                    "quote": pair["sentence"]["text"],
                },
                "witnesses": len(bag),
                "support_scope": "chemical_gene_same_sentence",
                "scores_are_nutrition_probabilities": False,
            }
            relations.append(relation)
            trace.append(
                {
                    "candidate_created": True,
                    "id_gate_before_candidate": False,
                    "type_selected": selected,
                    "role": role,
                    "reasons": reasons,
                    "cluster_pair": [
                        p["payload"]["mention_cluster_id"] for p in (pair["chemical"], pair["gene"])
                    ],
                }
            )
            if event:
                events.append(event)
    # Existing food/diet/health rules remain outside chemical-gene learned scope.
    for event in base["events"]:
        if not any(m["kind"] == "gene" for m in (event["controller"], event["theme"])):
            q = event["evidence_quotes"][0]
            sentence = next(
                (s for s in base["sentence_roles"] if s["start"] == q["start"] and s["end"] == q["end"]), None
            )
            if sentence and state(sentence, metadata)[0] == "observed":
                event["support_scope"] = "rule_only_outside_learned_scope"
                events.append(event)
    for previous, current in zip(base["sentence_roles"], base["sentence_roles"][1:]):
        current = {**current, "assertion_state": state(current, metadata)[0]}
        adjacent = v4.adjacent_candidates(text, previous, current, mentions, parse(current))
        for event in adjacent:
            event["status"] = "needs_review"
            event["support_scope"] = "adjacent_explicit_reference_review_only"
            events.append(event)
    unique = group_events(events)
    clinical, states = observations(
        text,
        base["sentence_roles"],
        mentions,
        metadata,
        parse,
        encoder,
        models.heads.get("outcome"),
        models.heads.get("evidence"),
    )
    assertions = []
    for event in unique.values():
        if event["assertion_state"] != "observed":
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
                    "rule_score": 1,
                    "triage_reasons": event["triage_reasons"],
                },
            }
        )
    hashes, rules = source_manifest()
    for observation in clinical:
        assertion = observation_assertion(observation)
        if assertion is not None:
            assertions.append(assertion)
    dictionary_version = (
        base["dictionary_version"] + "|mesh:" + metadata["_chemical_lexicon"]["manifest"]["snapshot_sha256"]
    )
    manifest = {
        **base["manifest"],
        "engine_version": ENGINE_VERSION,
        "rules_version": rules,
        "rule_source_hashes": hashes,
        "dictionary_version": dictionary_version,
        "oracle": metadata.get("_gold_entities") is not None,
        "ablation": metadata.get("_ablation", "context"),
        "learned_models": metadata["_learned_models"].manifest,
        "v6_models": models.manifest,
        "frozen_encoder": encoder.manifest,
        "chemical_dictionary": metadata.get("_chemical_lexicon", {}).get("manifest"),
        "artifact_identity": metadata.get("_artifact_identity"),
    }
    pico = []
    for sentence in base["sentence_roles"]:
        for span in outcome_spans(sentence, parse):
            if span["label"] == "outcomes" and "outcome" in models.heads:
                vector = encoder.encode(text, sentence, (span,)).reshape(-1)
                score = (
                    models.heads["outcome"]
                    .probabilities([{str(i): float(v) for i, v in enumerate(vector)}])[0]
                    .get("1", 0)
                )
                span = {**span, "priority_score": float(score)}
                if score < models.manifest["clinical_heads"]["outcome"]["selection"]["threshold"]:
                    continue
            pico.append(span)
    return {
        **base,
        "engine_version": ENGINE_VERSION,
        "rules_version": rules,
        "manifest": manifest,
        "mentions": mentions,
        "dictionary_version": dictionary_version,
        "annotated_spans": pico,
        "relation_comparison_hypotheses": [],
        "events": list(unique.values()),
        "assertions": assertions,
        "entity_hypotheses": hypotheses,
        "normalization_candidates": hypotheses,
        "mention_clusters": clusters,
        "relation_hypotheses": relations,
        "decision_trace": trace,
        "clinical_observations": clinical,
        "sentence_roles": states,
        "pico_spans": pico,
        "summary": summary(states, clinical),
        "benchmark_scopes": scopes,
        "candidate_pairs": [[p["chemical"], p["gene"]] for p in pairs],
        "duplicate_events_grouped": len(events) - len(unique),
        "rejected_entity_candidates": [],
        "abbreviations": definitions,
        "module_validation": {
            "candidate_preservation": "passed",
            "normalization": "candidate_beta",
            "relations": "candidate_beta",
            "clinical_observations": "candidate_beta",
            "nutrition_accuracy": "unmeasured",
        },
        "diagnostics": {
            **base["diagnostics"],
            "candidate_pairs": len(pairs),
            "relation_bags": len(bags),
            "raw_gene_candidates": sum(m["kind"] == "gene" for m in mentions),
            "automatic_gene_candidates": sum(
                m["kind"] == "gene" and m["payload"].get("automatic_selected") for m in mentions
            ),
            "id_unresolved_candidates_kept": sum(not p["gene"].get("normalized_id") for p in pairs),
            "span_filter_rejections": 0,
            "type_selected": sum(r["type_selected"] for r in relations),
            "semantic_verified": sum(r["semantic_verified"] for r in relations),
            "clinical_observations": len(clinical),
        },
    }


def outcome_spans(sentence, parse):
    from .clinical_v6 import outcome_candidates

    old = [s for s in sentence.get("pico_spans", []) if s["label"] != "outcomes"]
    return old + outcome_candidates(sentence, parse(sentence))


def group_events(events):
    """Group only identical scientific keys and retain every quotation."""
    result = {}
    for event in events:
        key = event["event_id"]
        if key not in result:
            # Parser manifests may contain spaCy's immutable list subclasses.
            # Materialize the same JSON representation that is persisted to DB.
            result[key] = json.loads(dumps(event))
        else:
            current = result[key]
            quotes = {
                (q["start"], q["end"], q.get("quote", q.get("text"))): q
                for q in current["evidence_quotes"] + event["evidence_quotes"]
            }
            current["evidence_quotes"] = list(quotes.values())
            current["triage_reasons"] = sorted(
                set(current.get("triage_reasons", []) + event.get("triage_reasons", []))
            )
    return result
