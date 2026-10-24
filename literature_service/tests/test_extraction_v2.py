"""Synthetic engineering regressions, never scientific gold/accuracy evidence."""

import hashlib
import json
from pathlib import Path

import pytest

from nutri_literature.dictionaries import Dictionaries, _insert
from nutri_literature.extraction_v2 import extract, abbreviations, _group_key, RULES_VERSION, _SOURCES
from nutri_literature.nlp_v2 import ParserUnavailable, validate_tokens
from nutri_literature.summary_v2 import summarize
from nutri_literature.text import sentence_spans

ROOT = Path(__file__).resolve().parents[1]


class DependencyFixture:
    """Declared dependencies, not an NLP model or a nearest-word fallback."""

    manifest = {
        "name": "synthetic-dependencies",
        "version": "test-only",
        "available": True,
        "mode": "fixture",
    }

    def __init__(self, specs):
        self.specs = specs

    def parse(self, sentence):
        result, cursor = [], 0
        specs = self.specs[sentence["text"]] if isinstance(self.specs, dict) else self.specs
        for index, (word, head, dep, lemma, pos) in enumerate(specs):
            start = sentence["text"].index(word, cursor)
            end = start + len(word)
            cursor = end
            result.append(
                {
                    "i": index,
                    "text": word,
                    "start": sentence["start"] + start,
                    "end": sentence["start"] + end,
                    "head": head,
                    "dep": dep,
                    "lemma": lemma,
                    "pos": pos,
                }
            )
        return result


@pytest.fixture
def dictionaries_v2():
    config = json.loads((ROOT / "config/entities-v2.json").read_text(encoding="utf-8"))
    entities, genes = {}, {}
    for entity in config["entities"]:
        for term in entity["terms"]:
            _insert(entities, term, {**entity, "term": term})
    # IDs here are fixtures for normalization mechanics, not an HGNC snapshot.
    for value in [
        {"id": "HGNC:12679", "symbol": "VDR", "name": "vitamin D receptor", "aliases": []},
        {"id": "HGNC:2707", "symbol": "DNMT1", "name": "DNA methyltransferase 1", "aliases": []},
        {"id": "HGNC:135", "symbol": "ACE", "name": "angiotensin I converting enzyme", "aliases": []},
    ]:
        for term, kind in [(value["symbol"], "symbol"), (value["name"], "name")]:
            _insert(genes, term, {**value, "term": term, "form_kind": kind})
    return Dictionaries(
        "synthetic-dictionaries-v2", {"source": "synthetic engineering fixture"}, genes, entities
    )


ACTIVE = [
    ("Caffeine", 1, "nsubj", "caffeine", "NOUN"),
    ("reduced", 1, "ROOT", "reduce", "VERB"),
    ("blood", 3, "compound", "blood", "NOUN"),
    ("pressure", 1, "dobj", "pressure", "NOUN"),
    ("in", 1, "prep", "in", "ADP"),
    ("adults", 4, "pobj", "adult", "NOUN"),
    (".", 1, "punct", ".", "PUNCT"),
]


def run(text, dictionary, specs, label="Results", **metadata):
    return extract(
        text,
        [{"label": label, "start": 0, "end": len(text)}],
        dictionary,
        {"_parser": DependencyFixture(specs), **metadata},
    )


def test_active_exact_spans_and_fixture_provenance(dictionaries_v2):
    text = "Caffeine reduced blood pressure in adults."
    output = run(text, dictionaries_v2, ACTIVE)
    assert output["engine_version"] == "heuristic-v2"
    assert output["manifest"]["parser"]["mode"] == "fixture"
    assertion = output["assertions"][0]
    assert (assertion["subject"], assertion["predicate"], assertion["object"]) == (
        "CHEBI:27732",
        "decreases",
        "local:blood-pressure",
    )
    event = assertion["payload"]["event"]
    assert event["species"] == "human" and event["study_model"] == "clinical"
    assert assertion["quote"] == text[assertion["start"] : assertion["end"]]
    for role in [event["controller"], event["theme"], event["trigger"]]:
        assert role["text"] == text[role["start"] : role["end"]]


