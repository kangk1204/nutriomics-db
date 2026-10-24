"""Nutrition observations independent of gene identity, clause-scoped and cited."""

from copy import deepcopy
import re

from .clinical_v4 import PLANNED, OBSERVED, bind_conditions, quantity_spans, summary as mmr_summary
from .db import digest, dumps

PRIOR = re.compile(
    r"\b(?:previous (?:studies|research)|previously reported|earlier (?:studies|investigators)|prior studies|et al\.)\b",
    re.I,
)
MEASURED = re.compile(
    r"\b(?:measured|assessed|evaluated|reported|found|observed|showed|revealed|reduced|lowered|increased|decreased|improved|inhibited|suppressed|unchanged|did not differ|was associated|were associated|differed|correlated)\b",
    re.I,
)
NUMERIC_COMPARISON = re.compile(
    r"\b(?:was|were|averaged|mean|median)\b.{0,90}\d.{0,60}\b(?:versus|vs\.?|compared|than|respectively)\b",
    re.I,
)
RESULT_NOUN = re.compile(
    r"\b(?:reduction|increase|decrease|improvement|association|correlation|inhibition|activation|difference|effect)\b",
    re.I,
)
NO_EFFECT = re.compile(r"\b(?:no (?:effect|change|difference)|unchanged|did not differ)\b", re.I)
NONSIGNIFICANT = re.compile(
    r"\b(?:not (?:statistically )?significant(?:ly)?|no significant|non[- ]significant)\b", re.I
)
NOT_INCREASE = re.compile(r"\b(?:did not|does not|not)\s+(?:significantly\s+)?increase\b", re.I)
OUTCOME = re.compile(
    r"\b(?:systolic blood pressure|diastolic blood pressure|blood pressure|hypertension|methylation|gene expression|expression|activity|concentration|cholesterol|triglycerides|heart rate|mortality|adverse events?|inflammatory markers?|BMI|body weight)\b",
    re.I,
)
SECONDARY = re.compile(r"\b(?:meta[- ]analysis|systematic review|pooled|pooling)\b", re.I)
NEGATED_OBSERVATION = re.compile(
    r"\b(?:did not|was not|were not)\s+(?:significantly\s+)?(?:reduce|reduced|increase|increased|decrease|decreased|improve|improved|affect)\b",
    re.I,
)


def clauses(sentence):
    begin = 0
    for match in re.finditer(r";|\b(?:but|however|whereas)\b", sentence["text"], re.I):
        if match.start() > begin and sentence["text"][begin : match.start()].strip():
            yield {
                **sentence,
                "text": sentence["text"][begin : match.start()],
                "start": sentence["start"] + begin,
                "end": sentence["start"] + match.start(),
            }
        begin = match.end()
    if begin < len(sentence["text"]) and sentence["text"][begin:].strip():
        yield {
            **sentence,
            "text": sentence["text"][begin:],
            "start": sentence["start"] + begin,
            "end": sentence["end"],
        }


def state(sentence, metadata):
    text, section = sentence["text"], sentence.get("label", "")
    if re.search(r"title|references?|bibliograph|background|introduction", section, re.I) or PRIOR.search(
        text
    ):
        return "background", "prior_or_background"
    if PLANNED.search(text):
        return "planned", "future_or_objective"
    if re.search(r"\b(?:may|might|could|hypothesi[sz])\b", text, re.I):
        return "interpretation", "modal_or_hypothesis"
    if re.search(r"methods?|materials?|objective|design", section, re.I):
        return "unknown", "method_not_result"
    secondary = any(
        re.search(r"\breview\b|meta[- ]analysis", str(v), re.I) for v in metadata.get("publication_types", [])
    )
    actual = bool(
        OBSERVED.search(text)
        or NEGATED_OBSERVATION.search(text)
        or NUMERIC_COMPARISON.search(text)
        or (
            RESULT_NOUN.search(text)
            and (re.search(r"\b(?:found|observed|showed|demonstrated|significant|was|were)\b", text, re.I))
        )
    )
    if actual and (
        MEASURED.search(text)
        or RESULT_NOUN.search(text)
        or NEGATED_OBSERVATION.search(text)
        or NUMERIC_COMPARISON.search(text)
    ):
        if secondary and not re.search(
            r"\b(?:we|our|pooled|meta[- ]analysis|summary estimate|included studies)\b", text, re.I
        ):
            return "background", "secondary_prior_result_unresolved"
        return "observed", "secondary_synthesis" if secondary else "primary_observation"
    return "unknown", "measurement_or_comparison_unconfirmed"


def outcome_candidates(sentence, tokens=()):
    found = []
    for span in sentence.get("pico_spans", []):
        if (
            span["label"] == "outcomes"
            and sentence["start"] <= span["start"] < span["end"] <= sentence["end"]
        ):
            found.append({**span, "origin": "pio_crf"})
    for match in OUTCOME.finditer(sentence["text"]):
        found.append(
            {
                "label": "outcomes",
                "start": sentence["start"] + match.start(),
                "end": sentence["start"] + match.end(),
                "text": match.group(),
                "origin": "nutrition_result_phrase",
            }
        )
    # Dependency noun phrases headed by a measured target; source offsets remain native.
    for token in tokens:
        if token.get("dep") not in {"nsubj", "nsubjpass", "dobj", "attr"} or token.get("pos") not in {
            "NOUN",
            "PROPN",
        }:
            continue
        if not sentence["start"] <= token["start"] < token["end"] <= sentence["end"]:
            continue
        if re.fullmatch(r"we|study|patients?|participants?|treatment|placebo", token["text"], re.I):
            continue
        children = [t for t in tokens if t.get("head") == token["i"] and t.get("dep") in {"compound", "amod"}]
        start, end = (
            min([token["start"]] + [t["start"] for t in children]),
            max([token["end"]] + [t["end"] for t in children]),
        )
        if sentence["start"] <= start < end <= sentence["end"]:
            found.append(
                {
                    "label": "outcomes",
                    "start": start,
                    "end": end,
                    "text": sentence["text"][start - sentence["start"] : end - sentence["start"]],
                    "origin": "dependency_result_phrase",
                }
            )
    return list({(s["start"], s["end"]): s for s in found}.values())


