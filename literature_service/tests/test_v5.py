import json
from pathlib import Path

import pytest

from nutri_literature.db import digest, dumps, initialize
from nutri_literature.features_v5 import FEATURE_SHA, pair_candidates, relation_features
from nutri_literature.relations_v5 import BagClassifier, ConstantBinary
from nutri_literature.training_v5 import choose_threshold, folds
from nutri_literature.evaluation_v5 import failures, project_hypotheses, relation_sets, strict_gene_counts
from nutri_literature.registry_v5 import ENGINE_CHOICES, default_engine, switch_engine
from nutri_literature.settings import Settings


def mention(kind, start, end, text, identifier=None):
    return {
        "kind": kind,
        "start": start,
        "end": end,
        "text": text,
        "normalized_id": identifier,
        "payload": {},
        "status": "candidate",
    }


def test_unknown_ids_still_generate_candidates():
    sentence = {"text": "C activates G.", "start": 0, "end": 14, "label": "Abstract"}
    entities = [mention("component", 0, 1, "C"), mention("gene", 12, 13, "G")]
    pairs = pair_candidates([sentence], entities, lambda s: [])
    assert len(pairs) == 1
    assert pairs[0]["gene"]["normalized_id"] is None


def test_candidate_recall_counts_id_unresolved_pairs_but_not_cross_sentence_omissions():
    from nutri_literature.evaluation_v5 import candidate_coverage

    c, g = mention("component", 0, 1, "C"), mention("gene", 2, 3, "G")
    c["native_ids"] = ["C"]
    g["native_ids"] = ["G"]
    doc = {
        "pmid": "1",
        "entities": [c, g],
        "relations": [{"entity1": "C", "entity2": "G", "predicate": "Bind"}],
    }
    assert candidate_coverage(doc, {"candidate_pairs": [[c, g]]})["recall"] == 1
    assert candidate_coverage(doc, {"candidate_pairs": []})["fn"] == 1


def test_ids_do_not_leak_into_features():
    s = {"text": "C activates G.", "start": 0, "end": 14}
    c, g = mention("component", 0, 1, "C", "MeSH:C1"), mention("gene", 12, 13, "G", "NCBIGene:1")
    before = relation_features(s, c, g, [])
    c["normalized_id"] = "MeSH:C999"
    g["normalized_id"] = "NCBIGene:999"
    assert before == relation_features(s, c, g, [])
    assert not any("NCBIGene" in k or "MeSH" in k for k in before)


def test_target_names_masked_before_context_features():
    s = {"text": "ABC activates XYZ.", "start": 0, "end": 18}
    f = relation_features(s, mention("component", 0, 3, "ABC"), mention("gene", 14, 17, "XYZ"), [])
    assert not any("abc" in k or "xyz" in k for k in f)


def test_mil_does_not_copy_document_positive_to_all_sentences():
    bags = [[{"signal": 1}, {"irrelevant": 1}], [{"irrelevant": 1}], [{"negative": 1}]]
    model = BagClassifier("mil").fit(bags, [{"Association"}, set(), set()], ["Association"])
    assert all(len(indices) == 1 for indices in model.selection_history["Association"])
    assert model.predict(bags)[0]["Association"]["witness"] in (0, 1)


def test_unannotated_types_not_replaced_with_effect_direction():
    c, g = mention("component", 0, 1, "C", "C1"), mention("gene", 2, 3, "G", "G1")
    result = {
        "relation_hypotheses": [
            {
                "chemical": c,
                "gene": g,
                "type_selected": True,
                "benchmark_relation_type": "Association",
                "role_direction": "gene_to_chemical",
                "semantic_verified": True,
                "evidence": {},
            }
        ]
    }
    predicted, _ = project_hypotheses(result, {("component", "C1"): {"C1"}, ("gene", "G1"): {"G1"}})
    assert predicted[0]["predicate"] == "Association"
    assert predicted[0]["subject"] == "G1"


