"""Scientific failure/compatibility regressions; synthetic cases are not accuracy."""

import io
import json
from pathlib import Path

import pytest

from nutri_literature.candidates_v4 import GeneIndex
from nutri_literature.chemical_v6 import parse_records, load_snapshot, SCHEMA
from nutri_literature.clinical_v6 import clauses, observations, summary, state
from nutri_literature.corpora_v6 import audit_training
from nutri_literature.db import digest, dumps, initialize
from nutri_literature.embeddings_v6 import FrozenEncoder
from nutri_literature.features_v6 import source_hashes
from nutri_literature.identity_v6 import CandidateSearch, cluster_mentions, normalize, taxon_evidence
from nutri_literature.models_v6 import LinearHead, Models, FEATURE_SHA
from nutri_literature.registry_v6 import ENGINE_CHOICES, default_engine, switch_engine, validate_source_spans
from nutri_literature.settings import Settings
from nutri_literature.training_v6 import folds, threshold


def sentence(text, label="Results", start=0):
    return {"text": text, "start": start, "end": start + len(text), "label": label, "pico_spans": []}


def mention(text, surface, kind="gene", taxon=None):
    start = text.index(surface)
    return {
        "text": surface,
        "kind": kind,
        "start": start,
        "end": start + len(surface),
        "normalized_id": None,
        "payload": {"taxon_id": taxon},
        "status": "needs_review",
    }


@pytest.fixture
def search():
    rows = [
        {
            "id": "NCBIGene:1",
            "native_id": "1",
            "symbol": "ABC",
            "name": "alpha beta cytokine",
            "aliases": ["X", "ABC1"],
            "taxon_id": 9606,
            "gene_type": "protein-coding",
        },
        {
            "id": "NCBIGene:2",
            "native_id": "2",
            "symbol": "Abc",
            "name": "alpha beta cytokine",
            "aliases": ["ABC1"],
            "taxon_id": 10090,
            "gene_type": "protein-coding",
        },
        {
            "id": "NCBIGene:3",
            "native_id": "3",
            "symbol": "OTHER",
            "name": "other gene",
            "aliases": ["X"],
            "taxon_id": 9606,
            "gene_type": "protein-coding",
        },
    ]
    return CandidateSearch(GeneIndex({"records": rows, "manifest": {"records_sha256": digest(dumps(rows))}}))


def test_mesh_c_identity_is_not_its_parent_descriptor():
    xml = b'<SupplementalRecordSet><SupplementalRecord SCRClass="1"><SupplementalRecordUI>C123</SupplementalRecordUI><SupplementalRecordName><String>Fixture metabolite</String></SupplementalRecordName><HeadingMappedToList><HeadingMappedTo><DescriptorReferredTo><DescriptorUI>D456</DescriptorUI></DescriptorReferredTo></HeadingMappedTo></HeadingMappedToList></SupplementalRecord></SupplementalRecordSet>'
    row = list(parse_records(io.BytesIO(xml), True))[0]
    assert row["id"] == "MESH:C123" and row["broader_headings"] == ["D456"]
    assert row["broader_headings_are_identity"] is False and row["chemical_candidate"]


@pytest.mark.parametrize("klass", ["2", "3", "4", "5", "6"])
def test_nonchemical_scr_classes_remain_records_but_not_chemical_candidates(klass):
    xml = f'<SupplementalRecordSet><SupplementalRecord SCRClass="{klass}"><SupplementalRecordUI>C123</SupplementalRecordUI></SupplementalRecord></SupplementalRecordSet>'
    assert not list(parse_records(io.BytesIO(xml.encode()), True))[0]["chemical_candidate"]


def test_mesh_hash_and_alias_collision_preserved(tmp_path):
    rows = [
        {"id": "MESH:C1", "native_id": "C1", "terms": ["same compound"], "chemical_candidate": True},
        {"id": "MESH:D1", "native_id": "D1", "terms": ["same compound"], "chemical_candidate": True},
    ]
    path = tmp_path / "mesh.json"
    path.write_text(
        dumps(
            {"manifest": {"schema_version": SCHEMA, "records_sha256": digest(dumps(rows))}, "records": rows}
        )
    )
    value = load_snapshot(path)
    assert len(value["index"]["same compound"]) == 2
    with pytest.raises(ValueError, match="hash"):
        load_snapshot(path, "incorrect")


def test_retrieval_keeps_ambiguous_top_ten_and_fuzzy_candidates(search):
    assert {r["id"] for r in search.retrieve("X")} == {"NCBIGene:1", "NCBIGene:3"}
    assert search.retrieve("alpha beta cytokine", 9606)[0]["taxon_id"] == 9606
    assert search.retrieve("alpha beta cytokin")[0]["retrieval_exact"] is False


@pytest.mark.parametrize(
    "line,taxon,model",
    [
        ("ABC in human cells", 9606, "cell"),
        ("ABC in patients", 9606, "clinical"),
        ("ABC in mice", 10090, "animal"),
        ("ABC in rats", 10116, "animal"),
        ("ABC in mice and human cells", None, "cell"),
        ("ABC expression", None, "unknown"),
    ],
)
def test_taxon_never_defaults_to_human(line, taxon, model):
    row = sentence(line)
    assert taxon_evidence(mention(line, "ABC"), row, [row], line, {})[:2] == (taxon, model)


