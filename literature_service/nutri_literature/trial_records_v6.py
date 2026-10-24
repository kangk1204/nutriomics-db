"""Occurrence-preserving trial records; identity includes regimen, not just name.

These structured records are independent of strict free-text ICO prediction.
No gene ID is needed. Multiple source interpretations and unknown conditions
remain unresolved rather than becoming a shared study arm.
"""
from collections import defaultdict
from pathlib import Path
import re

from .db import digest, dumps
from .clinical_grounding_v6 import source_surface, measured_comparison
from .trial_linking_v6 import quote, semantic_core, DOSE, timepoints, CONTROL
from .trial_active_v6 import respective_doses
from .text import sentence_spans

DURATION = re.compile(r"\bfor\s+\d+(?:\.\d+)?\s*(?:hours?|days?|weeks?|months?|years?)\b", re.I)
FREQUENCY = re.compile(r"\b(?:(?:once|twice|three times)\s+(?:a |per )?(?:day|daily|week|weekly)|daily|weekly|every\s+\d+\s*(?:hours?|days?|weeks?))\b", re.I)
ROUTE = re.compile(r"\b(?:orally|oral|intravenous(?:ly)?|intramuscular(?:ly)?|subcutaneous(?:ly)?|topical(?:ly)?)\b", re.I)
ADMINISTER = re.compile(r"\b(?:receiv|administer|randomi[sz]|assign|allocat|dose|given|supplement|treat)\w*", re.I)


def source_sha():
    return digest(Path(__file__).read_bytes())


def core_for(span, chemicals):
    inside = [c for c in chemicals if span["start"] <= c["start"] < c["end"] <= span["end"]]
    selected = []
    for c in sorted(inside, key=lambda m: (-(m["end"] - m["start"]), m["start"])):
        if not any(c["start"] < p["end"] and p["start"] < c["end"] for p in selected):
            selected.append(c)
    if selected:
        if re.search(r"\b(?:and|plus|combined with)\b|[+]", span["text"], re.I):
            return sorted(set().union(*(semantic_core(c["text"]) for c in selected)))
        return sorted(semantic_core(max(selected, key=lambda c: c["end"] - c["start"])["text"]))
    return sorted(semantic_core(span["text"]))