def test_unresolved_relations_count_as_outputs_not_silent_drops():
    result = {
        "relation_hypotheses": [
            {
                "chemical": mention("component", 0, 1, "C"),
                "gene": mention("gene", 2, 3, "G"),
                "type_selected": True,
                "benchmark_relation_type": "Bind",
                "role_direction": "unknown",
                "semantic_verified": False,
                "evidence": {},
            }
        ]
    }
    relations, verified = project_hypotheses(result, {})
    assert len(relations) == 1 and not verified
    assert relations[0]["entity2"].startswith("UNRESOLVED_GENE")


def test_oracle_preserves_multiple_gene_ids():
    e = mention("gene", 0, 1, "G")
    e["native_ids"] = ["1", "2"]
    doc = {"entities": [e]}
    prediction = {"entities": [e]}
    assert strict_gene_counts(doc, prediction, {}, True)["tp"] == 2
    assert strict_gene_counts(doc, prediction, {}, False)["fn"] == 2


def test_type_match_is_independent_of_direction_match():
    c, g = mention("component", 0, 1, "C"), mention("gene", 2, 3, "G")
    c["native_ids"] = ["C"]
    g["native_ids"] = ["G"]
    doc = {
        "pmid": "1",
        "entities": [c, g],
        "relations": [{"entity1": "C", "entity2": "G", "predicate": "Bind", "subject": "G"}],
    }
    prediction = {"relations": [{"entity1": "C", "entity2": "G", "predicate": "Bind", "subject": "C"}]}
    gold, observed, directed, roles = relation_sets(doc, prediction)
    assert gold == observed and directed != roles


def test_predicted_direction_is_scored_separately_from_semantic_verification():
    c, g = mention("component", 0, 1, "C", "C"), mention("gene", 2, 3, "G", "G")
    result = {
        "relation_hypotheses": [
            {
                "chemical": c,
                "gene": g,
                "type_selected": True,
                "benchmark_relation_type": "Bind",
                "role_direction": "gene_to_chemical",
                "semantic_verified": False,
                "evidence": {},
            }
        ]
    }
    observed, verified = project_hypotheses(result, {("component", "C"): {"C"}, ("gene", "G"): {"G"}})
    assert observed[0]["subject"] == "G" and not verified


def test_selection_ignores_old_rule_versions_and_small_smoke_cohorts():
    from nutri_literature.evaluation_v5 import selection
    from nutri_literature.registry_v5 import engine_rules_version

    report = {
        "corpus": "bioredirect",
        "mode": "end_to_end",
        "ablation": "context",
        "provider": "union",
        "at": "2026-10-09",
        "artifact": {"rules_version": "old"},
        "papers": 100,
        "elapsed_seconds": 1,
        "metrics": {"gene": {"precision": 1, "recall": 1}, "gene_strict_ids": {"f1": 1}},
    }
    assert not selection([report])["qualifying_gene_configuration"]
    report["artifact"]["rules_version"] = engine_rules_version("heuristic-v5")
    report["papers"] = 10
    assert not selection([report])["qualifying_gene_configuration"]


def test_diagnostic_matched_count_equals_type_true_positives():
    c, g = mention("component", 0, 1, "C", "C"), mention("gene", 2, 3, "G", "G")
    c["native_ids"] = ["C"]
    g["native_ids"] = ["G"]
    doc = {
        "pmid": "1",
        "entities": [c, g],
        "relations": [{"entity1": "C", "entity2": "G", "predicate": "Bind", "subject": "G"}],
    }
    pred = {
        "entities": [c, g],
        "candidate_pairs": [[c, g]],
        "relations": [{"entity1": "C", "entity2": "G", "predicate": "Bind", "subject": "C"}],
    }
    fn, role, fp, _ = failures(doc, pred, {("gene", "G"): {"G"}, ("component", "C"): {"C"}}, False)
    assert fn == {"matched_type_ids": 1}
    assert role == {"role_or_semantic_validation": 1} and not fp


def test_zero_predictions_cannot_meet_precision_target():
    selected = choose_threshold([1, 0], [0.01, 0.02])
    assert not selected["precision_target_met"]