def test_adjacent_species_source_and_local_precedence():
    text = "In mice. ABC expression was reduced in human cells."
    rows = [sentence("In mice.", start=0), sentence(text[9:], start=9)]
    taxon, model, evidence = taxon_evidence(mention(text, "ABC"), rows[1], rows, text, {})
    assert taxon == 9606 and model == "cell" and evidence[0]["source"] == "sentence"


def test_relative_id_selection_and_collision_hold(search):
    text = "ABC was expressed in human cells."
    row = sentence(text)
    value, hypothesis = normalize(mention(text, "ABC"), row, [row], text, search, {}, [])
    assert value["normalized_id"] == "NCBIGene:1" and hypothesis["candidates"][0]["native_id"] == "1"
    text = "X was expressed in human cells."
    row = sentence(text)
    value, hypothesis = normalize(mention(text, "X"), row, [row], text, search, {}, [])
    assert value["normalized_id"] is None and hypothesis["reason"] == "alias_collision"


def test_train_inference_clusters_ignore_native_and_predicted_ids(search):
    text = "ABC and ABC1 in human cells"
    values = [mention(text, "ABC", taxon=9606), mention(text, "ABC1", taxon=9606)]
    result = cluster_mentions(values, [], search)
    assert len(result) == 1
    first = result[0]["cluster_id"]
    for i, m in enumerate(values):
        m["normalized_id"] = "wrong-id-" + str(i)
    assert cluster_mentions(values, [], search)[0]["cluster_id"] == first
    values[1]["payload"]["taxon_id"] = 10090
    assert len(cluster_mentions(values, [], search)) == 2


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We will assess blood pressure (p=0.01).", "planned"),
        ("We aimed to assess whether blood pressure was reduced.", "planned"),
        ("The significance level was p=0.05.", "unknown"),
        ("Blood pressure was lower: 120 versus 140 mmHg.", "observed"),
        ("Blood pressure was not significantly reduced.", "observed"),
        ("Blood pressure may decrease.", "interpretation"),
        ("Previous studies found reduced blood pressure.", "background"),
    ],
)
def test_measurement_gate_does_not_use_statistics_alone(text, expected):
    assert state(sentence(text), {})[0] == expected


def test_plan_clause_does_not_inherit_observation():
    row = sentence("We will assess blood pressure; however vitamin D reduced blood pressure.")
    assert [state(c, {})[0] for c in clauses(row)] == ["planned", "observed"]


def test_nutrition_observations_work_without_gene_and_preserve_null_meaning():
    text = "Vitamin D did not significantly reduce blood pressure (p=0.07)."
    row = sentence(text)
    values = [mention(text, "Vitamin D", "nutrient")]
    records, states = observations(text, [row], values, {}, lambda s: [])
    # "not significantly reduce" needs an explicit measured-result pattern.
    assert records and records[0]["gene_identity_required"] is False
    assert records[0]["significance"] == "not_significant"
    assert records[0]["null_interpretation"] == "non_significant"
    from nutri_literature.extraction_v6 import observation_assertion

    assertion = observation_assertion(records[0])
    assert assertion["predicate"] == "has_observation"
    assert assertion["payload"]["endpoint_order_is_causal"] is False
    assert assertion["payload"]["observation"]["null_interpretation"] == "non_significant"
    result = summary(states, records)
    assert result["results"] and not result["missing_results"]
    validate_source_spans({"clinical_observations": records}, text)


def test_secondary_own_synthesis_is_not_primary_or_prior():
    row = sentence("Our meta-analysis found that vitamin D reduced blood pressure.")
    assert state(row, {"publication_types": ["Meta-Analysis"]}) == ("observed", "secondary_synthesis")
    row = sentence("Earlier investigators observed lower blood pressure.")
    assert state(row, {"publication_types": ["Review"]})[0] == "background"


def test_multiple_conditions_are_not_automatically_collapsed():
    text = "Vitamin D reduced blood pressure at 4 weeks and 8 weeks."
    row = sentence(text)
    records, _ = observations(text, [row], [mention(text, "Vitamin D", "nutrient")], {}, lambda s: [])
    assert records[0]["conditions"]["timepoint"] is None
    assert (
        len(
            [
                q
                for q in records[0]["conditions"]["unassigned_quantities"]
                if q["quantity_role"] == "timepoint"
            ]
        )
        == 2
    )


def test_duplicate_event_citations_remain_inspectable():
    from nutri_literature.extraction_v6 import group_events

    first = {
        "event_id": "synthetic-identical-meaning-key",
        "evidence_quotes": [{"start": 0, "end": 3, "quote": "One"}],
    }
    second = {
        "event_id": "synthetic-identical-meaning-key",
        "evidence_quotes": [{"start": 4, "end": 7, "quote": "Two"}],
    }
    result = group_events([first, second])
    assert len(result) == 1 and len(next(iter(result.values()))["evidence_quotes"]) == 2
    assert first["evidence_quotes"] == [{"start": 0, "end": 3, "quote": "One"}]


