"""Fixture-only checks; no account, network call, or scientific performance claim."""

import json

import pytest

from nutri_literature.contract_v7 import canonical, sha
from nutri_literature.chatgpt_extraction import SCHEMA, align_exact, dictionary_index, project, source_input


TEXT = "Human aspirin binds TP53."


def extraction():
    return {"schema_version": SCHEMA, "entities": [
        {"key": "c", "kind": "chemical", "text": "aspirin", "start": 6, "end": 13,
         "native_id": "D001241", "taxon": None},
        {"key": "g", "kind": "gene", "text": "TP53", "start": 20, "end": 24,
         "native_id": "7157", "taxon": 9606}], "relations": [
        {"chemical": "c", "gene": "g", "benchmark_relation_type": "Bind", "role_direction": "undirected",
         "scientific_mechanism": "binding", "negated": False, "speculative": False,
         "observation": "observed", "evidence": {"start": 0, "end": len(TEXT), "quote": TEXT}}]}


def dictionary():
    return {"schema_version": "literature-chatgpt-id-dictionary-v1", "source_references": ["https://www.ncbi.nlm.nih.gov/gene/"],
            "records": [{"kind": "chemical", "native_id": "D001241", "names": ["aspirin"]},
                        {"kind": "gene", "native_id": "7157", "names": ["TP53"], "taxon": 9606}]}


def fixture(tmp_path, monkeypatch, reasoning_effort=None):
    from nutri_literature import chatgpt_evaluation as evaluation, benchmarks
    monkeypatch.setattr(evaluation, "PAPERS", 2)
    monkeypatch.setattr(evaluation, "GOLD_RELATIONS", 2)
    monkeypatch.setattr(evaluation, "BASELINE_COUNTS", (1, 1, 1))
    docs = [{"pmid": pmid, "text": TEXT, "source_hash": sha(TEXT)} for pmid in ("1", "2")]
    receipt = {"corpus": "bioredirect", "split": "development", "complete_partition": True,
               "official_split": True, "inputs_sha256": evaluation.INPUT_SHA256, "gold_sha256": evaluation.GOLD_SHA256}
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    for pmid in ("1", "2"):
        (baseline / ("paper-" + pmid + ".json")).write_text(canonical({"source_hash": sha(TEXT),
            "relations": [{"entity1": "D001241", "entity2": "7157" if pmid == "1" else "9", "predicate": "Bind"}]}), encoding="utf-8")
    dpath = tmp_path / "dictionary.json"
    dpath.write_text(canonical(dictionary()), encoding="utf-8")
    monkeypatch.setattr(benchmarks, "_load_inputs", lambda *args: ({"documents": docs}, receipt, evaluation.INPUT_SHA256))
    def gold(*args):
        assert len(list((tmp_path / "experiment" / "predictions").glob("paper-*.json"))) == 2
        return {"documents": [{"pmid": pmid, "entities": [
            {"kind": "component", "native_ids": ["D001241"]}, {"kind": "gene", "native_ids": ["7157"]}],
            "relations": [{"entity1": "D001241", "entity2": "7157", "predicate": "Bind"}]} for pmid in ("1", "2")]}, receipt, evaluation.GOLD_SHA256
    monkeypatch.setattr(benchmarks, "_load_gold", gold)
    out = tmp_path / "experiment"
    evaluation.prepare_experiment(tmp_path, baseline, out, "fixture-model", dpath, prepared, reasoning_effort=reasoning_effort)
    return evaluation, out


def infer(instructions, value):
    payload = json.loads(value)
    assert set(payload) == {"source", "offset_unit", "source_sha256"}
    assert payload["source"] == TEXT
    return {"text": canonical(extraction()), "model": "fixture-model", "response_id": "fixture-response",
            "status": "completed", "usage": {"input_tokens": 1}, "headers": {"Authorization": "SECRET"}}


def test_same_raw_response_standalone_and_hybrid_do_not_repair_ids():
    value = extraction()
    value["entities"][1]["native_id"] = "99999"
    result = project(TEXT, canonical(value), dictionary_index(dictionary()))
    assert result["chatgpt_only"]["relations"][0]["entity2"] == "99999"
    assert result["hybrid"]["relations"] == []
    assert "gene_identity_unverified" in result["hybrid"]["decisions"][0]["reasons"]


def test_source_offsets_unicode_and_plans_cannot_become_hybrid_observations():
    value = extraction()
    source = "🧬 " + TEXT
    aligned = project(source, canonical(value), dictionary_index(dictionary()))
    assert aligned["hybrid"]["relations"][0]["evidence"]["start"] == 2
    assert aligned["hybrid"]["relations"][0]["endpoints"]["gene"]["start"] == 22
    assert aligned["chatgpt_only"]["alignment_trace"] == aligned["hybrid"]["alignment_trace"]
    value["relations"][0]["observation"] = "planned"
    assert project(TEXT, canonical(value), dictionary_index(dictionary()))["hybrid"]["relations"] == []
    assert project(TEXT, canonical(extraction()), dictionary_index(dictionary()))["hybrid"]["relations"]