def test_passive_agent_roles(dictionaries_v2):
    text = "Blood pressure was reduced by caffeine in adults."
    specs = [
        ("Blood", 1, "compound", "blood", "NOUN"),
        ("pressure", 3, "nsubjpass", "pressure", "NOUN"),
        ("was", 3, "auxpass", "be", "AUX"),
        ("reduced", 3, "ROOT", "reduce", "VERB"),
        ("by", 3, "agent", "by", "ADP"),
        ("caffeine", 4, "pobj", "caffeine", "NOUN"),
        ("in", 3, "prep", "in", "ADP"),
        ("adults", 6, "pobj", "adult", "NOUN"),
        (".", 3, "punct", ".", "PUNCT"),
    ]
    event = run(text, dictionaries_v2, specs)["events"][0]
    assert event["subject"] == "CHEBI:27732" and event["object"] == "local:blood-pressure"


def test_passive_without_agent_abstains_not_nearest(dictionaries_v2):
    text = "Blood pressure was reduced after caffeine intake."
    specs = [
        ("Blood", 1, "compound", "blood", "NOUN"),
        ("pressure", 3, "nsubjpass", "pressure", "NOUN"),
        ("was", 3, "auxpass", "be", "AUX"),
        ("reduced", 3, "ROOT", "reduce", "VERB"),
        ("after", 3, "prep", "after", "ADP"),
        ("caffeine", 6, "compound", "caffeine", "NOUN"),
        ("intake", 4, "pobj", "intake", "NOUN"),
        (".", 3, "punct", ".", "PUNCT"),
    ]
    output = run(text, dictionaries_v2, specs)
    assert not output["assertions"]
    assert output["abstentions"][0]["abstention_reasons"] == ["missing_explicit_dependency_role"]


@pytest.mark.parametrize(
    "modifier,significance,negation,speculation",
    [
        ("did not", "unknown", True, False),
        ("did not significantly", "not_significant", False, False),
        ("may", "unknown", False, True),
    ],
)
def test_negation_significance_and_speculation_separate(
    dictionaries_v2, modifier, significance, negation, speculation
):
    text = f"Caffeine {modifier} reduce blood pressure."
    words = modifier.split()
    trigger = len(words) + 1
    specs = [("Caffeine", trigger, "nsubj", "caffeine", "NOUN")]
    for word in words:
        specs.append(
            (
                word,
                trigger,
                "neg" if word == "not" else "advmod" if word == "significantly" else "aux",
                word,
                "ADV" if word in {"not", "significantly"} else "AUX",
            )
        )
    specs += [
        ("reduce", trigger, "ROOT", "reduce", "VERB"),
        ("blood", trigger + 2, "compound", "blood", "NOUN"),
        ("pressure", trigger, "dobj", "pressure", "NOUN"),
        (".", trigger, "punct", ".", "PUNCT"),
    ]
    event = run(text, dictionaries_v2, specs)["events"][0]
    assert event["significance"] == significance
    assert event["negation"] is negation
    assert event["speculation"] is speculation
    assert event["direction"] == "decrease"


def test_no_effect_does_not_mean_negated_direction(dictionaries_v2):
    text = "Caffeine had no effect on blood pressure."
    specs = [
        ("Caffeine", 1, "nsubj", "caffeine", "NOUN"),
        ("had", 1, "ROOT", "have", "VERB"),
        ("no", 3, "det", "no", "DET"),
        ("effect", 1, "dobj", "effect", "NOUN"),
        ("on", 3, "prep", "on", "ADP"),
        ("blood", 6, "compound", "blood", "NOUN"),
        ("pressure", 4, "pobj", "pressure", "NOUN"),
        (".", 1, "punct", ".", "PUNCT"),
    ]
    event = run(text, dictionaries_v2, specs)["events"][0]
    assert event["direction"] == "no_effect" and event["polarity"] == "null"
    assert event["significance"] == "unknown" and event["negation"] is False


