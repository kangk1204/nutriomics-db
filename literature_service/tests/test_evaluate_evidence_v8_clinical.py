"""Synthetic input-first/offline-rescore contracts, not scientific performance."""
import importlib.util
import json
from pathlib import Path

import joblib
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/evaluate_evidence_v8_clinical.py"
spec = importlib.util.spec_from_file_location("clinical_development_script", SCRIPT)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class FakeCRF:
    def predict_single(self, features):
        return ["B"] + ["I"] * (len(features) - 1)


@pytest.fixture
def fixture(tmp_path):
    models, source, output = tmp_path / "models", tmp_path / "source", tmp_path / "experiment"
    models.mkdir()
    source.mkdir()
    entries = {}
    for role in evaluation.ROLES:
        path = models / (role + ".joblib")
        joblib.dump(FakeCRF(), path)
        entries[role] = {"model_sha256": evaluation.filehash(path)}
    (models / "manifest.json").write_text(json.dumps({"trained": True, "models": entries}), encoding="utf-8")
    text = "Adults β receive DHA."
    doc = {"pmid": "1", "text": text, "source_hash": evaluation.sha(text),
           "sections": [{"start": 0, "end": len(text), "label": "Abstract"}]}
    gold = {"schema_version": "literature-public-gold-v1", "corpus": "evidenceoutcomes", "split": "development",
            "manifest": {"annotation_status": "published_corpus_annotations"},
            "documents": [{**doc, "annotated_spans": [{"label": "outcomes", "text": "DHA", "start": text.index("DHA"), "end": text.index("DHA") + 3}]}]}
    gold_path = source / "gold.json"
    gold_path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    inputs = source / "inputs.json"
    inputs.write_text(json.dumps({"schema_version": "literature-public-inputs-v1", "split": "development", "corpus": "evidenceoutcomes", "documents": [doc]}, ensure_ascii=False), encoding="utf-8")
    receipt = source / "manifest.json"
    receipt.write_text(json.dumps({"corpus": "evidenceoutcomes", "split": "development", "inputs_sha256": evaluation.filehash(inputs), "gold_sha256": evaluation.filehash(gold_path)}), encoding="utf-8")
    return models, inputs, receipt, output, gold_path


def test_prediction_does_not_open_gold_and_unicode_quotes_match(fixture, monkeypatch):
    models, inputs, receipt, output, gold = fixture
    original = Path.read_bytes

    def protected(path):
        if path == gold:
            raise AssertionError("Gold must not be opened before all predictions")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", protected)
    result = evaluation.predict(models, inputs, receipt, output)
    assert result["documents"] == 1 and result["status"] == "all_predictions_saved_no_gold_read"
    record = json.loads((output / "predictions/paper-1.json").read_text(encoding="utf-8"))
    assert len(record["annotated_spans"]) == 3
    for span in record["annotated_spans"]:
        assert span["text"] == "Adults β receive DHA."


def test_native_span_metric_and_offline_rescore_are_identical(fixture):
    models, inputs, receipt, output, gold = fixture
    evaluation.predict(models, inputs, receipt, output)
    report = evaluation.score(output, gold)
    assert report["metrics"]["spans_outcomes"] == {"tp": 1, "fp": 3, "fn": 0, "precision": .25, "recall": 1., "f1": .4}
    assert report["source_position_errors"] == 0 and report["full_ico_group_dose_time"] == "not_evaluated"
    assert evaluation.score(output, gold) == report
    assert json.loads((output / "offline-rescore.json").read_text())["identical"]


def test_missing_prediction_blocks_gold_read(fixture, monkeypatch):
    models, inputs, receipt, output, gold = fixture
    evaluation.predict(models, inputs, receipt, output)
    (output / "predictions/paper-1.json").unlink()
    original = Path.read_bytes

    def protected(path):
        assert path != gold
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", protected)
    with pytest.raises(FileNotFoundError):
        evaluation.score(output, gold)


def test_comparison_requires_same_input_and_per_paper_gold_denominator(fixture):
    models, inputs, receipt, output, gold = fixture
    evaluation.predict(models, inputs, receipt, output)
    evaluation.score(output, gold)
    equal = evaluation.compare(output, output / "report.json")
    assert equal["same_cohort_and_gold_token_denominators"] and equal["difference"]["f1"] == 0
    baseline = evaluation.read(output / "report.json")
    baseline["input_sha256"] = "a" * 64
    other = output / "different-baseline.json"
    other.write_text(json.dumps(baseline), encoding="utf-8")
    assert evaluation.compare(output, other)["difference"] is None


def test_test_split_or_gold_in_source_input_are_rejected(fixture):
    models, inputs, receipt, output, _ = fixture
    value = evaluation.read(inputs)
    value["documents"][0]["annotated_spans"] = []
    inputs.write_text(json.dumps(value), encoding="utf-8")
    manifest = evaluation.read(receipt)
    manifest["inputs_sha256"] = evaluation.filehash(inputs)
    receipt.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Source-only"):
        evaluation.predict(models, inputs, receipt, output)
    assert not output.exists()