def test_no_species_default_and_unsupported_labels_remain_standalone_false_positives():
    value = extraction()
    value["relations"][0]["benchmark_relation_type"] = "unsupported"
    projected = project(TEXT, canonical(value), dictionary_index(dictionary()))
    assert projected["chatgpt_only"]["relations"][0]["predicate"] == "unsupported"
    assert not projected["hybrid"]["relations"]
    value = extraction()
    value["entities"][1]["taxon"] = None
    assert not project(TEXT, canonical(value), dictionary_index(dictionary()))["hybrid"]["relations"]


@pytest.mark.parametrize("invalid", [None, [], "bad", {"chemical": [], "gene": {}, "role_direction": []},
                                      {"evidence": {"quote": 1}, "benchmark_relation_type": []}])
def test_malformed_records_remain_visible_without_crashing(invalid):
    value = extraction()
    value["relations"] = [invalid]
    result = project(TEXT, canonical(value), dictionary_index(dictionary()))
    assert len(result["chatgpt_only"]["relations"]) == 1
    assert result["hybrid"]["relations"] == []


def test_source_whitelist_never_forwards_gold_or_prior_candidates():
    payload = json.loads(source_input({"text": TEXT, "entities": ["GOLD"], "relations": ["GOLD"], "prior": "LABEL"}))
    assert "GOLD" not in canonical(payload) and "LABEL" not in canonical(payload)


def test_v2_minimal_quote_response_needs_no_model_offsets():
    value = extraction()
    for entity in value["entities"]:
        entity.pop("start")
        entity.pop("end")
    value["relations"][0]["evidence"] = {"quote": TEXT}
    result = project(TEXT, canonical(value), dictionary_index(dictionary()))
    assert result["hybrid"]["relations"]
    assert result["chatgpt_only"]["relations"][0]["endpoints"]["gene"]["start"] == TEXT.index("TP53")
    assert "start" not in result["chatgpt_only"]["raw_entities"][0]


def test_repeated_entity_uses_unique_evidence_and_never_first_occurrence():
    source = "aspirin mentioned before. " + TEXT
    value = extraction()
    result = project(source, canonical(value), dictionary_index(dictionary()))
    endpoint = result["hybrid"]["relations"][0]["endpoints"]["chemical"]
    assert endpoint["start"] == source.rindex("aspirin")
    assert any(t["method"] == "unique_in_unique_evidence" for t in result["hybrid"]["alignment_trace"])
    ambiguous = TEXT + " " + TEXT
    result = project(ambiguous, canonical(value), dictionary_index(dictionary()))
    assert result["hybrid"]["relations"] == []
    assert "start" not in result["chatgpt_only"]["relations"][0]["evidence"]


def test_repeated_entity_inside_unique_evidence_is_unverified_despite_supplied_offsets():
    source = "Human aspirin binds TP53 and aspirin."
    value = extraction()
    value["relations"][0]["evidence"] = {"quote": source}
    result = project(source, canonical(value), dictionary_index(dictionary()))
    assert result["hybrid"]["relations"] == []
    entity, trace = align_exact(source, value["entities"][0], evidence={"quote": source, "start": 0, "end": len(source)})
    assert "start" not in entity and trace["method"] == "ambiguous"


@pytest.mark.parametrize("verb,predicate", [("increased", "Positive_Correlation"), ("reduced", "Negative_Correlation")])
def test_native_guideline_categories_keep_mechanism_and_direction_separate(verb, predicate):
    source = "Human aspirin " + verb + " TP53 expression."
    value = extraction()
    gene = value["entities"][1]
    gene["start"], gene["end"] = source.index("TP53"), source.index("TP53") + 4
    relation = value["relations"][0]
    relation.update({"benchmark_relation_type": predicate, "role_direction": "chemical_to_gene",
                     "scientific_mechanism": "expression", "evidence": {"start": 0, "end": len(source), "quote": source}})
    result = project(source, canonical(value), dictionary_index(dictionary()))
    assert result["hybrid"]["relations"][0]["predicate"] == predicate
    assert result["hybrid"]["relations"][0]["subject"] == "D001241"
    assert result["hybrid"]["decisions"][0]["direction_verified"]