def test_no_significant_effect_does_not_assert_absence(dictionaries_v2):
    text = "Caffeine had no significant effect on blood pressure."
    specs = [
        ("Caffeine", 1, "nsubj", "caffeine", "NOUN"),
        ("had", 1, "ROOT", "have", "AUX"),
        ("no", 4, "neg", "no", "DET"),
        ("significant", 4, "amod", "significant", "ADJ"),
        ("effect", 1, "dobj", "effect", "NOUN"),
        ("on", 4, "prep", "on", "ADP"),
        ("blood", 7, "compound", "blood", "NOUN"),
        ("pressure", 5, "pobj", "pressure", "NOUN"),
        (".", 1, "punct", ".", "PUNCT"),
    ]
    event = run(text, dictionaries_v2, specs)["events"][0]
    assert event["direction"] == "change" and event["significance"] == "not_significant"
    assert event["polarity"] == "positive" and event["negation"] is False


def test_p_value_not_significance_rule(dictionaries_v2):
    text = "Caffeine reduced blood pressure (p=0.08)."
    specs = ACTIVE[:4] + [
        ("(", 1, "punct", "(", "PUNCT"),
        ("p", 1, "dep", "p", "NOUN"),
        ("=", 5, "punct", "=", "PUNCT"),
        ("0.08", 5, "nummod", "0.08", "NUM"),
        (")", 1, "punct", ")", "PUNCT"),
        (".", 1, "punct", ".", "PUNCT"),
    ]
    event = run(text, dictionaries_v2, specs)["events"][0]
    assert event["significance"] == "unknown"
    assert event["conditions"]["statistics"][0]["text"] == "p=0.08"


def test_human_cells_not_clinical_and_animal_not_human_hgnc(dictionaries_v2):
    text = "Vitamin D3 increased VDR gene expression in human cells."
    specs = [
        ("Vitamin", 1, "compound", "vitamin", "NOUN"),
        ("D3", 2, "nsubj", "d3", "NOUN"),
        ("increased", 2, "ROOT", "increase", "VERB"),
        ("VDR", 5, "compound", "vdr", "NOUN"),
        ("gene", 5, "compound", "gene", "NOUN"),
        ("expression", 2, "dobj", "expression", "NOUN"),
        ("in", 2, "prep", "in", "ADP"),
        ("human", 8, "amod", "human", "ADJ"),
        ("cells", 6, "pobj", "cell", "NOUN"),
        (".", 2, "punct", ".", "PUNCT"),
    ]
    output = run(text, dictionaries_v2, specs)
    event = output["events"][0]
    assert event["species"] == "human" and event["study_model"] == "cell"
    assert event["object"] == "HGNC:12679" and event["outcome_type"] == "expression"
    animal = text.replace("human cells", "mouse cells")
    animal_specs = [
        ("mouse" if word == "human" else word, head, dep, "mouse" if word == "human" else lemma, pos)
        for word, head, dep, lemma, pos in specs
    ]
    result = run(animal, dictionaries_v2, animal_specs)
    gene = next(m for m in result["mentions"] if m["kind"] == "gene")
    assert gene["normalized_id"] is None
    assert result["events"][0]["study_model"] == "mixed" or result["events"][0]["study_model"] == "cell"


def test_chemical_substances_are_distinct(dictionaries_v2):
    text = (
        "Vitamin D, vitamin D2, vitamin D3, calcitriol, calcifediol, catechin and epicatechin were measured."
    )
    specs = [(text, 0, "ROOT", text, "X")]
    result = run(text, dictionaries_v2, specs)
    ids = {m["text"].casefold(): m["normalized_id"] for m in result["mentions"]}
    assert len(set(ids.values())) == 7
    assert ids["calcitriol"] == "CHEBI:17823"
    assert ids["calcifediol"] == "CHEBI:17933"
    assert ids["epicatechin"] == "local:epicatechin-unspecified"


def test_abbreviation_collision_retains_candidate_unresolved(dictionaries_v2):
    text = "Adverse cardiac events (ACE) were recorded in adults. Caffeine increased ACE."
    spans = sentence_spans(text)
    specs = {s["text"]: [(s["text"], 0, "ROOT", s["text"], "X")] for s in spans}
    result = run(text, dictionaries_v2, specs)
    assert abbreviations(text)["ACE"][0]["long"] == "Adverse cardiac events"
    genes = [m for m in result["mentions"] if m["kind"] == "gene"]
    assert genes and all(m["payload"]["abbreviation_conflict"] and m["normalized_id"] is None for m in genes)


