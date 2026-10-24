import hashlib
import json
from pathlib import Path

import pytest

from nutri_literature.dictionaries import load_dictionaries
from nutri_literature import extraction
from nutri_literature.extraction import extract
from nutri_literature.settings import Settings
from nutri_literature.text import sentence_spans


@pytest.fixture
def dictionary(tmp_path):
    return load_dictionaries(
        Settings(tmp_path, Path(__file__).resolve().parents[1] / "config" / "topics.json")
    )


def run(text, dictionary, label="Results", **metadata):
    return extract(text, [{"label": label, "start": 0, "end": len(text)}], metadata, dictionary)


def test_explicit_relation_with_unicode_offsets(dictionary):
    text = "연구 결과: In 120 adults, caffeine significantly reduced systolic blood pressure (p=0.03)."
    result = run(text, dictionary)
    assert result["engine_version"] == "heuristic-v1"
    assertion = result["assertions"][0]
    assert (assertion["subject"], assertion["predicate"], assertion["object"]) == (
        "local:caffeine",
        "decreases",
        "local:systolic-bp",
    )
    assert assertion["payload"]["explicit_relation"] is True
    assert assertion["quote"] == text[assertion["start"] : assertion["end"]]
    for mention in result["mentions"]:
        assert mention["text"] == text[mention["start"] : mention["end"]]
    assert result["context"]["participants"]["value"] == "120 adults"
    assert result["summary"]["limitations"] == "미확인"
    assert "probability" not in json.dumps(result)


@pytest.mark.parametrize(
    "text",
    [
        "The cat slept beside coffee.",
        "CAT scans were used to measure blood pressure.",
        "We drank tea on Tuesday.",
    ],
)
def test_no_gene_false_positive(dictionary, text):
    assert not [m for m in run(text, dictionary)["mentions"] if m["kind"] == "gene"]


def test_ambiguous_cat_unresolved(dictionary):
    genes = [
        m
        for m in run("In human adults, CAT was observed with caffeine.", dictionary)["mentions"]
        if m["kind"] == "gene"
    ]
    assert len(genes) == 1
    assert genes[0]["status"] == "needs_review"
    assert genes[0]["normalized_id"] is None
    assert not run("In human adults, caffeine increased CAT.", dictionary)["assertions"]


@pytest.mark.parametrize(
    "line,polarity,speculative",
    [
        ("In adults, caffeine did not reduce blood pressure.", "negated", False),
        ("In adults, caffeine may reduce blood pressure.", "positive", True),
        ("In adults, caffeine was not significantly associated with blood pressure.", "null", False),
        ("In adults, caffeine did not significantly reduce blood pressure.", "null", False),
        ("In adults, caffeine had no effect on blood pressure.", "null", False),
    ],
)
def test_source_modifiers(dictionary, line, polarity, speculative):
    assertion = run(line, dictionary)["assertions"][0]
    assert assertion["payload"]["polarity"] == polarity
    assert assertion["payload"]["speculative"] is speculative


def test_cooccurrence_is_not_relation(dictionary):
    assert not run("Caffeine, VDR expression and blood pressure were measured in adults.", dictionary)[
        "assertions"
    ]


@pytest.mark.parametrize(
    "label,line,metadata",
    [
        ("Introduction", "Caffeine reduces blood pressure in humans.", {}),
        ("References", "Caffeine reduces blood pressure in humans.", {}),
        ("Methods", "Caffeine reduces blood pressure in humans.", {}),
        ("Results", "Previous studies reported that caffeine reduced blood pressure in humans.", {}),
        ("Results", "Caffeine reduces blood pressure in humans.", {"publication_types": ["Review"]}),
        ("Results", "Caffeine reduces blood pressure in humans.", {"title": "Caffeine: a systematic review"}),
    ],
)
def test_prior_and_review_are_not_primary_findings(dictionary, label, line, metadata):
    assert not run(line, dictionary, label, **metadata)["assertions"]


def test_nonhuman_gene_has_no_hgnc_normalization(dictionary):
    result = run("Vitamin D reduced Vdr gene expression in mice.", dictionary)
    gene = next(m for m in result["mentions"] if m["kind"] == "gene")
    assert gene["payload"]["organism"] == "animal"
    assert gene["normalized_id"] is None
    assert result["context"]["organism"] == "animal"
    assert result["assertions"][0]["object"] == "unresolved-gene:animal:VDR"