def test_direction_unknown_does_not_remove_native_type_prediction_and_background_not_observed():
    value = extraction()
    value["relations"][0].update({"role_direction": "unknown", "observation": "background"})
    result = project(TEXT, canonical(value), dictionary_index(dictionary()))
    assert result["hybrid"]["relations"][0]["predicate"] == "Bind"
    assert "subject" not in result["hybrid"]["relations"][0]
    assert not result["hybrid"]["decisions"][0]["observed_event_verified"]


def test_auth_pending_has_no_fake_metrics_and_gold_waits_for_full_cohort(tmp_path, monkeypatch):
    evaluation, out = fixture(tmp_path, monkeypatch)
    state = evaluation.run_experiment(out, None)
    assert state["completed_papers"] == 0 and state["metrics"] is None
    with pytest.raises(ValueError, match="Full-cohort"):
        evaluation.evaluate_experiment(out, tmp_path)
    assert not (out / "comparison.json").exists()


def test_resume_complete_then_same_native_scorer_and_bootstrap(tmp_path, monkeypatch):
    evaluation, out = fixture(tmp_path, monkeypatch)
    assert evaluation.run_experiment(out, infer, max_papers=1)["pending_papers"] == 1
    assert evaluation.run_experiment(out, infer)["pending_papers"] == 0
    def forbidden(*args):
        raise AssertionError("Saved responses should be resumed without new calls")
    evaluation.run_experiment(out, forbidden)
    report = evaluation.evaluate_experiment(out, tmp_path)
    assert report["metrics"]["existing_algorithm"]["relation"]["tp"] == 1
    assert report["metrics"]["chatgpt_only"]["relation"]["tp"] == 2
    assert report["metrics"]["hybrid"]["relation"]["tp"] == 2
    assert report["differences"]["hybrid_minus_existing_algorithm"]["paired_paper_bootstrap2000_ci95"]
    assert "SECRET" not in "".join(p.read_text(encoding="utf-8") for p in out.rglob("*.json"))
    assert (out / "comparison.md").exists()


def test_reasoning_is_frozen_in_request_and_mismatched_client_cannot_run(tmp_path, monkeypatch):
    evaluation, out = fixture(tmp_path, monkeypatch, reasoning_effort="max")
    class Client:
        model = "fixture-model"
        reasoning_effort = "medium"
        def __call__(self, *args):
            return infer(*args)
    client = Client()
    with pytest.raises(ValueError, match="reasoning"):
        evaluation.run_experiment(out, client)
    client.reasoning_effort = "max"
    evaluation.run_experiment(out, client, max_papers=1)
    request = json.loads(next((out / "requests").glob("*.json")).read_bytes())
    assert request["reasoning"] == {"effort": "max"}


def test_incomplete_response_does_not_open_gold_and_retry_is_possible(tmp_path, monkeypatch):
    evaluation, out = fixture(tmp_path, monkeypatch)
    def incomplete(*args):
        return {"text": canonical(extraction()), "model": "fixture-model", "status": "incomplete"}
    state = evaluation.run_experiment(out, incomplete)
    assert state["state"] == "inference_failed" and state["completed_papers"] == 0
    assert not list((out / "responses").glob("*.json")) if (out / "responses").exists() else True
    assert evaluation.run_experiment(out, infer)["completed_papers"] == 2


def test_test_only_and_modified_projections_never_publish_real_scores(tmp_path, monkeypatch):
    evaluation, out = fixture(tmp_path, monkeypatch)
    def test_infer(*args):
        return {**infer(*args), "test_only": True}
    evaluation.run_experiment(out, test_infer)
    with pytest.raises(ValueError, match="test-only"):
        evaluation.evaluate_experiment(out, tmp_path)
    assert not (out / "comparison.json").exists()


def test_modified_projection_rejected_before_gold(tmp_path, monkeypatch):
    evaluation, out = fixture(tmp_path, monkeypatch)
    evaluation.run_experiment(out, infer)
    path = next((out / "predictions").glob("*.json"))
    value = json.loads(path.read_bytes())
    value["hybrid"]["relations"] = []
    path.write_text(canonical(value), encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        evaluation.evaluate_experiment(out, tmp_path)


def test_protected_partition_is_rejected_without_any_gold_access(tmp_path, monkeypatch):
    evaluation, out = fixture(tmp_path, monkeypatch)
    from nutri_literature import benchmarks
    monkeypatch.setattr(benchmarks, "_load_inputs", lambda *args: ({"documents": []}, {
        "gold_sha256": evaluation.GOLD_SHA256, "corpus": "bioredirect", "split": "test"}, evaluation.INPUT_SHA256))
    with pytest.raises(ValueError, match="protected"):
        evaluation.prepare_experiment(tmp_path, tmp_path / "baseline", out, "fixture-model", prepared_dir=tmp_path / "prepared")
