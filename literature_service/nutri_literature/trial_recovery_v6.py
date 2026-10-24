"""Learned regimen descriptors + source-defined trial arms and local follow-up.

The strict ICO endpoint is unchanged. Registry hypotheses are review candidates;
source locations and explicit ambiguity survive every connection.
"""
from collections import defaultdict
from pathlib import Path
import re

from .db import digest
from .refinement_v6 import words
from .trial_roles_v6 import predict
from .trial_linking_v6 import (quote, CONTROL, METHOD, DOSE, semantic_core, bind_arm_conditions,
                               timepoints, resolve_arms, COMPARE)
from .clinical_grounding_v6 import measured_comparison, source_surface
from .clinical_refinement_v6 import STOP, arm_key, distinct_arms
from .text import sentence_spans


def source_sha():
    return digest(Path(__file__).read_bytes())


def registry(document, roles, chemicals, sentences):
    text = document["text"]
    arms = []
    for role in ("intervention", "comparator"):
        for span in roles[role]:
            sentence = next((s for s in sentences if s["start"] <= span["start"] < span["end"] <= s["end"]), None)
            if sentence is None or span["score"] < .5:
                continue
            # Nontrial/background passages cannot establish trial membership.
            if re.search(r"introduction|background|references|discussion", sentence.get("label", ""), re.I) and not re.search(r"randomi[sz]|allocat|assign", sentence["text"], re.I):
                continue
            if not METHOD.search(sentence["text"]):
                continue
            span = source_surface(text, span)
            terms = [c for c in chemicals if span["start"] <= c["start"] < c["end"] <= span["end"]]
            core = None
            if terms:
                # Longest source chemical phrase, avoiding incidental unit or
                # solvent fragments inside a complete regimen descriptor.
                term = max(terms, key=lambda c: (len(words(c["text"])), c["end"] - c["start"]))
                core = semantic_core(term["text"])
            if not core:
                core = semantic_core(span["text"])
            if not core or len(core) > 12:
                continue
            arms.append({**span, "sentence_start": sentence["start"], "sentence_end": sentence["end"],
                         "canonical_core": sorted(core), "role": role})
    # An explicit source placebo/control is independently grounded even if the
    # CRF prefers an overlong boundary. No comparator is invented from absence.
    for sentence in sentences:
        if not METHOD.search(sentence["text"]):
            continue
        if re.search(r"introduction|background|references|discussion", sentence.get("label", ""), re.I):
            continue
        for match in re.finditer(r"\b(?:placebo|usual care|standard care|sham)\b", sentence["text"], re.I):
            span = quote(text, sentence["start"] + match.start(), sentence["start"] + match.end())
            arms.append({**span, "score": .8, "sentence_start": sentence["start"], "sentence_end": sentence["end"],
                         "canonical_core": sorted(semantic_core(span["text"])), "role": "comparator"})
    # Retain two source descriptor alternatives per core + exact regimen dose.
    # Multiple distinct doses remain distinct study arms.
    grouped = defaultdict(dict)
    for arm in arms:
        key = (tuple(arm["canonical_core"]), tuple(m.group().casefold() for m in DOSE.finditer(arm["text"])), bool(CONTROL.search(arm["text"])))
        text_key = tuple(words(arm["text"]))
        if text_key not in grouped[key] or grouped[key][text_key]["score"] < arm["score"]:
            grouped[key][text_key] = arm
    selected = []
    for values in grouped.values():
        selected.extend(sorted(values.values(), key=lambda a: (-a["score"], a["start"]))[:2])
    registered = bind_arm_conditions(document, selected, sentences)
    for original, arm in zip(selected, registered):
        arm["core"] = original["canonical_core"]
        arm["source_role"] = original["role"]
    return registered


def comparisons(document, heads, trie, inherited=()):
    text = document["text"]
    sentences = sentence_spans(text, document["sections"])
    roles, chemicals = predict(document, heads, trie)
    arms = registry(document, roles, chemicals, sentences)
    found = {tuple(tuple(words(v[k + "_description"])) for k in ("intervention", "comparator", "outcome")): dict(v) for v in inherited}
    observations = []
    core_groups = {(tuple(a["core"]), tuple(q["text"].casefold() for q in a["doses"]), a["control"]) for a in arms}
    for sentence in sentences:
        if not measured_comparison(sentence) or not COMPARE.search(sentence["text"]):
            continue
        available = resolve_arms(sentence, arms)
        linkage = "both_explicit_named_arms"
        if not (any(not a["control"] for a in available) and any(a["control"] for a in available)) and len(core_groups) == 2 and sum(k[2] for k in core_groups) == 1 and re.search(r"\b(?:both|two|between|treatment|placebo)\b.*\bgroups?\b", sentence["text"], re.I):
            available = arms
            linkage = "unique_document_two_arm_group_reference"
        pairs = [(a, b) for a in available for b in available if not a["control"] and b["control"]
                 and distinct_arms(a["description"]["text"], b["description"]["text"])]
        if not pairs:
            continue
        outcomes = []
        sentence_words = set(words(sentence["text"]))
        for span in roles["outcome"]:
            surface = source_surface(text, span)
            terms = set(words(surface["text"])) - STOP
            local = sentence["start"] <= surface["start"] < surface["end"] <= sentence["end"]
            if surface["score"] >= .5 and terms and (local or terms <= sentence_words):
                outcomes.append(surface)
        unique = {}
        for o in outcomes:
            key = tuple(words(o["text"]))
            if key not in unique or unique[key]["score"] < o["score"]:
                unique[key] = o
        outcomes = sorted(unique.values(), key=lambda o: (-o["score"], o["start"]))[:4]
        points = timepoints(document, sentence)
        for a, b in pairs:
            for o in outcomes:
                i, c = a["description"], b["description"]
                if arm_key(o["text"]) in {arm_key(i["text"]), arm_key(c["text"])}:
                    continue
                q = {"start": sentence["start"], "end": sentence["end"], "quote": sentence["text"]}
                condition = {"intervention_arm_id": a["arm_id"], "comparator_arm_id": b["arm_id"],
                             "intervention_doses": a["doses"], "comparator_doses": b["doses"],
                             "timepoint": points[0] if len(points) == 1 else None,
                             "timepoint_candidates": points, "hold_reasons": ["multiple_timepoints"] if len(points) > 1 else [],
                             "binding_rule": linkage}
                key = tuple(tuple(words(v)) for v in (i["text"], c["text"], o["text"]))
                observations.append({"intervention": i, "comparator": c, "outcome": o, "conditions": condition, "evidence": q, "status": "needs_review"})
                if key not in found:
                    found[key] = {"intervention_description": i["text"], "comparator_description": c["text"], "outcome_description": o["text"],
                                  "evidence": [q], "source_roles": {"intervention": i, "comparator": c, "outcome": o},
                                  "status": "needs_review", "source_only": True, "trial_conditions": [condition]}
                elif not any(e["start"] == q["start"] and e["end"] == q["end"] for e in found[key]["evidence"]):
                    found[key]["evidence"].append(q)
                    found[key].setdefault("trial_conditions", []).append(condition)
    return list(found.values()), {"arm_registry": arms, "observations": observations, "source_role_candidates": roles,
                                "new_comparisons": len(found) - len(inherited), "inherited_comparisons": len(inherited),
                                "conditions_are_human_validated": False}
