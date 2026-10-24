"""Source-only trial-arm registry and result/dose/time binding (experimental R4).

All descriptions and quantities retain exact character locations. A shared drug
name never resolves different dose arms. Duration of administration is not a
follow-up time. Ambiguous links stay explicit review candidates.
"""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from pathlib import Path
import re

from .clinical_boundary_v6 import span_features, variants
from .clinical_grounding_v6 import source_surface, measured_comparison
from .clinical_refinement_v6 import STOP, arm_key, distinct_arms
from .clinical_v4 import quantity_spans
from .db import digest
from .refinement_v6 import clinical_role_candidates, tokens, words
from .text import sentence_spans

CONTROL = re.compile(r"\b(?:placebo|control|usual care|standard care|sham|no treatment)\b", re.I)
METHOD = re.compile(r"\b(?:randomi[sz]|assign|allocat|receiv|administer|treat|supplement|dose|arm|group)", re.I)
FINITE = re.compile(r"\b(?:was|were|had|have|has|showed|reported|received|assigned|randomized|randomised)\b", re.I)
COMPARE = re.compile(r"\b(?:versus|vs\.?|compared|than|between|both groups|two groups|respectively)\b", re.I)
GENERIC = {"group", "groups", "arm", "arms", "patient", "patients", "treatment", "treatments", "study", "subjects", "participants", "therapy"}
DOSE = re.compile(r"(?<!\w)\d+(?:\.\d+)?\s*(?:mg|mcg|ug|μg|g|ml|mL|IU|U)(?:\s*/\s*(?:kg|day|d))?\b|(?<!\w)\d+(?:\.\d+)?\s*%", re.I)
TIME = re.compile(r"\b(?:at|after|by|through|during)\s+(?:the\s+)?(?:end of\s+)?\d+(?:\.\d+)?\s*(?:hours?|days?|weeks?|months?|years?)\b|\b(?:at\s+)?(?:day|week|month|year)\s+\d+(?:\.\d+)?\b", re.I)


def source_sha():
    return digest(Path(__file__).read_bytes())


def quote(text, start, end):
    if not 0 <= start < end <= len(text):
        raise ValueError("Invalid trial source span")
    return {"start": start, "end": end, "text": text[start:end]}


def semantic_core(surface):
    return set(arm_key(surface)) - GENERIC - {"mg", "mcg", "ug", "g", "ml", "iu", "daily", "day", "twice", "once", "weekly", "dose", "doses"} - {w for w in words(surface) if w.isdigit()}


def role_pool(document, role_models, manifest, boundary):
    text = document["text"]
    sequence = tokens(text)
    starts, ends = [t["start"] for t in sequence], [t["end"] for t in sequence]
    sentences = sentence_spans(text, document["sections"])
    found = clinical_role_candidates(text, role_models, manifest)
    seeds = {"arms": set(), "outcome": set()}
    for role, phrases in found.items():
        for mentions in phrases.values():
            for m in mentions:
                seeds["outcome" if role == "outcome" else "arms"].add((bisect_left(starts, m["start"]), bisect_right(ends, m["end"])))
    for match in re.finditer(r"\b[A-Za-z][\w-]*\s+\d+(?:\.\d+)?\s*(?:mg|g|μg|mcg|IU|%)|\bplacebo\b|\busual care\b|\bsham\b", text, re.I):
        seeds["arms"].add((bisect_left(starts, match.start()), bisect_right(ends, match.end())))
    ranges = [(bisect_left(starts, s["start"]), bisect_right(ends, s["end"]), s) for s in sentences]
    pools = {"arms": [], "outcome": []}
    for role in pools:
        candidates = {}
        for lo, hi in seeds[role]:
            entry = next((r for r in ranges if r[0] <= lo < hi <= r[1]), None)
            if entry is None:
                continue
            for a, b in variants(lo, hi, len(sequence), entry[:2]):
                if b - a > (18 if role == "arms" else 28):
                    continue
                span = source_surface(text, quote(text, sequence[a]["start"], sequence[b - 1]["end"]))
                if FINITE.search(span["text"]) or ";" in span["text"] or not semantic_core(span["text"]):
                    continue
                if role == "arms" and (len(semantic_core(span["text"])) > 10 or not METHOD.search(entry[2]["text"])):
                    continue
                candidates[(a, b)] = (span, entry[2])
        keys = sorted(candidates)
        scores = boundary[role].scores([span_features(text, sequence, a, b, role) for a, b in keys])
        # Keep boundaries locally, rather than letting unrelated high-scoring
        # phrases elsewhere in a long article evict a trial's actual arms.
        grouped = defaultdict(list)
        for key, score in zip(keys, scores):
            span, sentence = candidates[key]
            grouped[sentence["start"]].append({**span, "score": score, "sentence_start": sentence["start"], "sentence_end": sentence["end"], "origin": "source-local-boundary-r4"})
        for values in grouped.values():
            selected = sorted(values, key=lambda m: (-m["score"], m["start"], m["end"]))
            # Distinct semantic cores keep multiarm alternatives alive. Up to
            # two descriptor boundaries per core are review hypotheses.
            counts = Counter()
            for m in selected:
                key = tuple(sorted(semantic_core(m["text"])))
                if counts[key] < 2:
                    pools[role].append(m)
                    counts[key] += 1
                if sum(counts.values()) >= (8 if role == "arms" else 6):
                    break
    return pools, sentences