def test_event_grouping_preserves_immutable_parser_pipeline_without_deepcopy():
    from copy import deepcopy
    from nutri_literature.extraction_v6 import group_events

    class FrozenPipeline(list):
        def append(self, value):
            raise NotImplementedError("immutable parser pipeline")

    pipeline = FrozenPipeline(["tagger", "parser"])
    event = {"event_id": "synthetic-parser-manifest", "parser": {"pipeline": pipeline},
             "evidence_quotes": [{"start": 0, "end": 3, "quote": "One"}]}
    with pytest.raises(NotImplementedError):
        deepcopy(event)
    copied = group_events([event])[event["event_id"]]
    assert type(copied["parser"]["pipeline"]) is list
    assert copied["parser"]["pipeline"] == ["tagger", "parser"]
    copied["parser"]["pipeline"].append("fixture")
    assert pipeline == ["tagger", "parser"]


def test_source_mismatch_blocks_observation():
    text = "Vitamin D reduced blood pressure."
    records, _ = observations(
        text, [sentence(text)], [mention(text, "Vitamin D", "nutrient")], {}, lambda s: []
    )
    records[0]["outcome"]["end"] += 1
    with pytest.raises(ValueError):
        validate_source_spans({"clinical_observations": records}, text)


def test_paper_folds_and_threshold_only_use_training_oof():
    assert folds([str(i) for i in range(100)]) == folds(list(reversed([str(i) for i in range(100)])))
    result = threshold([1, 1, 0, 0], [0.9, 0.85, 0.3, 0.1])
    assert result["target_met"] and result["recall"] == 1 and result["source"] == "paper-separated-train-OOF"


def test_three_source_keys_excluded_and_test_split_rejected(tmp_path):
    train = tmp_path / "train.json"
    train.write_text(
        dumps(
            {
                "split": "train",
                "documents": [
                    {"pmid": "1", "pmcid": "PMC1", "text": "A source"},
                    {"pmid": "2", "text": "Same source"},
                    {"pmid": "3", "text": "Unique source"},
                ],
            }
        )
    )
    protected = tmp_path / "inputs.json"
    protected.write_text(
        dumps(
            {
                "split": "test",
                "documents": [
                    {"pmid": "9", "pmcid": "PMC1", "text": "Different"},
                    {"pmid": "8", "text": "Same-source"},
                ],
            }
        )
    )
    result = audit_training(train, [protected], tmp_path / "out")
    assert result["excluded_pmids"] == ["1", "2"] and result["passed"]
    train.write_text(dumps({"split": "test", "documents": []}))
    with pytest.raises(ValueError):
        audit_training(train, [protected], tmp_path / "invalid")


def test_v5_rules_preserved_and_v6_default_requires_authentic_gates(tmp_path):
    from nutri_literature.extraction_v5 import source_manifest

    assert (
        source_manifest()[1]
        == "rules-sha256:479b02e4cba01bfd22d1a1939185984a42c5645ba716438ca1993256d3f866ff"
    )
    settings = Settings(tmp_path / "runtime", Path(__file__).parents[1] / "config/topics.json")
    initialize(settings)
    assert "heuristic-v6" in ENGINE_CHOICES and default_engine(settings) == "heuristic-v1"
    with pytest.raises(ValueError):
        switch_engine(settings, "heuristic-v6", {"passed": True, "seven_days": True})


def test_lightweight_head_has_no_backbone_training():
    head = LinearHead().fit([{"context": 0}, {"context": 1}, {"context": 0}, {"context": 1}], [0, 1, 0, 1])
    result = head.probabilities([{"context": 0}, {"context": 1}])
    assert result[1]["1"] > result[0]["1"]


def test_model_loader_rejects_missing_and_tampered_files(tmp_path):
    (tmp_path / "bad.joblib").write_bytes(b"not a model")
    (tmp_path / "manifest.json").write_text(
        dumps(
            {
                "feature_sha256": FEATURE_SHA,
                "training_split": "train",
                "test_used_for_selection": False,
                "files": {"bad.joblib": "incorrect"},
            }
        )
    )
    with pytest.raises(ValueError, match="hash"):
        Models(tmp_path).load()


def test_embedding_cache_keys_source_model_and_entities_and_checks_hash(tmp_path):
    import numpy as np

    encoder = FrozenEncoder(tmp_path / "model", tmp_path / "cache")
    encoder.model = object()
    encoder.manifest = {
        "available": True,
        "model_sha256": "fixture",
        "tokenizer_sha256": "fixture",
        "dimension": 2,
    }
    encoder.cache.mkdir()
    encoder._compute = lambda sentence, entities: np.array([[1, 2]] * (len(entities) + 1), dtype=np.float32)
    text = "α gene was measured."
    row = sentence(text)
    value = mention(text, "gene")
    first = encoder.encode(text, row, [value])
    assert np.array_equal(first, encoder.encode(text, row, [value]))
    receipt = next(encoder.cache.glob("*.json"))
    payload = json.loads(receipt.read_bytes())
    payload["sha256"] = "wrong"
    receipt.write_text(dumps(payload))
    with pytest.raises(ValueError, match="integrity"):
        encoder.encode(text, row, [value])


