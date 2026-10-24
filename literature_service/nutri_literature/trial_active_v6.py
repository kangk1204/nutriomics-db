"""Explicit active-comparator roles and 'respectively' dose correspondence.

Source order alone never creates a clinical comparison. A learned role proposal
also requires an explicit directional comparison phrase. All outputs are beta.
"""
from pathlib import Path
import re

from .db import digest
from .refinement_v6 import words
from .trial_recovery_v6 import registry
from .trial_roles_v6 import predict
from .trial_linking_v6 import quote, timepoints, resolve_arms, COMPARE
from .clinical_grounding_v6 import measured_comparison, source_surface
from .clinical_refinement_v6 import STOP, arm_key, distinct_arms
from .text import sentence_spans


def source_sha():
    return digest(Path(__file__).read_bytes())


def explicit_active_pair(sentence, a, b):
    if a.get("source_role") != "intervention" or b.get("source_role") != "comparator":
        return False
    text = sentence["text"].casefold()
    def find(core):
        positions = [m for term in core for m in re.finditer(r"\b" + re.escape(term) + r"\b", text)]
        return (min(m.start() for m in positions), max(m.end() for m in positions)) if positions else None
    left, right = find(a["core"]), find(b["core"])
    if not left or not right or set(a["core"]) == set(b["core"]):
        return False
    if left[1] <= right[0]:
        return bool(re.search(r"\b(?:versus|vs\.?|compared (?:with|to)|than)\b", text[left[1]:right[0]]))
    if right[1] <= left[0]:
        return bool(re.search(r"\bcompared (?:with|to)\s*$", text[:right[0]]))
    return False


def respective_doses(document, arms):
    text = document["text"]
    for arm in arms:
        sentence = arm["source_sentence"]
        if not re.search(r"\brespectively\b", sentence["text"], re.I) or not re.search(r"\b(?:receiv|administer|dose|given|supplement)\w*", sentence["text"], re.I):
            continue
        matches = list(re.finditer(r"(?<!\w)(\d+(?:\.\d+)?)\s*(?:and|,)\s*(\d+(?:\.\d+)?)\s*(mg|mcg|μg|g|ml|IU)\b", sentence["text"], re.I))
        if len(matches) != 1:
            continue
        match = matches[0]
        remainder = sentence["text"][:match.start()] + sentence["text"][match.end():]
        if re.search(r"\d+(?:\.\d+)?\s*(?:mg|mcg|μg|g|ml|IU)\b", remainder, re.I):
            continue
        # Require exactly two distinct named arms in the same source sentence.
        unique = {}
        for candidate in arms:
            if candidate["source_sentence"]["start"] == sentence["start"]:
                unique.setdefault(tuple(candidate["core"]), candidate)
        if len(unique) != 2:
            continue
        ordered = sorted(unique.values(), key=lambda a: a["description"]["start"])
        index = next((i for i, a in enumerate(ordered) if a["core"] == arm["core"]), None)
        if index is None:
            continue
        a, b = sentence["start"] + match.start(index + 1), sentence["start"] + match.end(index + 1)
        unit = quote(text, sentence["start"] + match.start(3), sentence["start"] + match.end(3))
        # Numeric and shared-unit locations remain separate original spans.
        arm["doses"] = [{**quote(text, a, b), "unit_evidence": unit,
                          "assignment": "explicit_two_arm_respectively", "quantity_role": "dose"}]
    return arms


def comparisons(document, heads, trie, inherited=()):
    text = document["text"]
    sentences = sentence_spans(text, document["sections"])
    roles, chemicals = predict(document, heads, trie)
    arms = respective_doses(document, registry(document, roles, chemicals, sentences))
    found = {tuple(tuple(words(v[k + "_description"])) for k in ("intervention", "comparator", "outcome")): dict(v) for v in inherited}
    by_arm = {a["arm_id"]: a for a in arms}
    for comparison in found.values():
        for condition in comparison.get("trial_conditions", []):
            for role in ("intervention", "comparator"):
                arm = by_arm.get(condition[role + "_arm_id"])
                if arm and any(q["assignment"] == "explicit_two_arm_respectively" for q in arm["doses"]):
                    condition[role + "_doses"] = arm["doses"]
    observations = []
    for sentence in sentences:
        if not measured_comparison(sentence) or not COMPARE.search(sentence["text"]):
            continue
        available = resolve_arms(sentence, arms)
        pairs = [(a, b) for a in available for b in available if not a["control"] and not b["control"]
                 and distinct_arms(a["description"]["text"], b["description"]["text"])
                 and explicit_active_pair(sentence, a, b)]
        if not pairs:
            continue
        outcomes = [source_surface(text, o) for o in roles["outcome"] if o["score"] >= .5
                    and (sentence["start"] <= o["start"] < o["end"] <= sentence["end"]
                         or (set(words(o["text"])) - STOP) and (set(words(o["text"])) - STOP) <= set(words(sentence["text"])))]
        unique = {tuple(words(o["text"])): o for o in outcomes}
        outcomes = sorted(unique.values(), key=lambda o: (-o["score"], o["start"]))[:4]
        points = timepoints(document, sentence)
        for a, b in pairs:
            for o in outcomes:
                i, c = a["description"], b["description"]
                if arm_key(o["text"]) in {arm_key(i["text"]), arm_key(c["text"])}:
                    continue
                condition = {"intervention_arm_id": a["arm_id"], "comparator_arm_id": b["arm_id"],
                             "intervention_doses": a["doses"], "comparator_doses": b["doses"],
                             "timepoint": points[0] if len(points) == 1 else None, "timepoint_candidates": points,
                             "hold_reasons": ["multiple_timepoints"] if len(points) > 1 else [],
                             "binding_rule": "learned_roles_plus_explicit_active_comparator_phrase"}
                q = {"start": sentence["start"], "end": sentence["end"], "quote": sentence["text"]}
                key = tuple(tuple(words(v)) for v in (i["text"], c["text"], o["text"]))
                observations.append({"intervention": i, "comparator": c, "outcome": o, "conditions": condition, "evidence": q, "status": "needs_review"})
                if key not in found:
                    found[key] = {"intervention_description": i["text"], "comparator_description": c["text"], "outcome_description": o["text"],
                                  "evidence": [q], "source_roles": {"intervention": i, "comparator": c, "outcome": o},
                                  "status": "needs_review", "source_only": True, "trial_conditions": [condition]}
    return list(found.values()), {"arm_registry": arms, "observations": observations, "source_role_candidates": roles,
                                "new_comparisons": len(found) - len(inherited), "inherited_comparisons": len(inherited),
                                "conditions_are_human_validated": False}
