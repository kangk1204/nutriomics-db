"""The evaluator adapter selects the predeclared saved profile, without tuning."""

import pytest

from nutri_literature import benchmarks, chatgpt_evaluation as frozen, chatgpt_rescore as corrected
from nutri_literature.chatgpt_extraction import SCHEMA
from nutri_literature.contract_v7 import canonical, sha

TEXT = "Human aspirin binds TP53."


def setup(tmp_path, monkeypatch, nested=True, complete=True):
    monkeypatch.setattr(frozen, "PAPERS", 2)
    monkeypatch.setattr(frozen, "GOLD_RELATIONS", 2)
    monkeypatch.setattr(frozen, "BASELINE_COUNTS", (1, 1, 1))
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    for pmid in ("1", "2"):
        selected = {"policy": "predeclared-existing-ID-union", "relations": [
            {"entity1": "D001241", "entity2": "7157" if pmid == "1" else "9", "predicate": "Bind"}]}
        saved = {"source_hash": sha(TEXT), "relations": [{"entity1": "D001241", "entity2": "999", "predicate": "Bind"}]}
        if nested:
            saved["recall_ensemble"] = selected
        else:
            saved.update(selected)
        (baseline / ("paper-" + pmid + ".json")).write_text(canonical(saved), encoding="utf-8")
    receipt = {"corpus": "bioredirect", "split": "development", "complete_partition": True,
               "official_split": True, "inputs_sha256": frozen.INPUT_SHA256, "gold_sha256": frozen.GOLD_SHA256}
    documents = [{"pmid": pmid, "text": TEXT, "source_hash": sha(TEXT)} for pmid in ("1", "2")]
    monkeypatch.setattr(benchmarks, "_load_inputs", lambda *args: ({"documents": documents}, receipt, frozen.INPUT_SHA256))
    output = tmp_path / "experiment"
    def gold(*args):
        assert len(list((output / "predictions").glob("paper-*.json"))) == 2
        return {"documents": [{"pmid": pmid, "entities": [
            {"kind": "component", "native_ids": ["D001241"]}, {"kind": "gene", "native_ids": ["7157"]}],
            "relations": [{"entity1": "D001241", "entity2": "7157", "predicate": "Bind"}]} for pmid in ("1", "2")]}, receipt, frozen.GOLD_SHA256
    monkeypatch.setattr(benchmarks, "_load_gold", gold)
    dictionary = tmp_path / "dictionary.json"
    dictionary.write_text(canonical({"schema_version": "literature-chatgpt-id-dictionary-v1",
        "source_references": ["https://www.ncbi.nlm.nih.gov/gene/"], "records": [
            {"kind": "chemical", "native_id": "D001241", "names": ["aspirin"]},
            {"kind": "gene", "native_id": "7157", "taxon": 9606, "names": ["TP53"]}]}), encoding="utf-8")
    frozen.prepare_experiment(tmp_path, baseline, output, "fixture-model", dictionary, tmp_path / "prepared")
    value = {"schema_version": SCHEMA, "entities": [
        {"key": "c", "kind": "chemical", "text": "aspirin", "native_id": "D001241"},
        {"key": "g", "kind": "gene", "text": "TP53", "native_id": "7157", "taxon": 9606}], "relations": [
        {"chemical": "c", "gene": "g", "benchmark_relation_type": "Bind", "role_direction": "undirected",
         "negated": False, "speculative": False, "observation": "observed", "evidence": {"quote": TEXT}}]}
    frozen.run_experiment(output, lambda *args: {"text": canonical(value), "model": "fixture-model", "status": "completed"},
                          max_papers=None if complete else 1)
    return output


def test_saved_union_profile_reproduced_before_publishing_and_originals_unchanged(tmp_path, monkeypatch):
    output = setup(tmp_path, monkeypatch)
    originals = {str(p): sha(p.read_bytes()) for p in output.rglob("*.json")}
    with pytest.raises(ValueError, match=corrected.KNOWN_BASELINE_ERROR):
        frozen.evaluate_experiment(output, tmp_path)
    assert not (output / "comparison.json").exists()
    report = corrected.score_corrected_experiment(output, tmp_path)
    metric = report["metrics"]["existing_algorithm"]["relation"]
    assert (metric["tp"], metric["fp"], metric["fn"]) == (1, 1, 1)
    assert report["metrics"]["chatgpt_only"]["relation"]["tp"] == 2
    assert report["baseline_adapter_correction"]["selected_profiles"] == {"recall_ensemble": 2}
    assert report["baseline_adapter_correction"]["original_full_provenance_preflight_passed"]
    assert report["differences"]["hybrid_minus_existing_algorithm"]["paired_paper_bootstrap2000_ci95"]
    assert all(sha(open(path, "rb").read()) == digest for path, digest in originals.items())
    assert (output / "comparison-correction-contract.json").exists()
    assert (output / "comparison.md").exists()
    assert corrected.score_corrected_experiment(output, tmp_path) == report


def test_flat_baseline_remains_supported_without_unnecessary_rewrite(tmp_path, monkeypatch):
    output = setup(tmp_path, monkeypatch, nested=False)
    report = corrected.score_corrected_experiment(output, tmp_path)
    assert report["metrics"]["existing_algorithm"]["relation"]["tp"] == 1
    assert "baseline_adapter_correction" not in report
    assert corrected.baseline_profile({"relations": []})[1] == "flat_relations_compatibility"


def test_other_provenance_errors_are_never_swallowed(tmp_path, monkeypatch):
    output = setup(tmp_path, monkeypatch)
    path = next((output / "baseline").glob("*.json"))
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Baseline prediction changed"):
        corrected.score_corrected_experiment(output, tmp_path)
    assert not (output / "comparison.json").exists()


def test_incomplete_cohort_cannot_use_correction_to_open_gold(tmp_path, monkeypatch):
    output = setup(tmp_path, monkeypatch, complete=False)
    def forbidden(*args):
        raise AssertionError("Gold must remain closed")
    monkeypatch.setattr(benchmarks, "_load_gold", forbidden)
    with pytest.raises(ValueError, match="Full-cohort"):
        corrected.score_corrected_experiment(output, tmp_path)
    assert not (output / "comparison.json").exists()


def test_malformed_nested_profile_is_not_replaced_by_score_chosen_flat_profile():
    with pytest.raises(ValueError, match="malformed"):
        corrected.baseline_profile({"recall_ensemble": {"relations": None}, "relations": []})