def test_long_unicode_window_pooling_covers_final_entity_without_truncation(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import re
    import sys
    import numpy as np
    from nutri_literature import embeddings_v6

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext))
    monkeypatch.setattr(embeddings_v6, "cpu_inference_lock", lambda *a: nullcontext())

    class Tensor:
        def __init__(self, value):
            self.value = value

        def __getitem__(self, index):
            return Tensor(self.value[index])

        def cpu(self):
            return self

        def numpy(self):
            return self.value

        @property
        def ndim(self):
            return self.value.ndim

        def unsqueeze(self, axis):
            return Tensor(np.expand_dims(self.value, axis))

    class Tokenizer:
        def __call__(self, text, **kwargs):
            parts = list(re.finditer(r"\S+", text))
            return {"input_ids": list(range(len(parts))), "offset_mapping": [m.span() for m in parts]}

        def prepare_for_model(self, values, **kwargs):
            return {"input_ids": Tensor(np.array([-1, *values, -2]))}

    encoder = FrozenEncoder(tmp_path / "model", tmp_path / "cache")
    encoder.tokenizer = Tokenizer()

    def model(input_ids):
        assert input_ids.ndim == 2, "The real BERT interface requires a batch dimension"
        return SimpleNamespace(
            last_hidden_state=Tensor(np.array([[[v, v * v] for v in input_ids.value[0]]], dtype=float))
        )

    encoder.model = model
    encoder.manifest = {"dimension": 2}
    text = " ".join("α" + str(i) for i in range(1000))
    last = mention(text, "α999")
    vectors = encoder._compute(sentence(text), [last])
    assert np.allclose(vectors[0], [np.mean(np.arange(1000)), np.mean(np.arange(1000) ** 2)])
    assert np.allclose(vectors[1], [999, 999**2])


def test_undirected_native_projection_never_invents_subject():
    from nutri_literature.evaluation_v6 import project_hypotheses

    h = {
        "type_selected": True,
        "chemical": {"kind": "component", "normalized_id": "MESH:C1"},
        "gene": {"kind": "gene", "normalized_id": "NCBIGene:1"},
        "benchmark_relation_type": "Association",
        "role_direction": "undirected",
        "semantic_verified": False,
        "evidence": {},
    }
    rows, _ = project_hypotheses(
        {"relation_hypotheses": [h]}, {("component", "MESH:C1"): {"C1"}, ("gene", "NCBIGene:1"): {"1"}}
    )
    assert "subject" not in rows[0]


def test_review_rejects_unanchored_observation_corrections():
    from nutri_literature.review_v6 import correction_reasons

    text = "Vitamin D reduced blood pressure."
    payload = {"observation": {"outcome": mention(text, "blood pressure", "outcome"), "conditions": {}}}
    correction = {"observation": {"outcome": {"text": "wrong", "start": 0, "end": 5}}}
    assert "unanchored_observation_outcome" in correction_reasons(correction, payload, text)
    assert correction_reasons({"role_direction": "undirected", "predicate": "increases"}, {}, text) == [
        "undirected_predicate_conflict"
    ]


def test_raw_gene_candidates_survive_low_span_priority(search, monkeypatch):
    from types import SimpleNamespace
    from nutri_literature import candidates_v6

    text = "ABC was expressed in human cells."
    raw = mention(text, "ABC")
    monkeypatch.setattr(candidates_v6, "collect_candidates", lambda *a, **k: ([raw], {}))
    learned = SimpleNamespace(score=lambda *a: [0.01], manifest={"span_threshold": 0.9})
    metadata = {
        "_candidate_provider": object(),
        "_gene_index": search.gene_index,
        "_id_search": search,
        "_learned_models": learned,
    }
    values, _, hypotheses, clusters = candidates_v6.collect(text, [sentence(text)], None, metadata)
    assert len(values) == 1 and not values[0]["payload"]["automatic_selected"]
    assert values[0]["payload"]["raw_candidate_preserved"] and hypotheses and clusters


def test_union_provider_shares_chemical_and_frozen_backbone(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import sys
    from nutri_literature import provider_v6

    calls = []
    monkeypatch.setitem(
        sys.modules, "spacy", SimpleNamespace(load=lambda name, **k: calls.append(name) or object())
    )
    for name in ("en_ner_bc5cdr_md", "en_ner_bionlp13cg_md"):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(__file__=str(tmp_path / (name + ".py"))))
    monkeypatch.setattr(provider_v6, "directory_hash", lambda path: "fixture")
    model = SimpleNamespace(config=SimpleNamespace(id2label={0: "O", 1: "B-GENE", 2: "I-GENE"}))
    encoder = SimpleNamespace(
        load=lambda: None, ner_model=model, tokenizer=object(), manifest={"model_sha256": "fixture"}
    )
    provider = provider_v6.SharedCandidates(tmp_path, encoder)
    provider.load()
    provider.load()
    assert calls == ["en_ner_bc5cdr_md", "en_ner_bionlp13cg_md"]
    assert provider.providers[0].chemical is provider.providers[1].chemical
    assert provider.providers[0].gene is model


def test_directed_review_cannot_reverse_role_without_reversing_endpoint(search):
    from nutri_literature.review_v6 import correction_reasons

    text = "Vitamin D reduced ABC expression in human cells."
    chem = mention(text, "Vitamin D", "component")
    gene = mention(text, "ABC")
    payload = {
        "event": {
            "controller": chem,
            "theme": gene,
            "subject": "Vitamin D",
            "role_direction": "chemical_to_gene",
        }
    }
    assert "corrected_role_endpoint_conflict" in correction_reasons(
        {"subject": "Vitamin D", "role_direction": "gene_to_chemical"}, payload, text
    )
    assert not correction_reasons({"subject": "ABC", "role_direction": "gene_to_chemical"}, payload, text)