def test_fallback_has_no_invented_hgnc_identifier(dictionary):
    result = run("Vitamin D increased VDR gene expression in human cells.", dictionary)
    gene = next(m for m in result["mentions"] if m["kind"] == "gene")
    assert gene["normalized_id"] is None
    assert gene["payload"]["hgnc_verified"] is False
    assert result["dictionary_manifest"]["fallback"] is True
    assert result["assertions"][0]["object"] == "unresolved-gene:human:VDR"


def test_decimals_abbreviations_and_sections_keep_exact_text():
    text = "We used 2.5 mg daily, e.g. caffeine, vs. placebo. BP fell by 3.4 mmHg (p = 0.02)."
    spans = sentence_spans(text)
    assert len(spans) == 2
    assert spans[0]["text"] == "We used 2.5 mg daily, e.g. caffeine, vs. placebo."
    assert all(s["text"] == text[s["start"] : s["end"]] for s in spans)


def test_numeric_context_no_assignment_from_ambiguous_values(dictionary):
    result = run(
        "In 100 adults, participants received vitamin D at 1000 IU daily or 2000 IU daily for 8 weeks. Blood pressure was measured.",
        dictionary,
    )
    assert result["context"]["dose"]["value"] == "미확인"
    assert len(result["context"]["dose"]["candidates"]) == 2
    assert result["context"]["response_time"]["value"] == "for 8 weeks"


def test_measurement_concentration_is_not_dose(dictionary):
    result = run(
        "In 80 adults, serum vitamin D concentration was 20 mg/L, and blood pressure was 120 mmHg.",
        dictionary,
    )
    assert result["context"]["dose"] == "미확인"
    assert result["context"]["participants"]["value"] == "80 adults"


def test_empty_or_missing_source_does_not_fabricate_summary(dictionary):
    result = run("", dictionary)
    assert result["summary"]["method"] == "미확인"
    assert result["summary"]["results"] == []
    assert result["assertions"] == []


def test_passive_relation_has_correct_direction(dictionary):
    assertion = run("In adults, blood pressure was reduced by caffeine.", dictionary)["assertions"][0]
    assert assertion["subject"] == "local:caffeine"
    assert assertion["object"] == "local:blood-pressure"


def test_null_between_relation(dictionary):
    assertion = run(
        "No significant association was found between caffeine and blood pressure in adults.", dictionary
    )["assertions"][0]
    assert assertion["subject"] == "local:caffeine"
    assert assertion["object"] == "local:blood-pressure"
    assert assertion["payload"]["polarity"] == "null"


def test_null_modifier_does_not_leak_to_separate_outcome(dictionary):
    result = run(
        "Caffeine reduced systolic blood pressure, with no effect on diastolic blood pressure in adults.",
        dictionary,
    )
    systolic = next(a for a in result["assertions"] if a["object"] == "local:systolic-bp")
    assert systolic["payload"]["polarity"] == "positive"


def test_numeric_p_value_is_not_interpreted_as_significance(dictionary):
    assertion = run("Caffeine reduced blood pressure (p=0.08) in adults.", dictionary)["assertions"][0]
    assert assertion["payload"]["polarity"] == "positive"
    assert "significant" not in assertion["payload"]


def test_plain_genes_with_unknown_species_are_unresolved(dictionary):
    result = run("Caffeine increased VDR gene expression.", dictionary)
    gene = next(m for m in result["mentions"] if m["kind"] == "gene")
    assert gene["normalized_id"] is None
    assert gene["payload"]["organism"] == "unknown"


def test_rules_version_hashes_actual_source_files_offline(dictionary):
    directory = Path(extraction.__file__).resolve().parent
    expected = (
        "rules-sha256:"
        + hashlib.sha256(
            b"extraction.py\0"
            + (directory / "extraction.py").read_bytes()
            + b"\0text.py\0"
            + (directory / "text.py").read_bytes()
        ).hexdigest()
    )
    result = run("Caffeine reduced blood pressure in adults.", dictionary)
    assert result["rules_version"] == expected == extraction.RULES_VERSION
    assert run("", dictionary)["rules_version"] == expected
    assert result["engine_version"] == "heuristic-v1"
