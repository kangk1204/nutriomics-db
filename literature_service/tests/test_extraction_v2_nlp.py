"""Pinned-model CPU regressions. Skipped when NLP dependency is unavailable.

These synthetic sentences validate actual model/rule integration; they do not
constitute a scientific gold corpus or establish relation accuracy.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from nutri_literature.dictionaries import Dictionaries, _insert
from nutri_literature.extraction_v2 import extract
from nutri_literature.nlp_v2 import CpuParser

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("en_core_sci_sm") is None, reason="pinned CPU NLP model is not installed"
)


@pytest.fixture(scope="module")
def dictionary():
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "config/entities-v2.json").read_text(encoding="utf-8")
    )
    genes, entities = {}, {}
    for value in config["entities"]:
        for term in value["terms"]:
            _insert(entities, term, {**value, "term": term})
    gene = {"id": "HGNC:12679", "symbol": "VDR", "name": "vitamin D receptor", "aliases": []}
    for term, kind in [(gene["symbol"], "symbol"), (gene["name"], "name")]:
        _insert(genes, term, {**gene, "term": term, "form_kind": kind})
    return Dictionaries(
        "synthetic-integration-fixture", {"source": "synthetic engineering fixture"}, genes, entities
    )


@pytest.fixture(scope="module")
def parser():
    return CpuParser()


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "Caffeine reduced blood pressure in adults.",
            {
                "subject": "CHEBI:27732",
                "object": "local:blood-pressure",
                "direction": "decrease",
                "negation": False,
                "speculation": False,
            },
        ),
        (
            "Blood pressure was reduced by caffeine in adults.",
            {"subject": "CHEBI:27732", "object": "local:blood-pressure", "direction": "decrease"},
        ),
        (
            "Caffeine did not significantly reduce blood pressure.",
            {"direction": "decrease", "significance": "not_significant", "negation": False},
        ),
        ("Caffeine may reduce blood pressure.", {"direction": "decrease", "speculation": True}),
        (
            "Caffeine had no effect on blood pressure.",
            {"direction": "no_effect", "negation": False, "polarity": "null"},
        ),
        (
            "Vitamin D3 increased VDR gene expression in human cells.",
            {
                "subject": "CHEBI:28940",
                "object": "HGNC:12679",
                "study_model": "cell",
                "species": "human",
                "outcome_type": "expression",
            },
        ),
        (
            "Caffeine was associated with blood pressure.",
            {"subject": "CHEBI:27732", "object": "local:blood-pressure", "direction": "association"},
        ),
        (
            "No significant association was found between caffeine and blood pressure in adults.",
            {
                "subject": "CHEBI:27732",
                "object": "local:blood-pressure",
                "significance": "not_significant",
                "negation": False,
            },
        ),
    ],
)
def test_pinned_actual_parser_event_roles(dictionary, parser, line, expected):
    # Actual CpuParser instances remain production mode; dictionary still
    # records fixture provenance so these examples are never clinical gold.
    result = extract(
        line, [{"label": "Results", "start": 0, "end": len(line)}], dictionary, {"_parser": parser}
    )
    assert result["status"] == "complete", result["abstentions"]
    assert len(result["assertions"]) == 1, result["abstentions"]
    event = result["assertions"][0]["payload"]["event"]
    for key, value in expected.items():
        assert event[key] == value
    assert result["manifest"]["parser"]["mode"] == "production"
    assert len(result["manifest"]["parser"]["model_sha256"]) == 64


def test_pinned_actual_parser_contrast_and_coordination(dictionary, parser):
    contrast = "Caffeine reduced systolic blood pressure but did not change diastolic blood pressure."
    result = extract(
        contrast, [{"label": "Results", "start": 0, "end": len(contrast)}], dictionary, {"_parser": parser}
    )
    assert [e["negation"] for e in result["events"]] == [False, True]
    coordination = "Caffeine and calcitriol reduced blood pressure."
    result = extract(
        coordination,
        [{"label": "Results", "start": 0, "end": len(coordination)}],
        dictionary,
        {"_parser": parser},
    )
    assert {e["subject"] for e in result["events"]} == {"CHEBI:27732", "CHEBI:17823"}
