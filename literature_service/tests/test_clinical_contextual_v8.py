"""Contextual adapter controls, without pretrained weights or benchmark labels."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest
from nutri_literature.clinical_v6 import outcome_candidates
from nutri_literature.text import sentence_spans

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/clinical_contextual_v8.py"
spec = importlib.util.spec_from_file_location("contextual_v8_script", SCRIPT)
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


class Encoder:
    def __init__(self):
        self.calls = []
    def encode(self, text, sentence, entities):
        self.calls.append(tuple(e["text"] for e in entities))
        return np.array([[1., 0.], [0., 1.]])


class Head:
    def __init__(self, probability=.7):
        self.probability = probability
    def probabilities(self, rows):
        return [{"1": self.probability} for _ in rows]


def document():
    text = "Adults received DHA. Blood pressure decreased."
    source = {"pmid": "1", "text": text, "source_hash": context.sha(text),
              "sections": [{"start": 0, "end": len(text), "label": "Abstract"}]}
    prediction = {"pmid": "1", "source_hash": source["source_hash"], "annotated_spans": [
        {"label": "participants", "start": 0, "end": 6, "text": "Adults"},
        {"label": "interventions", "start": 16, "end": 19, "text": "DHA"}]}
    return source, prediction


def test_new_candidates_are_actually_scored_not_legacy_outcome_replayed():
    source, prediction = document()
    encoder = Encoder()
    result = context.contextual_document(source, prediction, lambda s: [], encoder, Head(), outcome_candidates, .6, sentence_spans)
    assert encoder.calls == [("Blood pressure",)]
    outcomes = [s for s in result["annotated_spans"] if s["label"] == "outcomes"]
    assert len(outcomes) == 1 and outcomes[0]["text"] == "Blood pressure"
    assert not result["original_v6_outcome_predictions_replayed"]
    assert result["diagnostics"]["proposals_nutrition_result_phrase"] == 1


def test_unchanged_head_threshold_applied_to_new_proposals():
    source, prediction = document()
    encoder = Encoder()
    result = context.contextual_document(source, prediction, lambda s: [], encoder, Head(.59), outcome_candidates, .6, sentence_spans)
    assert len(result["annotated_spans"]) == 2
    assert result["diagnostics"]["rejected_nutrition_result_phrase"] == 1
    assert len(encoder.calls) == 1


def test_new_pio_outcome_and_dependency_candidates_share_the_same_frozen_head():
    source, prediction = document()
    a = source["text"].index("decreased")
    prediction["annotated_spans"].append({"label": "outcomes", "start": a, "end": a + 9, "text": "decreased"})
    encoder = Encoder()
    result = context.contextual_document(source, prediction, lambda s: [], encoder, Head(), outcome_candidates, .6, sentence_spans)
    assert len(encoder.calls) == 2
    assert result["diagnostics"]["proposals_pio_crf"] == 1
    assert len([s for s in result["annotated_spans"] if s["label"] == "outcomes"]) == 2


def test_changed_or_invalid_proposal_source_fails_closed():
    source, prediction = document()
    prediction["source_hash"] = "a" * 64
    with pytest.raises(ValueError, match="identity"):
        context.contextual_document(source, prediction, lambda s: [], Encoder(), Head(), outcome_candidates, .6, sentence_spans)