def test_upstream_clinical_overlap_requires_retraining(tmp_path):
    from nutri_literature.audit_v6 import upstream_clinical

    (tmp_path / "manifest.json").write_text(
        dumps({"training_split": "train", "training_pmids": {"rct": ["1"], "ebm": ["2"]}})
    )
    with pytest.raises(ValueError, match="retraining"):
        upstream_clinical(tmp_path, {"protected_pmids": ["2"]})
    assert upstream_clinical(tmp_path, {"protected_pmids": ["3"]})["protected_pmid_overlap"] == 0


def test_v6_training_persists_paper_oof_direction_and_relative_id_selection(tmp_path):
    from nutri_literature.training_v6 import train

    rows = []
    for i in range(10):
        path = tmp_path / (str(i) + ".json")
        bags = [
            {"features": {"effect": 1}, "labels": ["Association"], "direction": "undirected"},
            {"features": {"effect": 2}, "labels": ["Association"], "direction": "chemical_to_gene"},
            {"features": {"effect": 0}, "labels": [], "direction": None},
        ]
        ids = {
            "features": [
                {"specificity": 1, "retrieval_exact": True, "taxon_match": True},
                {"specificity": 0, "retrieval_exact": True, "taxon_match": True},
            ],
            "labels": [1, 0],
            "eligible": [True, True],
            "supported": True,
            "gold_available": True,
        }
        path.write_text(dumps({"pmid": str(i), "bags": bags, "identities": [ids]}))
        rows.append({"path": path.name, "sha256": digest(path.read_bytes())})
    audit = tmp_path / "audit.json"
    audit.write_text(
        dumps(
            {
                "passed": True,
                "protected_overlap": 0,
                "audited_training_sha256": "synthetic-input",
                "training_pmids": [str(i) for i in range(10)],
            }
        )
    )
    (tmp_path / "manifest.json").write_text(
        dumps(
            {
                "split": "train",
                "feature_sha256": FEATURE_SHA,
                "files": rows,
                "audit_sha256": digest(audit.read_bytes()),
                "training_sha256": "synthetic-input",
                "encoder": {"synthetic": True},
                "feature_source_hashes": source_hashes(),
            }
        )
    )
    result = train(tmp_path, tmp_path / "model", audit)
    model = Models(tmp_path / "model")
    model.load()
    assert result["identity_selection"]["source"] == "train-paper-OOF-only"
    assert set(result["fold_assignments"].values()) == set(range(5))
    assert set(model.heads["direction"].classes_) == {"undirected", "chemical_to_gene"}
    assert not result["test_used_for_selection"]


@pytest.mark.parametrize("predicate", ["affects", "has_observation"])
def test_v6_observation_api_reviews_and_report_use_stored_sources_only(tmp_path, monkeypatch, predicate):
    from fastapi.testclient import TestClient
    from nutri_literature import auth, operations_v6
    from nutri_literature.api import create_app
    from nutri_literature.db import connect, now, save_source
    from nutri_literature.extraction_v6 import source_manifest
    from nutri_literature.reporting import _engine_state

    monkeypatch.setattr(operations_v6, "components", lambda *a, **k: pytest.fail("API may not invoke NLP"))
    settings = Settings(tmp_path / "runtime", Path(__file__).parents[1] / "config/topics.json")
    text = "Vitamin D reduced blood pressure and heart rate."
    records, _ = observations(
        text, [sentence(text)], [mention(text, "Vitamin D", "nutrient")], {}, lambda s: []
    )
    observation = records[0]
    hashes, rules = source_manifest()
    encoder = {"available": True, "synthetic_fixture": True}
    manifest = {
        "artifact_identity": "fixture",
        "parser": {"mode": "production", "available": True, "model_sha256": "fixture-parser"},
        "oracle": False,
        "ablation": "context",
        "profile": "local",
        "rule_source_hashes": hashes,
        "gene_dictionary": {"records_sha256": "fixture-gene"},
        "chemical_dictionary": {"snapshot_sha256": "fixture-chemical"},
        "candidate_provider": {"available": True},
        "clinical_models": {"available": True},
        "learned_models": {"available": True, "feature_sha256": "fixture-feature"},
        "v6_models": {"available": True, "encoder": encoder},
        "frozen_encoder": encoder,
    }
    with TestClient(create_app(settings)) as client:
        path = settings.home / "engine-artifacts/heuristic-v6.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            dumps(
                {
                    **manifest,
                    "rules_version": rules,
                    "model_sha256": "fixture-parser",
                    "dictionary_version": "fixture-dictionary",
                }
            )
        )
        auth.create_account(settings, "synthetic_reviewer", "Synthetic password 123")
        with connect(settings) as con:
            topic = con.execute(
                "SELECT topic_id,query_version FROM topics WHERE enabled=1 LIMIT 1"
            ).fetchone()
            con.execute(
                "INSERT INTO papers(paper_id,pmid,title,pub_date,created_at,updated_at) VALUES(1,'synthetic-v6','Synthetic fixture','2026-01-01',?,?)",
                (now(), now()),
            )
            con.execute("INSERT INTO paper_topics VALUES(?,?,?)", (1, *topic))
            source = save_source(
                con,
                1,
                "pubmed",
                settings.raw / "fixture.xml",
                b"fixture",
                text,
                [],
                "https://example.org/synthetic",
            )
            con.execute("UPDATE papers SET current_source_id=? WHERE paper_id=1", (source,))
            result = {
                "engine_version": "heuristic-v6",
                "rules_version": rules,
                "dictionary_version": "fixture-dictionary",
                "manifest": manifest,
                "clinical_observations": [observation],
            }
            con.execute(
                "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "v6-fixture",
                    1,
                    source,
                    digest(text),
                    "heuristic-v6",
                    "fixture-dictionary",
                    dumps(result),
                    now(),
                    1,
                ),
            )
            con.execute(
                "INSERT INTO assertions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "a-v6",
                    "v6-fixture",
                    1,
                    "fingerprint",
                    "Vitamin D",
                    predicate,
                    "blood pressure",
                    text,
                    0,
                    len(text),
                    "v6-independent-observation",
                    "candidate",
                    dumps({"observation": observation, "explicit_relation": True}),
                ),
            )
        response = client.get("/api/v1/observations")
        assert response.status_code == 200 and response.json()["total"] == 1
        assert client.get("/api/v1/observations?limit=101").status_code == 422
        token = client.post(
            "/api/v1/auth/login",
            json={"username": "synthetic_reviewer", "password": "Synthetic password 123"},
        ).json()["access_token"]
        outcome = mention(text, "heart rate", "outcome")
        response = client.post(
            "/api/v1/reviews",
            headers={"Authorization": "Bearer " + token},
            json={
                "assertion_id": "a-v6",
                "decision": "corrected",
                "comment": "Synthetic source correction",
                "corrected_payload": {
                    "subject": "Vitamin D",
                    "predicate": predicate,
                    "object": "heart rate",
                    "observation": {"outcome": outcome},
                },
            },
        )
        assert response.status_code == 200, response.text
        stored = client.get("/api/v1/observations?status=approved").json()
        assert stored["total"] == 1 and stored["items"][0]["observation"]["outcome"]["text"] == "heart rate"
        with connect(settings) as con:
            paper = dict(con.execute("SELECT * FROM papers WHERE paper_id=1").fetchone())
            saved = _engine_state(con, paper, "heuristic-v6", settings)
            assert saved["assertions"][0]["display_status"] == "approved", (
                saved["extraction"]["stale_reasons"],
                saved["assertions"][0]["review_eligibility_reasons"],
            )
            assert saved["assertions"][0]["quote"] == text
            assert saved["assertions"][0]["effective_observation"]["outcome"]["text"] == "heart rate"
            con.execute("UPDATE papers SET retracted=1 WHERE paper_id=1")
        assert client.get("/api/v1/observations").json()["total"] == 0
        for endpoint in ("papers", "events", "evidence-map", "experiments", "experiments/diagnostics"):
            assert client.get("/api/v1/" + endpoint, params={"engine": "heuristic-v6"}).status_code == 200
        assert client.get("/healthz").json()["engine"] == "heuristic-v1"