def observations(
    text, sentences, mentions, metadata, parse, encoder=None, outcome_ranker=None, evidence_ranker=None
):
    result, sentence_states = [], []
    for sentence in sentences:
        tokens = parse(sentence)
        for clause in clauses(sentence):
            assertion, reason = state(clause, metadata)
            sentence_states.append({**clause, "assertion_state": assertion, "observation_reason": reason})
            if assertion != "observed":
                continue
            outcomes = outcome_candidates(clause, tokens)
            local = [m for m in mentions if clause["start"] <= m["start"] < m["end"] <= clause["end"]]
            interventions = [m for m in local if m["kind"] in {"component", "nutrient", "food", "diet"}]
            interventions.extend(
                {"kind": "component", **span}
                for span in sentence.get("pico_spans", [])
                if span["label"] == "interventions"
                and clause["start"] <= span["start"] < span["end"] <= clause["end"]
                and not any(m["start"] <= span["start"] and span["end"] <= m["end"] for m in interventions)
            )
            for outcome in outcomes:
                reasons = []
                controller = interventions[0] if len(interventions) == 1 else None
                if controller is None:
                    reasons.append(
                        "intervention_unresolved" if not interventions else "multiple_local_interventions"
                    )
                score = None
                if outcome_ranker is not None and encoder is not None:
                    vector = encoder.encode(text, clause, (outcome,)).reshape(-1)
                    score = outcome_ranker.probabilities([{str(i): float(v) for i, v in enumerate(vector)}])[
                        0
                    ].get("1", 0)
                conditions = bind_conditions(
                    {"controller": controller or {}, "subject": (controller or {}).get("text")},
                    clause,
                    local,
                    len(interventions) == 1 and len(outcomes) == 1,
                )
                quantities = quantity_spans(clause)
                measured = [
                    q
                    for q in quantities
                    if q.get("quantity_role")
                    not in {"timepoint", "dose", "sample_size", "p_value", "confidence_interval"}
                ]
                evidence_score = None
                comparator = conditions.get("comparator")
                if evidence_ranker is not None and encoder is not None and controller and comparator:
                    from .clinical_training_v6 import inference_features

                    comparison = {
                        "intervention_description": controller["text"],
                        "comparator_description": comparator["text"],
                        "outcome_description": outcome["text"],
                    }
                    evidence_score = evidence_ranker.probabilities(
                        [inference_features(text, clause, comparison, encoder)]
                    )[0].get("1", 0)
                record = {
                    "intervention": deepcopy(controller),
                    "intervention_candidates": deepcopy(interventions),
                    "outcome": outcome,
                    "comparator": conditions.get("comparator"),
                    "conditions": conditions,
                    "measurement_values": measured,
                    "evidence_quotes": [
                        {"start": clause["start"], "end": clause["end"], "quote": clause["text"]}
                    ],
                    "assertion_state": assertion,
                    "evidence_level": "secondary" if reason == "secondary_synthesis" else "primary",
                    "significance": "not_significant"
                    if NONSIGNIFICANT.search(clause["text"])
                    else "significant"
                    if re.search(r"\bsignificant(?:ly)?\b", clause["text"], re.I)
                    else "unknown",
                    "null_interpretation": "no_effect_reported"
                    if NO_EFFECT.search(clause["text"]) and not NONSIGNIFICANT.search(clause["text"])
                    else "not_increased"
                    if NOT_INCREASE.search(clause["text"])
                    else "non_significant"
                    if NONSIGNIFICANT.search(clause["text"])
                    else None,
                    "status": "needs_review" if reasons else "candidate",
                    "reasons": reasons,
                    "priority_score": score,
                    "scores_are_nutrition_probabilities": False,
                    "ico_evidence_priority_score": evidence_score,
                    "gene_identity_required": False,
                    "verification": {
                        "source": "passed",
                        "observation": "passed",
                        "condition_binding": "deferred" if reasons else "local_only",
                        "automatic_ico_evidence": "scored" if evidence_score is not None else "deferred",
                    },
                }
                key = {
                    "outcome": outcome,
                    "intervention": controller,
                    "conditions": conditions,
                    "evidence": record["evidence_quotes"],
                    "level": record["evidence_level"],
                }
                record["observation_id"] = "observation:" + digest(dumps(key))
                result.append(record)
    return result, sentence_states


def summary(sentences, records):
    anchors = {
        (q["start"], q["end"])
        for r in records
        if r["verification"]["observation"] == "passed"
        for q in r["evidence_quotes"]
    }
    checked = [
        {
            **s,
            "assertion_state": s["assertion_state"]
            if s["assertion_state"] != "observed" or (s["start"], s["end"]) in anchors
            else "unknown",
        }
        for s in sentences
    ]
    diversity = [
        {
            "object": r["outcome"]["text"],
            "direction": r["null_interpretation"],
            "conditions": r["conditions"],
            "evidence_quotes": r["evidence_quotes"],
        }
        for r in records
    ]
    result = mmr_summary(checked, diversity, 0.7)
    result["selection"]["method"] = "verified-observation-condition-tfidf-mmr-v6"
    result["gene_identity_required"] = False
    return result
