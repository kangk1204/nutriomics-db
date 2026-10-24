"""Source-only experimental clinical comparisons, without annotation prompts."""

from collections import Counter

from .clinical_v6 import state
from .db import digest, dumps
from .refinement_v6 import clinical_role_candidates, words
from .text import sentence_spans

STOP = set(
    "a an the of and or to in on for with without by from at as is was were be been than versus vs group groups patients subjects treatment treated received receive".split()
)


def arm_key(text):
    return tuple(w for w in words(text) if w not in STOP)


def distinct_arms(first, second):
    """Never manufacture a comparison from the same arm twice."""
    a, b = arm_key(first), arm_key(second)
    return bool(a and b and a != b)


class MemoEncoder:
    """Per-document memoization of the unchanged frozen encoder's exact output."""

    def __init__(self, encoder):
        self.encoder, self.cache = encoder, {}

    def encode(self, text, sentence, entities=()):
        key = (
            digest(text),
            sentence["start"],
            sentence["end"],
            tuple((e["start"], e["end"]) for e in entities),
        )
        if key not in self.cache:
            self.cache[key] = self.encoder.encode(text, sentence, entities)
        return self.cache[key]


def names_for_role(found, limit):
    output = []
    for key, mentions in found.items():
        if not key or len(key) > 50:
            continue
        # Scores order review candidates, not nutrition accuracy probabilities.
        best = max(mentions, key=lambda x: (x["score"], -x["start"]))
        output.append({**best, "mentions": mentions, "count": len(mentions)})
    return sorted(output, key=lambda x: (-x["count"], -x["score"], -len(words(x["text"])), x["start"]))[
        :limit
    ]


def comparisons(document, role_models, role_manifest, evidence_head, threshold, encoder):
    from .clinical_training_v6 import inference_features

    text = document["text"]
    found = clinical_role_candidates(text, role_models, role_manifest)
    roles = {role: names_for_role(values, 4 if role != "outcome" else 12) for role, values in found.items()}
    sentences = sentence_spans(text, document["sections"])
    # Full sentence offsets match the provided-ICO evaluation and training.
    observed = [s for s in sentences if state(s, {})[0] == "observed"]
    encoder = MemoEncoder(encoder)
    proposals, trace = [], Counter()
    for intervention in roles["intervention"]:
        for comparator in roles["comparator"]:
            if not distinct_arms(intervention["text"], comparator["text"]):
                trace["same_arm_rejected"] += 1
                continue
            for outcome in roles["outcome"]:
                if arm_key(outcome["text"]) in {arm_key(intervention["text"]), arm_key(comparator["text"])}:
                    trace["outcome_is_arm_rejected"] += 1
                    continue
                terms = set(words(outcome["text"])) - STOP
                if not terms:
                    continue
                eligible = []
                for sentence in observed:
                    surface = set(words(sentence["text"]))
                    overlap = len(terms & surface) / len(terms)
                    if overlap < 0.6:
                        continue
                    # An outcome anchor alone is insufficient: at least one
                    # distinct intervention/control anchor must occur as well.
                    arm_terms = set(arm_key(intervention["text"])) | set(arm_key(comparator["text"]))
                    arm_terms = {w for w in arm_terms if not w.isdigit() and len(w) > 2}
                    if not arm_terms & surface:
                        continue
                    eligible.append((overlap, sentence))
                if not eligible:
                    continue
                prompt = {
                    "intervention_description": intervention["text"],
                    "comparator_description": comparator["text"],
                    "outcome_description": outcome["text"],
                }
                eligible = sorted(eligible, key=lambda x: (-x[0], x[1]["start"]))[:8]
                scores = evidence_head.probabilities(
                    [inference_features(text, sentence, prompt, encoder) for _, sentence in eligible]
                )
                evidence = [
                    {"start": sentence["start"], "end": sentence["end"], "quote": sentence["text"]}
                    for (_, sentence), score in zip(eligible, scores)
                    if score.get("1", 0) >= threshold
                ]
                trace["triples_scored"] += 1
                if evidence:
                    proposals.append(
                        {
                            **prompt,
                            "evidence": evidence,
                            "score": max(score.get("1", 0) for score in scores),
                            "source_roles": {
                                "intervention": intervention,
                                "comparator": comparator,
                                "outcome": outcome,
                            },
                            "status": "needs_review",
                            "source_only": True,
                            "observation_id": "v6-r1-ico:" + digest(dumps(prompt)),
                        }
                    )
    unique = {}
    for proposal in proposals:
        key = tuple(
            tuple(words(proposal[k]))
            for k in ("intervention_description", "comparator_description", "outcome_description")
        )
        unique.setdefault(key, proposal)
    return list(unique.values()), {
        "role_unique_candidates": {k: len(v) for k, v in found.items()},
        "role_retained_candidates": {k: len(v) for k, v in roles.items()},
        "observed_source_sentences": len(observed),
        **trace,
    }