def test_sealed_evaluation_rejects_forged_freeze_before_opening_test(tmp_path):
    from nutri_literature.evaluation_v6 import final_evaluate

    settings = Settings(tmp_path / "runtime", Path(__file__).parents[1] / "config/topics.json")
    initialize(settings)
    fake = tmp_path / "fake.json"
    fake.write_text(dumps({"passed": True, "id": "fake"}))
    with pytest.raises(ValueError, match="freeze"):
        final_evaluate(settings, tmp_path / "nonexistent-sealed-test.json", fake)


@pytest.mark.parametrize("valid,invalid", [("1", "0"), ("True", "False")])
def test_inference_development_keeps_test_annotations_unopened_and_inputs_label_free(tmp_path, valid, invalid):
    import csv
    import tarfile
    from nutri_literature.corpora_v6 import inference_development, inference_train

    texts = {"1": "Training result.", "2": "α observed result."}
    prompts = "PromptID,PMCID,Intervention,Comparator,Outcome\np1,1,treatment,placebo,result\np2,2,treatment,placebo,result\np3,3,sealed,sealed,sealed\n"
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(
        ["PromptID", "Valid Label", "Valid Reasoning", "Evidence Start", "Evidence End", "Label Code"]
    )
    writer.writerow(["p1", valid, valid, "0", len(texts["1"]), "0"])
    writer.writerow(["p1", valid, valid, "-1", "-1", "missing-evidence"])
    writer.writerow(["p1", invalid, valid, "-100", "99999999", "invalid-label"])
    writer.writerow(["p1", valid, invalid, "-100", "99999999", "invalid-reasoning"])
    writer.writerow(["p2", valid, valid, "2", len(texts["2"]), "1"])
    writer.writerow(["p3", "1", "1", "-100", "99999999", "sealed-invalid-offset"])
    members = {
        "splits/ev2_train_article_ids.txt": "1",
        "splits/ev2_validation_article_ids.txt": "2",
        "splits/ev2_test_article_ids.txt": "3",
        "prompts_merged.csv": prompts,
        "annotations_merged.csv": stream.getvalue(),
        **{"txt_files/PMC" + key + ".txt": value for key, value in texts.items()},
    }
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        for name, value in members.items():
            raw = value.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            target.addfile(info, io.BytesIO(raw))
    inference_train(archive, tmp_path / "train.json")
    spec = inference_development(archive, tmp_path / "development")
    inputs = json.loads(Path(spec["inputs_path"]).read_bytes())
    gold = json.loads(Path(spec["gold_path"]).read_bytes())
    assert [doc["pmid"] for doc in inputs["documents"]] == ["PMC2"]
    assert "comparisons" not in inputs["documents"][0]
    quote = gold["documents"][0]["comparisons"][0]["evidence"][0]
    assert quote["text"] == texts["2"][2:] and spec["test_labels_read"] is False
    assert json.loads((tmp_path / "train.json").read_bytes())["documents"][0]["comparisons"][0]["labels"] == [
        "0"
    ]