def bind_arm_conditions(document, arms, sentences):
    text = document["text"]
    registry = []
    for arm in arms:
        sentence = next(s for s in sentences if s["start"] == arm["sentence_start"])
        # Other long boundary variants of this same name are not independent
        # competitors; only different source cores can make a dose ambiguous.
        own_numbers = set(re.findall(r"\d+(?:\.\d+)?", arm["text"]))
        others = [a for a in arms if a["sentence_start"] == sentence["start"] and (
            semantic_core(a["text"]) != semantic_core(arm["text"])
            or set(re.findall(r"\d+(?:\.\d+)?", a["text"])) != own_numbers)]
        doses, unassigned = [], []
        for match in DOSE.finditer(sentence["text"]):
            q = quote(text, sentence["start"] + match.start(), sentence["start"] + match.end())
            contained = arm["start"] <= q["start"] and q["end"] <= arm["end"]
            gap = max(arm["start"] - q["end"], q["start"] - arm["end"], 0)
            competitors = [a for a in others if max(a["start"] - q["end"], q["start"] - a["end"], 0) <= gap]
            if contained or (gap <= 32 and not competitors and not DOSE.search(arm["text"])):
                doses.append({**q, "assignment": "contained_in_arm" if contained else "unique_local_arm", "quantity_role": "dose"})
            else:
                unassigned.append({**q, "hold_reason": "multiple_arms_or_nonlocal_dose"})
        key = tuple(words(arm["text"]))
        registry.append({"arm_id": "trial-arm:" + digest(str(document["pmid"]) + repr(key)),
                         "description": arm, "core": sorted(semantic_core(arm["text"])),
                         "control": bool(CONTROL.search(arm["text"])), "doses": doses,
                         "unassigned_doses": unassigned, "source_sentence": quote(text, sentence["start"], sentence["end"]),
                         "species_inferred": False})
    return registry


def timepoints(document, sentence):
    return [quote(document["text"], sentence["start"] + m.start(), sentence["start"] + m.end()) for m in TIME.finditer(sentence["text"])]


def resolve_arms(sentence, registry):
    terms = set(words(sentence["text"]))
    candidates = []
    for arm in registry:
        core = set(arm["core"])
        if not core or not core <= terms:
            continue
        dose_numbers = set(re.findall(r"\d+(?:\.\d+)?", arm["description"]["text"]))
        # Dose variants of a compound cannot inherit identity from its name.
        siblings = [a for a in registry if a["core"] == arm["core"] and set(re.findall(r"\d+(?:\.\d+)?", a["description"]["text"])) != dose_numbers]
        if siblings and (not dose_numbers or not dose_numbers <= terms):
            continue
        candidates.append(arm)
    return candidates


def comparisons(document, role_models, manifest, boundary, inherited=()):
    pools, sentences = role_pool(document, role_models, manifest, boundary)
    registry = bind_arm_conditions(document, pools["arms"], sentences)
    found = {tuple(tuple(words(v[k + "_description"])) for k in ("intervention", "comparator", "outcome")): dict(v) for v in inherited}
    observations, counters = [], Counter()
    for sentence in sentences:
        if not measured_comparison(sentence) or not COMPARE.search(sentence["text"]):
            continue
        available = resolve_arms(sentence, registry)
        active = [a for a in available if not a["control"]]
        controls = [a for a in available if a["control"]]
        # No direction from relative position: explicit active/control roles,
        # or an unambiguous source 'A versus B' is required.
        pairs = [(a, c) for a in active for c in controls if distinct_arms(a["description"]["text"], c["description"]["text"])]
        if not pairs:
            counters["arm_pair_unresolved"] += 1
            continue
        outcomes = [o for o in pools["outcome"] if o["sentence_start"] == sentence["start"]]
        if not outcomes:
            # Explicit cross-sentence outcome names may be linked only by exact
            # lexical identity; the record keeps the original descriptor span.
            terms = set(words(sentence["text"]))
            outcomes = [o for o in pools["outcome"] if (set(words(o["text"])) - STOP) and (set(words(o["text"])) - STOP) <= terms]
        outcomes = sorted(outcomes, key=lambda o: (-o["score"], o["start"]))[:6]
        points = timepoints(document, sentence)
        for active_arm, comparator in pairs:
            for outcome in outcomes:
                i, c = active_arm["description"], comparator["description"]
                if arm_key(outcome["text"]) in {arm_key(i["text"]), arm_key(c["text"])}:
                    continue
                key = tuple(tuple(words(v)) for v in (i["text"], c["text"], outcome["text"]))
                q = {"start": sentence["start"], "end": sentence["end"], "quote": sentence["text"]}
                condition = {"intervention_arm_id": active_arm["arm_id"], "comparator_arm_id": comparator["arm_id"],
                             "intervention_doses": active_arm["doses"], "comparator_doses": comparator["doses"],
                             "timepoint": points[0] if len(points) == 1 else None,
                             "timepoint_candidates": points, "hold_reasons": ["multiple_timepoints"] if len(points) > 1 else [],
                             "binding_rule": "explicit_source_core_and_active_control_roles"}
                observations.append({"intervention": i, "comparator": c, "outcome": outcome, "conditions": condition, "evidence": q, "status": "needs_review"})
                if key not in found:
                    found[key] = {"intervention_description": i["text"], "comparator_description": c["text"], "outcome_description": outcome["text"],
                                  "evidence": [q], "source_roles": {"intervention": i, "comparator": c, "outcome": outcome},
                                  "status": "needs_review", "source_only": True, "trial_conditions": [condition]}
                elif not any(v["start"] == q["start"] and v["end"] == q["end"] for v in found[key]["evidence"]):
                    found[key]["evidence"].append(q)
                    found[key].setdefault("trial_conditions", []).append(condition)
    return list(found.values()), {"arm_registry": registry, "observations": observations,
        "pool_counts": {k: len(v) for k, v in pools.items()}, "source_role_candidates": pools,
        "counters": dict(counters), "inherited_comparisons": len(inherited),
        "new_comparisons": len(found) - len(inherited), "conditions_are_human_validated": False}