def test_parser_unavailable_is_blocked_not_false_success(dictionaries_v2):
    class MissingParser:
        manifest = {"available": False}

        def parse(self, sentence):
            raise ParserUnavailable("test missing model")

    result = extract("Caffeine reduced blood pressure.", [], dictionaries_v2, {"_parser": MissingParser()})
    assert result["status"] == "blocked" and not result["assertions"]
    assert result["abstentions"][0]["abstention_reasons"] == ["parser_unavailable"]


def test_oracle_uses_native_ids_and_same_roles(dictionaries_v2):
    text = "Caffeine reduced blood pressure in adults."
    entities = [
        {"kind": "nutrient", "text": "Caffeine", "start": 0, "end": 8, "normalized_id": "MESH:D002110"},
        {
            "kind": "gene",
            "text": "blood pressure",
            "start": 17,
            "end": 31,
            "normalized_id": "ENTREZ:synthetic",
        },
    ]
    result = run(text, dictionaries_v2, ACTIVE, _gold_entities=entities)
    assert result["manifest"]["oracle"] is True
    assert result["events"][0]["subject"] == "MESH:D002110"
    assert result["events"][0]["object"] == "ENTREZ:synthetic"


@pytest.mark.parametrize(
    "label,metadata", [("Methods", {}), ("Introduction", {}), ("Results", {"publication_types": ["Review"]})]
)
def test_background_not_reviewable_assertion(dictionaries_v2, label, metadata):
    output = run(
        "Caffeine reduced blood pressure in adults.", dictionaries_v2, ACTIVE, label=label, **metadata
    )
    assert not output["assertions"] and output["events"][0]["status"] == "background"


def test_summary_mmr_retains_null_and_exact_quotes():
    text = "Caffeine reduced blood pressure. Caffeine reduced blood pressure. Caffeine had no effect on gene expression. Small sample size limited the study."
    summary = summarize(sentence_spans(text, [{"label": "Results", "start": 0, "end": len(text)}]), [])
    assert summary["selection"]["lambda"] == 0.7
    assert summary["selection"]["null_result_reserved"]
    assert len(summary["results"]) == 2
    assert any("no effect" in value["text"] for value in summary["results"])
    assert all(s["text"] == text[s["start"] : s["end"]] for s in summary["results"])


def test_numeric_quantities_owner_and_dimension(dictionaries_v2):
    text = "Participants received caffeine at 100 mg/day for 8 weeks. Serum caffeine concentration was 2 mg/L. Cocoa contained caffeine at 20 mg/100 g."
    spans = sentence_spans(text)
    specs = {s["text"]: [(s["text"], 0, "ROOT", s["text"], "X")] for s in spans}
    output = run(text, dictionaries_v2, specs)
    quantities = [q for study in output["studies"] for q in study["quantities"]]
    assert [q["quantity_kind"] for q in quantities] == ["dose", "concentration", "food_content"]
    assert quantities[0]["owner_id"] == "CHEBI:27732"
    assert quantities[0]["normalized"]["denominator"] == "day"
    assert output["context"]["dose"]["text"] == "100 mg/day"
    assert all(q["text"] == text[q["start"] : q["end"]] for q in quantities)


def test_malformed_dependency_offsets_rejected():
    with pytest.raises(ParserUnavailable):
        validate_tokens(
            [{"i": 0, "text": "x", "start": 0, "end": 1, "head": 0}], {"text": "y", "start": 0, "end": 1}
        )


def test_rules_hash_all_new_sources_and_ontology():
    digest = hashlib.sha256()
    for name in _SOURCES:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes() + b"\0")
    assert RULES_VERSION == "rules-sha256:" + digest.hexdigest()