def test_paper_folds_never_split_same_article():
    assignments = folds([str(i) for i in range(20)] + ["1"])
    assert len(assignments) == 20 and set(assignments.values()) == set(range(5))
    assert assignments == folds(list(reversed([str(i) for i in range(20)])))


def test_models_fail_closed_on_hash_mutation(tmp_path):
    import joblib
    from nutri_literature.models_v5 import LearnedModels

    joblib.dump(ConstantBinary(0.5), tmp_path / "span.joblib")
    (tmp_path / "manifest.json").write_text(
        dumps(
            {
                "training_split": "train",
                "feature_sha256": FEATURE_SHA,
                "files": {"span.joblib": digest((tmp_path / "span.joblib").read_bytes())},
            }
        )
    )
    (tmp_path / "span.joblib").write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifact changed"):
        LearnedModels(tmp_path).load()


def test_default_stays_v1_and_manual_v5_flag_cannot_promote(tmp_path):
    settings = Settings(tmp_path, Path(__file__).resolve().parents[1] / "config/topics.json")
    initialize(settings)
    assert "heuristic-v5" in ENGINE_CHOICES and default_engine(settings) == "heuristic-v1"
    with pytest.raises(ValueError, match="independent automatic evaluation"):
        switch_engine(settings, "heuristic-v5", {"passed": True})
    (tmp_path / "engine-default.json").write_text(
        json.dumps({"engine": "heuristic-v5", "promotion": {"passed": True}})
    )
    assert default_engine(settings) == "heuristic-v1"


def test_source_only_import_does_not_parse_heldout_relation_labels():
    from nutri_literature.corpora_v5 import source_only

    data = "1|t|Title\n1|a|Abstract\n1\tSENSITIVE_UNKNOWN_RELATION\tX\tY\n"
    docs = source_only(data)
    assert docs[0]["text"] == "Title Abstract" and "relations" not in docs[0]


def test_unverified_freeze_blocks_before_test_spec_access(tmp_path):
    from nutri_literature.evaluation_v5 import final_evaluate

    settings = Settings(tmp_path, Path(__file__).resolve().parents[1] / "config/topics.json")
    initialize(settings)
    freeze = tmp_path / "freeze.json"
    freeze.write_text('{"artifact":{}}')
    with pytest.raises((ValueError, KeyError)):
        final_evaluate(settings, tmp_path / "MUST_NOT_OPEN_TEST.json", freeze)


def test_v4_scientific_sources_are_unchanged():
    from nutri_literature.extraction_v4 import source_manifest

    assert (
        source_manifest()[1]
        == "rules-sha256:f694c28350a0d1bb4ab13d3c7bacf025e296d1ccac051c2b751bc392f5adf647"
    )


@pytest.mark.parametrize(
    "line,score,expected",
    [
        ("Caffeine reduced VDR expression in human cells.", 0.99, "chemical_to_gene"),
        ("Caffeine was reduced by VDR in human cells.", 0.01, "gene_to_chemical"),
        ("Caffeine reduced VDR expression in human cells.", 0.5, "chemical_to_gene"),
    ],
)
def test_v5_active_passive_roles_match_direction(line, score, expected):
    from nutri_literature.extraction_v5 import semantic_event
    from nutri_literature.nlp_v2 import parser_for

    cstart = line.lower().index("caffeine")
    gstart = line.index("VDR")
    c = mention("component", cstart, cstart + 8, line[cstart : cstart + 8], "CHEBI:27732")
    g = mention("gene", gstart, gstart + 3, "VDR", "NCBIGene:7421")
    g["payload"] = {"taxon_id": 9606, "study_model": "cell"}
    sentence = {"text": line, "start": 0, "end": len(line), "label": "Results", "rhetorical_role": "results"}
    pair = {"chemical": c, "gene": g, "sentence": sentence, "tokens": parser_for({}).parse(sentence)}
    event, reasons = semantic_event(pair, "Association", score, [], {"_ablation": "relations"}, [c, g])
    assert event is not None and not reasons
    assert event["role_direction"] == expected
    assert event["status"] == "candidate"


