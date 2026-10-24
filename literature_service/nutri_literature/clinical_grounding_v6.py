"""Source-preserving clinical surfaces and explicit measured comparisons."""

from collections import Counter
from pathlib import Path
import re

from .clinical_boundary_v6 import expanded_roles
from .clinical_refinement_v6 import STOP, MemoEncoder, arm_key, distinct_arms
from .clinical_v6 import state
from .db import digest
from .refinement_v6 import words
from .text import sentence_spans

UNITS = {
    "mg",
    "kg",
    "g",
    "ml",
    "iu",
    "mcg",
    "ug",
    "μg",
    "day",
    "daily",
    "weekly",
    "once",
    "twice",
    "dose",
    "doses",
}
COMPARISON = re.compile(r"\b(?:vs|versus|compared|than|between|relative to)\b", re.I)
FINITE_PAST = re.compile(r"\b(?:had|was|were|reported|experienced|occurred)\b", re.I)


def implementation_sha():
    return digest(Path(__file__).read_bytes())


def source_surface(text, span):
    """Keep attached percent units and balanced closing brackets in the source."""
    start, end = span["start"], span["end"]
    if text[start:end] != span["text"]:
        raise ValueError("Clinical surface/source offset mismatch")
    if end > start and text[end - 1].isdigit():
        suffix = re.match(r"[ \t\u00a0]*%", text[end:])
        if suffix:
            end += suffix.end()
    for opening, closing in (("(", ")"), ("[", "]")):
        while text[start:end].count(opening) > text[start:end].count(closing):
            suffix = re.match(r"[ \t\u00a0]*" + re.escape(closing), text[end:])
            if not suffix:
                break
            end += suffix.end()
    return {**span, "text": text[start:end], "end": end, "surface_adjusted": end != span["end"]}


def measured_comparison(sentence):
    existing = state(sentence, {})[0]
    if existing == "observed":
        return True
    if existing != "unknown":
        return False
    # P-values alone never establish observation. This extension requires a
    # finite past assertion, a comparison and multiple measured numbers.
    return bool(
        FINITE_PAST.search(sentence["text"])
        and COMPARISON.search(sentence["text"])
        and len(re.findall(r"(?<!\w)\d+(?:\.\d+)?", sentence["text"])) >= 2
    )


def arms_grounded(sentence, intervention, comparator):
    if not distinct_arms(intervention, comparator) or not COMPARISON.search(sentence["text"]):
        return False
    surface = set(words(sentence["text"]))
    cores = []
    for description in (intervention, comparator):
        if FINITE_PAST.search(description):
            return False
        core = {w for w in arm_key(description) if w not in UNITS and not w.isdigit()}
        if not core or len(core & surface) / len(core) < 0.8:
            return False
        cores.append(core)
    if cores[0] == cores[1]:
        # Different doses of one drug require explicit numeric support for both
        # arms, rather than inheriting dose identity from the drug name.
        for description in (intervention, comparator):
            quantities = {w for w in words(description) if w.isdigit()}
            if not quantities or not quantities <= surface:
                return False
    return True


def comparisons(document, role_models, role_manifest, boundary, evidence_head, threshold, encoder):
    from .clinical_training_v6 import inference_features

    roles, trace = expanded_roles(document, role_models, role_manifest, boundary)
    text = document["text"]
    roles = {k: [source_surface(text, v) for v in values] for k, values in roles.items()}
    observed = [s for s in sentence_spans(text, document["sections"]) if measured_comparison(s)]
    encoder = MemoEncoder(encoder)
    counters, found = Counter(), []
    for intervention in roles["intervention"]:
        for comparator in roles["comparator"]:
            if not distinct_arms(intervention["text"], comparator["text"]):
                continue
            grounded = [s for s in observed if arms_grounded(s, intervention["text"], comparator["text"])]
            if not grounded:
                continue
            for outcome in roles["outcome"]:
                terms = set(words(outcome["text"])) - STOP
                if not terms or arm_key(outcome["text"]) in {
                    arm_key(intervention["text"]),
                    arm_key(comparator["text"]),
                }:
                    continue
                eligible = []
                for sentence in grounded:
                    overlap = len(terms & set(words(sentence["text"]))) / len(terms)
                    if overlap >= 0.6:
                        eligible.append((overlap, sentence))
                eligible = sorted(eligible, key=lambda x: (-x[0], x[1]["start"]))[:4]
                if not eligible:
                    continue
                prompt = {
                    "intervention_description": intervention["text"],
                    "comparator_description": comparator["text"],
                    "outcome_description": outcome["text"],
                }
                scores = evidence_head.probabilities(
                    [inference_features(text, s, prompt, encoder) for _, s in eligible]
                )
                evidence = [
                    {"start": s["start"], "end": s["end"], "quote": s["text"]}
                    for (_, s), score in zip(eligible, scores)
                    if score.get("1", 0) >= threshold
                ]
                counters["triples_scored"] += 1
                if evidence:
                    found.append(
                        {
                            **prompt,
                            "evidence": evidence,
                            "source_roles": {
                                "intervention": intervention,
                                "comparator": comparator,
                                "outcome": outcome,
                            },
                            "status": "needs_review",
                            "source_only": True,
                        }
                    )
    return found, {
        **trace,
        **counters,
        "retained_roles": {k: len(v) for k, v in roles.items()},
        "source_role_candidates": roles,
        "source_surfaces_restored": sum(v["surface_adjusted"] for values in roles.values() for v in values),
        "measured_sentences": len(observed),
    }