def test_inference_validity_rejects_unknown_encodings():
    from nutri_literature.corpora_v6 import _inference_flag

    assert _inference_flag(" TRUE ") is True
    assert _inference_flag("false") is False
    with pytest.raises(ValueError, match="validity flag"):
        _inference_flag("yes")


def test_ico_provided_and_automatic_evidence_have_separate_full_denominators():
    from nutri_literature.clinical_evaluation_v6 import evidence_counts

    text = "α treatment reduced blood pressure."
    quote = {"start": 0, "end": len(text), "text": text}
    prompt = {
        "prompt_id": "p1",
        "intervention_description": "Vitamin D",
        "comparator_description": "placebo",
        "outcome_description": "blood pressure",
        "evidence": [quote],
    }
    document = {"text": text, "comparisons": [prompt]}
    provided = [{"prompt_id": "p1", "evidence": [quote], "verified_evidence": [quote]}]
    wrong = {**prompt, "intervention_description": "caffeine"}
    scored = evidence_counts(document, [wrong], provided)
    assert scored["provided_ico_evidence_tokens"]["f1"] == 1
    assert scored["automatic_ico_evidence_tokens"]["tp"] == 0
    assert (
        scored["automatic_ico_evidence_tokens"]["fp"] > 0
        and scored["automatic_ico_evidence_tokens"]["fn"] > 0
    )
    assert scored["ico_alignment"]["matched_prompts"] == 0
    correct = {**prompt, "intervention_description": "VITAMIN D"}
    assert evidence_counts(document, [correct], provided)["automatic_ico_evidence_tokens"]["f1"] == 1
    with pytest.raises(ValueError, match="offset"):
        evidence_counts(document, [{**correct, "evidence": [{**quote, "text": "invented"}]}], provided)


def test_v6_separates_mapping_allowance_from_resident_budget():
    from nutri_literature.resources_v6 import virtual_limit, memory_budget, POLICY

    assert POLICY["resident_gib"] == 8 and POLICY["virtual_gib"] == 16
    assert virtual_limit(16, -1, -1) == 16 * 1024**3
    assert virtual_limit(16, 7 * 1024**3, -1) == 7 * 1024**3
    with pytest.raises(ValueError, match="8 GiB"):
        with memory_budget(9):
            pass


def test_clinical_development_rejects_test_before_file_or_model_loading(tmp_path):
    from nutri_literature.clinical_evaluation_v6 import development

    spec = tmp_path / "spec.json"
    spec.write_text(dumps({"corpus": "evidenceinference", "official_split": "test"}))
    settings = Settings(tmp_path, Path(__file__).parents[1] / "config/topics.json")
    with pytest.raises(ValueError, match="sealed"):
        development(settings, spec)


def test_normalized_final_inputs_reject_annotation_and_source_drift(tmp_path):
    from nutri_literature.holdout_v6 import source_inputs

    doc = {"pmid": "1", "text": "α Results.", "sections": [], "source_hash": digest("α Results.")}
    value = {"split": "test", "corpus": "nlmgene", "documents": [doc]}
    path = tmp_path / "inputs.json"
    path.write_text(dumps(value), encoding="utf-8")
    spec = {
        "inputs_path": str(path),
        "inputs_sha256": digest(path.read_bytes()),
        "corpus": "nlmgene",
        "papers": 1,
    }
    assert source_inputs(spec)["documents"][0] == doc
    value["documents"][0]["entities"] = []
    path.write_text(dumps(value), encoding="utf-8")
    spec["inputs_sha256"] = digest(path.read_bytes())
    with pytest.raises(ValueError, match="labels"):
        source_inputs(spec)


def test_normalized_freeze_requires_cohort_protection_before_opening_gold(tmp_path):
    from nutri_literature.holdout_v6 import bind_specs, CORPORA
    from nutri_literature.training_v4 import text_key

    sources = []
    for corpus in sorted(CORPORA):
        source = tmp_path / (corpus + "-inputs.json")
        text = "Protected test " + corpus
        source.write_text(
            dumps(
                {
                    "split": "test",
                    "corpus": corpus,
                    "documents": [
                        {"pmid": corpus, "text": text, "source_hash": digest(text), "sections": []}
                    ],
                }
            )
        )
        sources.append(
            {
                "corpus": corpus,
                "papers": 1,
                "official_split": "test",
                "inputs_path": str(source),
                "inputs_sha256": digest(source.read_bytes()),
                "gold_path": str(tmp_path / "unopened.json"),
                "gold_sha256": "not-read",
            }
        )
    audit = {
        "passed": True,
        "protected_overlap": 0,
        "training_pmids": ["training-only"],
        "protected_text_hashes": [text_key("other")],
    }
    manifest = tmp_path / "clinical-manifest.json"
    manifest.write_text(dumps({"training_pmids": {"rct": ["training-only"]}}))
    plan = tmp_path / "holdout-audit.json"
    plan.write_text(dumps({"completed": True, "specs": sources, "clinical_manifest_path": str(manifest)}))
    artifact = {
        "v6_models": {"audit_sha256": digest(dumps(audit)), "clinical_heads": {}},
        "clinical_models": {"manifest_sha256": digest(manifest.read_bytes())},
    }
    with pytest.raises(ValueError, match="protected before"):
        bind_specs(plan, audit, artifact)