def test_passive_association_keeps_first_entity_controller(dictionaries_v2):
    text = "Caffeine was associated with blood pressure."
    specs = [
        ("Caffeine", 2, "nsubjpass", "caffeine", "NOUN"),
        ("was", 2, "auxpass", "be", "AUX"),
        ("associated", 2, "ROOT", "associate", "VERB"),
        ("with", 2, "prep", "with", "ADP"),
        ("blood", 5, "compound", "blood", "NOUN"),
        ("pressure", 3, "pobj", "pressure", "NOUN"),
        (".", 2, "punct", ".", "PUNCT"),
    ]
    output = run(text, dictionaries_v2, specs)
    assert output["assertions"][0]["predicate"] == "associated_with"
    assert output["events"][0]["subject"] == "CHEBI:27732"


def test_explicit_between_association_and_nonsignificance(dictionaries_v2):
    text = "No significant association was found between caffeine and blood pressure."
    specs = [
        ("No", 2, "det", "no", "DET"),
        ("significant", 2, "amod", "significant", "ADJ"),
        ("association", 4, "nsubjpass", "association", "NOUN"),
        ("was", 4, "auxpass", "be", "AUX"),
        ("found", 4, "ROOT", "find", "VERB"),
        ("between", 4, "prep", "between", "ADP"),
        ("caffeine", 5, "pobj", "caffeine", "NOUN"),
        ("and", 6, "cc", "and", "CCONJ"),
        ("blood", 9, "compound", "blood", "NOUN"),
        ("pressure", 6, "conj", "pressure", "NOUN"),
        (".", 4, "punct", ".", "PUNCT"),
    ]
    event = run(text, dictionaries_v2, specs)["events"][0]
    assert event["subject"] == "CHEBI:27732" and event["object"] == "local:blood-pressure"
    assert event["direction"] == "association" and event["significance"] == "not_significant"
    assert event["negation"] is False


def test_contrast_clause_modifiers_do_not_leak(dictionaries_v2):
    text = "Caffeine reduced systolic blood pressure but did not change diastolic blood pressure."
    specs = [
        ("Caffeine", 1, "nsubj", "caffeine", "NOUN"),
        ("reduced", 1, "ROOT", "reduce", "VERB"),
        ("systolic", 4, "amod", "systolic", "ADJ"),
        ("blood", 4, "compound", "blood", "NOUN"),
        ("pressure", 1, "dobj", "pressure", "NOUN"),
        ("but", 1, "cc", "but", "CCONJ"),
        ("did", 8, "aux", "do", "AUX"),
        ("not", 8, "neg", "not", "ADV"),
        ("change", 1, "conj", "change", "VERB"),
        ("diastolic", 11, "amod", "diastolic", "ADJ"),
        ("blood", 11, "compound", "blood", "NOUN"),
        ("pressure", 8, "dobj", "pressure", "NOUN"),
        (".", 1, "punct", ".", "PUNCT"),
    ]
    events = run(text, dictionaries_v2, specs)["events"]
    assert len(events) == 2
    assert events[0]["negation"] is False and events[1]["negation"] is True


def test_explicit_controller_coordination(dictionaries_v2):
    text = "Caffeine and calcitriol reduced blood pressure."
    specs = [
        ("Caffeine", 3, "nsubj", "caffeine", "NOUN"),
        ("and", 0, "cc", "and", "CCONJ"),
        ("calcitriol", 0, "conj", "calcitriol", "NOUN"),
        ("reduced", 3, "ROOT", "reduce", "VERB"),
        ("blood", 5, "compound", "blood", "NOUN"),
        ("pressure", 3, "dobj", "pressure", "NOUN"),
        (".", 3, "punct", ".", "PUNCT"),
    ]
    events = run(text, dictionaries_v2, specs)["events"]
    assert {e["subject"] for e in events} == {"CHEBI:27732", "CHEBI:17823"}


def test_nested_receptor_name_is_not_vitamin_intervention(dictionaries_v2):
    text = "Vitamin D receptor expression was measured in humans."
    result = run(text, dictionaries_v2, [(text, 0, "ROOT", text, "X")])
    assert not [m for m in result["mentions"] if m["kind"] == "nutrient"]
    assert len([m for m in result["mentions"] if m["kind"] == "gene"]) == 1