def build_registry(document, roles, chemicals):
    text = document["text"]
    sentences = sentence_spans(text, document["sections"])
    instances = []
    for role in ("intervention", "comparator"):
        for candidate in roles[role]:
            span = source_surface(text, candidate)
            s = next((s for s in sentences if s["start"] <= span["start"] < span["end"] <= s["end"]), None)
            if not s or candidate.get("score", 1) < .5 or not ADMINISTER.search(s["text"]):
                continue
            if re.search(r"references|background|introduction|discussion", s.get("label", ""), re.I):
                continue
            core = core_for(span, chemicals)
            if not core:
                continue
            # Merge only overlapping alternative boundaries of this occurrence.
            existing = next((a for a in instances if a["core"] == core and a["sentence"]["start"] == s["start"]
                             and any(span["start"] < v["end"] and v["start"] < span["end"] for v in a["descriptions"])), None)
            if existing:
                existing["descriptions"].append(span)
                existing["role_candidates"] = sorted(set(existing["role_candidates"]) | {role})
            else:
                instances.append({"core": core, "descriptions": [span], "role_candidates": [role], "sentence": s})
    records = []
    for a in instances:
        sentence = a["sentence"]
        description = max(a["descriptions"], key=lambda v: (v["score"], v["end"] - v["start"]))
        overlapping = [b for b in instances if b is not a and b["sentence"]["start"] == sentence["start"] and b["core"] != a["core"]
                       and any(x["start"] < y["end"] and y["start"] < x["end"] for x in a["descriptions"] for y in b["descriptions"])]
        record = {"description": description, "descriptor_alternatives": a["descriptions"], "core": a["core"],
                  "role_candidates": a["role_candidates"], "source_sentence": quote(text, sentence["start"], sentence["end"]),
                  "doses": [], "duration_candidates": [], "frequency_candidates": [], "route_candidates": [], "unassigned_quantities": [],
                  "hold_reasons": ["overlapping_arm_interpretations"] if overlapping else [],
                  "control": bool(CONTROL.search(description["text"]))}
        def distance(instance, q):
            return min(max(v["start"] - q["end"], q["start"] - v["end"], 0) for v in instance["descriptions"])
        for m in DOSE.finditer(sentence["text"]):
            q = quote(text, sentence["start"] + m.start(), sentence["start"] + m.end())
            gap = distance(a, q)
            competitors = [b for b in instances if b is not a and b["sentence"]["start"] == sentence["start"] and distance(b, q) <= gap]
            context = text[max(sentence["start"], q["start"] - 35):q["start"]]
            per_volume = re.match(r"\s*/\s*(?:ml|l)\b", text[q["end"]:], re.I)
            concentration = re.search(r"\b(?:serum|plasma|blood|concentration)\b", context, re.I)
            if gap <= 32 and not competitors and not overlapping and not per_volume and not concentration:
                record["doses"].append({**q, "assignment": "unique_source_arm_occurrence", "quantity_role": "dose"})
            else:
                record["unassigned_quantities"].append({**q, "hold_reason": "nonunique_or_nonadministration_quantity"})
        record["duration_candidates"] = [quote(text, sentence["start"] + m.start(), sentence["start"] + m.end()) for m in DURATION.finditer(sentence["text"])]
        record["frequency_candidates"] = [quote(text, sentence["start"] + m.start(), sentence["start"] + m.end()) for m in FREQUENCY.finditer(sentence["text"])]
        record["route_candidates"] = [quote(text, sentence["start"] + m.start(), sentence["start"] + m.end()) for m in ROUTE.finditer(sentence["text"])]
        if len(record["duration_candidates"]) > 1:
            record["hold_reasons"].append("multiple_administration_durations")
        records.append(record)
    records = respective_doses(document, records)
    for record in records:
        signature = {"core": record["core"], "doses": [(q["text"], q.get("unit_evidence", {}).get("text")) for q in record["doses"]],
                     "duration": [d["text"] for d in record["duration_candidates"]],
                     "frequency": [d["text"].casefold() for d in record["frequency_candidates"]],
                     "route": [d["text"].casefold() for d in record["route_candidates"]]}
        record["regimen_signature"] = digest(dumps(signature)) if record["doses"] and not record["hold_reasons"] else None
        record["regimen_complete"] = bool(record["doses"] and not record["hold_reasons"] and all(
            len(record[key]) == 1 for key in ("duration_candidates", "frequency_candidates", "route_candidates")))
        record["arm_instance_id"] = "arm-instance:" + digest(str(document["pmid"]) + dumps(signature)
            + dumps([(v["start"], v["end"]) for v in record["descriptor_alternatives"]]))
        record["status"] = "needs_review"
    return records


def link_results(document, registry):
    records = []
    for sentence in sentence_spans(document["text"], document["sections"]):
        if not measured_comparison(sentence):
            continue
        words = set(re.findall(r"\w+", sentence["text"].casefold()))
        named = [a for a in registry if set(a["core"]) <= words]
        grouped = defaultdict(list)
        for a in named:
            grouped[tuple(a["core"])].append(a)
        for _, candidates in grouped.items():
            complete = {a["regimen_signature"] for a in candidates if a["regimen_signature"]}
            held = any(a["hold_reasons"] for a in candidates)
            uniquely_same = len(candidates) == 1 or (len(complete) == 1 and all(a["regimen_complete"] for a in candidates))
            assigned = candidates if uniquely_same and not held else []
            if not assigned:
                # An explicit result dose can resolve a named multi-dose arm.
                source_doses = {m.group().casefold() for m in DOSE.finditer(sentence["text"])}
                matching = [a for a in candidates if a["doses"] and all(q["text"].casefold() in source_doses for q in a["doses"]) and not a["hold_reasons"]]
                if len(matching) == 1:
                    assigned = matching
            points = timepoints(document, sentence)
            records.append({"evidence": quote(document["text"], sentence["start"], sentence["end"]),
                "arm_instance_ids": [a["arm_instance_id"] for a in assigned],
                "arm_candidates": [a["arm_instance_id"] for a in candidates],
                "timepoint": points[0] if len(points) == 1 else None, "timepoint_candidates": points,
                "hold_reasons": ([] if assigned else ["multiple_regimens_or_unknown_conditions"]) + (["multiple_followup_times"] if len(points) > 1 else []),
                "status": "needs_review", "source_only": True})
    return records