def test_chemical_final_unresolved_ids_are_reported_in_strict_denominator():
    from nutri_literature.holdout_v6 import entity_counts

    gold = {"entities": [{"kind": "component", "start": 0, "end": 5, "native_ids": ["C1"]}]}
    predicted = {
        "entities": [{"kind": "component", "start": 0, "end": 5, "normalized_id": None, "payload": {}}]
    }
    metric = entity_counts(gold, predicted, {}, "nlmchem")
    assert metric["component_detection"]["tp"] == 1
    assert metric["chemical_strict_ids"]["fn"] == 1 and metric["chemical_strict_ids"]["fp"] == 1


def test_legacy_development_rescore_uses_v6_undirected_metric_without_rewriting(tmp_path):
    from nutri_literature.evaluation_v6 import rescore

    source = "Chemical and gene were associated."
    document = {
        "pmid": "1",
        "text": source,
        "entities": [
            {"kind": "component", "start": 0, "end": 8, "native_ids": ["C1"]},
            {"kind": "gene", "start": 13, "end": 17, "native_ids": ["1"]},
        ],
        "relations": [{"entity1": "C1", "entity2": "1", "predicate": "Association", "subject": None}],
    }
    gold = tmp_path / "gold.json"
    gold.write_text(dumps({"documents": [document]}))
    (tmp_path / "inputs.json").write_text(dumps({"split": "development", "corpus": "bioredirect"}))
    predictions = tmp_path / "predictions"
    predictions.mkdir()
    predicted = {
        "pmid": "1",
        "source_hash": digest(source),
        "entities": [],
        "relations": [
            {"entity1": "C1", "entity2": "1", "predicate": "Association", "role_direction": "undirected"}
        ],
        "verified_relations": [],
    }
    path = predictions / "paper-1.json"
    path.write_text(dumps(predicted))
    before = digest(path.read_bytes())
    report = rescore(predictions, gold, tmp_path / "rescored.json")
    assert report["metrics"]["relation_direction_all"]["tp"] == 1
    assert digest(path.read_bytes()) == before and report["engine_predictions"] == "unchanged"


def test_clinical_train_audit_inherits_reserved_papers_and_texts(tmp_path):
    from nutri_literature.training_v4 import text_key

    reserved = tmp_path / "protection.json"
    reserved.write_text(
        dumps(
            {
                "passed": True,
                "protected_overlap": 0,
                "protected_pmids": ["1"],
                "protected_text_hashes": [text_key("reserved source")],
                "protected_pmcids": [],
                "protected_sources": [],
            }
        )
    )
    train = tmp_path / "train.json"
    train.write_text(
        dumps(
            {
                "split": "train",
                "documents": [
                    {"pmid": "1", "text": "different abstract"},
                    {"pmid": "2", "text": "reserved source"},
                    {"pmid": "3", "text": "training source"},
                ],
            }
        )
    )
    record = audit_training(train, [], tmp_path / "audited", prior_audit=reserved)
    assert record["training_pmids"] == ["3"]
    assert record["excluded_pmids"] == ["1", "2"] and record["inherited_protection_sha256"] == digest(
        reserved.read_bytes()
    )


def test_trained_v6_rejects_changed_feature_implementation_before_unpickling(tmp_path):
    import joblib

    path = tmp_path / "span.joblib"
    joblib.dump(LinearHead().fit([{"a": 0}, {"a": 1}], [0, 1]), path)
    (tmp_path / "manifest.json").write_text(
        dumps(
            {
                "feature_sha256": FEATURE_SHA,
                "feature_source_hashes": {"stale.py": "old"},
                "training_split": "train",
                "test_used_for_selection": False,
                "files": {path.name: digest(path.read_bytes())},
            }
        )
    )
    with pytest.raises(ValueError, match="implementation changed"):
        Models(tmp_path).load()


def test_compact_gene_postings_preserve_alias_collisions_and_jaccard(search):
    from array import array
    from nutri_literature.identity_v6 import grams

    assert all(isinstance(values, array) and values.typecode == "I" for values in search.postings.values())
    exact = search.retrieve("ABC1", 10090)
    assert exact[0]["id"] == "NCBIGene:2" and exact[1]["id"] == "NCBIGene:1"
    for result in search.retrieve("alpha beta cytokin", 9606):
        record = next(row for row in search.gene_index.records if row["id"] == result["id"])
        query = grams("alpha beta cytokin")
        expected = set().union(
            *(grams(term) for term in [record["symbol"], record["name"], *record["aliases"]])
        )
        assert result["retrieval_similarity"] == pytest.approx(len(query & expected) / len(query | expected))


def test_development_goal_uses_frozen_predictions_not_a_gold_selected_configuration():
    from nutri_literature.evaluation_v6 import frozen_relation_report

    report = {"metrics": {"relation": {"precision": 0.04, "recall": 0.07}},
              "development_relation_selection": {"precision": 1.0, "recall": 1.0},
              "relation_configuration_comparisons": ["synthetic-exploratory-perfect-fit"]}
    frozen_relation_report(report)
    assert report["development_relation_goal_met"] is False
    assert report["development_relation_selection"] is None
    assert report["relation_selection_source"] == "train-paper-OOF-only"