def test_grouped_repeated_event_preserves_all_quotes(dictionaries_v2):
    line = "Caffeine reduced blood pressure in adults."
    text = line + " " + line
    result = run(text, dictionaries_v2, {line: ACTIVE})
    assert len(result["events"]) == 2 and len(result["assertions"]) == 1
    assert result["duplicate_events_grouped"] == 1
    evidence = result["assertions"][0]["payload"]["event"]["evidence_quotes"]
    assert len(evidence) == 2
    assert all(q["quote"] == text[q["start"] : q["end"]] for q in evidence)


def test_explicit_arms_keep_quantity_and_model_context_local(dictionaries_v2):
    text = "Group A received caffeine at 100 mg/day; Group B received calcitriol at 2 μg/day."
    result = run(text, dictionaries_v2, [(text, 0, "ROOT", text, "X")])
    quantities = result["studies"][0]["quantities"]
    assert [q["arm_id"] for q in quantities] == ["arm:group-a", "arm:group-b"]
    assert [q["owner_id"] for q in quantities] == ["CHEBI:27732", "CHEBI:17823"]
    assert quantities[1]["normalized"]["value"] == 0.002
    assert result["context"]["dose"]["value"] == "미확인"


def test_later_concentration_does_not_reclassify_dose(dictionaries_v2):
    text = "Participants received caffeine at 100 mg/day; serum caffeine concentration was 2 mg/L."
    result = run(text, dictionaries_v2, [(text, 0, "ROOT", text, "X")])
    assert [q["quantity_kind"] for q in result["studies"][0]["quantities"]] == ["dose", "concentration"]


@pytest.mark.parametrize(
    "adverb,direction", [("positively", "positive"), ("negatively", "negative"), ("inversely", "negative")]
)
def test_association_sign_is_not_causal_direction(dictionaries_v2, adverb, direction):
    text = f"Caffeine was {adverb} associated with blood pressure."
    specs = [
        ("Caffeine", 3, "nsubjpass", "caffeine", "NOUN"),
        ("was", 3, "auxpass", "be", "AUX"),
        (adverb, 3, "advmod", adverb, "ADV"),
        ("associated", 3, "ROOT", "associate", "VERB"),
        ("with", 6, "case", "with", "ADP"),
        ("blood", 6, "compound", "blood", "NOUN"),
        ("pressure", 3, "nmod", "pressure", "NOUN"),
        (".", 3, "punct", ".", "PUNCT"),
    ]
    result = run(text, dictionaries_v2, specs)
    event = result["events"][0]
    assert event["direction"] == "association" and event["association_direction"] == direction
    assert result["assertions"][0]["predicate"] == "associated_with"


def test_development_ablation_skips_real_context_components(dictionaries_v2, monkeypatch):
    from nutri_literature import extraction_v2

    def forbidden(*args, **kwargs):
        raise AssertionError("context component must not execute in syntax ablation")

    monkeypatch.setattr(extraction_v2, "_modifiers", forbidden)
    monkeypatch.setattr(extraction_v2, "conditions_for", forbidden)
    result = run(
        "Caffeine reduced blood pressure in adults.",
        dictionaries_v2,
        ACTIVE,
        _ablation_stage="syntax_without_context",
    )
    assert result["manifest"]["ablation_stage"] == "syntax_without_context"
    assert result["rules_version"].endswith("|ablation:syntax_without_context")
    event = result["events"][0]
    assert event["significance"] == "unknown" and event["conditions"]["timepoint"] is None


def test_group_key_never_collapses_polarity_time_or_significance(dictionaries_v2):
    event = run("Caffeine reduced blood pressure in adults.", dictionaries_v2, ACTIVE)["events"][0]
    assert _group_key(event) != _group_key({**event, "negation": True})
    assert _group_key(event) != _group_key({**event, "significance": "not_significant"})
    assert _group_key(event) != _group_key(
        {**event, "conditions": {**event["conditions"], "timepoint": {"value": "after 8 weeks"}}}
    )