def test_v5_planned_event_never_becomes_observed():
    from nutri_literature.extraction_v5 import semantic_event
    from nutri_literature.nlp_v2 import parser_for

    line = "Caffeine will reduce VDR expression in human cells."
    cstart = 0
    gstart = line.index("VDR")
    c = mention("component", cstart, cstart + 8, "Caffeine", "CHEBI:27732")
    g = mention("gene", gstart, gstart + 3, "VDR", "NCBIGene:7421")
    sentence = {"text": line, "start": 0, "end": len(line), "label": "Results"}
    pair = {"chemical": c, "gene": g, "sentence": sentence, "tokens": parser_for({}).parse(sentence)}
    event, _ = semantic_event(pair, "Association", 0.99, [], {"_ablation": "relations"}, [c, g])
    assert event is not None and event["assertion_state"] == "planned" and event["status"] == "abstained"


def test_v5_api_is_stored_only(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from nutri_literature import operations_v5
    from nutri_literature.api import create_app

    settings = Settings(tmp_path, Path(__file__).resolve().parents[1] / "config/topics.json")
    monkeypatch.setattr(operations_v5, "components", lambda *a, **k: pytest.fail("API cannot run inference"))
    with TestClient(create_app(settings)) as client:
        for url in (
            "/api/v1/papers",
            "/api/v1/events",
            "/api/v1/evidence-map",
            "/api/v1/experiments",
            "/api/v1/experiments/diagnostics",
        ):
            assert client.get(url, params={"engine": "heuristic-v5"}).status_code == 200
        assert client.get("/healthz").json()["engine"] == "heuristic-v1"


def test_batch_identity_initializes_lazy_parser_before_registering(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nutri_literature.operations_v5 import identity
    from nutri_literature import nlp_v2

    class Parser:
        manifest = {"available": False, "model_sha256": None}

        def parse(self, sentence):
            assert sentence["text"] == "Model initialization."
            self.manifest = {"available": True, "model_sha256": "initialized-model"}

    parser = Parser()
    monkeypatch.setattr(nlp_v2, "parser_for", lambda metadata: parser)
    monkeypatch.setattr(nlp_v2, "parser_manifest", lambda value: dict(value.manifest))
    values = {
        name: SimpleNamespace(manifest=manifest)
        for name, manifest in {
            "_candidate_provider": {},
            "_clinical_models": {},
            "_learned_models": {},
            "_gene_index": {"records_sha256": "genes"},
        }.items()
    }
    settings = Settings(tmp_path, Path(__file__).resolve().parents[1] / "config/topics.json")
    first = identity(settings, values, SimpleNamespace(version="dictionary"))
    assert first["model_sha256"] == "initialized-model"
    assert first == identity(settings, values, SimpleNamespace(version="dictionary"))


def test_unique_official_name_and_explicit_taxon_survives_ranker_uncertainty():
    from nutri_literature.candidates_v5 import gene_hypotheses
    from nutri_literature.candidates_v4 import GeneIndex

    records = [
        {
            "id": "NCBIGene:7421",
            "native_id": "7421",
            "taxon_id": 9606,
            "symbol": "VDR",
            "name": "vitamin D receptor",
            "aliases": [],
            "gene_type": "protein-coding",
        }
    ]
    index = GeneIndex({"manifest": {"records_sha256": digest(dumps(records))}, "records": records})

    class Learner:
        manifest = {"identity_threshold": 0.85, "identity_margin": 0.15}

        def score(self, *args):
            return [0.2]

    text = "VDR expression in human cells."
    entity, hypothesis = gene_hypotheses(
        mention("gene", 0, 3, "VDR"), {"text": text}, text, index, {}, {}, Learner()
    )
    assert entity["normalized_id"] == "NCBIGene:7421"
    assert hypothesis["candidates"][0]["priority_score"] == 0.2
    # Rank scores cannot supply an absent species.
    entity, _ = gene_hypotheses(
        mention("gene", 0, 3, "VDR"), {"text": "VDR expression."}, "VDR expression.", index, {}, {}, Learner()
    )
    assert entity["normalized_id"] is None
