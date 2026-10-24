"""Synthetic resource/resume regressions, not scientific performance evidence."""

import json

import numpy as np
import pytest

from nutri_literature.clinical_training_v6 import DenseClinicalHead, numeric_examples, train
from nutri_literature.db import digest, dumps


def test_numeric_paper_checkpoints_resume_and_reject_tampering(tmp_path):
    documents = [{"pmid": str(i), "text": str(i)} for i in range(3)]
    visited = []

    def first(doc):
        visited.append(doc["pmid"])
        if doc["pmid"] == "1":
            raise RuntimeError("synthetic interruption")
        yield {"x": 1, "y": 2}, 1

    with pytest.raises(RuntimeError, match="interruption"):
        numeric_examples(documents, ["x", "y"], tmp_path, {"fixture": True}, first)
    assert visited == ["0", "1"]
    visited.clear()

    def resumed(doc):
        visited.append(doc["pmid"])
        yield {"x": 1, "y": 2}, 1

    matrix, labels, pmids, receipts = numeric_examples(
        documents, ["x", "y"], tmp_path, {"fixture": True}, resumed
    )
    assert visited == ["1", "2"] and isinstance(matrix, np.memmap)
    assert matrix.dtype == np.float32 and matrix.tolist() == [[1, 2]] * 3
    assert labels.tolist() == [1] * 3 and pmids == ["0", "1", "2"]
    del matrix, labels
    cached = tmp_path / receipts[0]["filename"]
    cached.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checkpoint hash"):
        numeric_examples(documents, ["x", "y"], tmp_path, {"fixture": True}, resumed)


def test_dense_clinical_prediction_matches_training_columns_and_empty_input():
    head = DenseClinicalHead(["x", "y"]).fit(
        np.asarray([[0, 0], [1, 1], [0, 0], [1, 1]], dtype=np.float32), [0, 1, 0, 1]
    )
    actual = head.probabilities([{"y": 1, "x": 1, "ignored": 999}])
    expected = head.matrix_probabilities(np.asarray([[1, 1]], dtype=np.float32))[0]
    assert actual[0]["1"] == pytest.approx(expected[head.classes_.index(1)])
    assert head.probabilities([]) == []
    constant = DenseClinicalHead(["x"]).fit(np.ones((3, 1), dtype=np.float32), [0, 0, 0])
    assert constant.probabilities([{}]) == [{"0": 1.0}]


def test_clinical_head_fit_uses_paper_oof_and_real_numeric_cache(tmp_path, monkeypatch):
    import joblib
    from nutri_literature import audit_v6, clinical_training_v6 as module

    text = "pressure random."
    docs = [{"pmid": str(i), "text": text, "sections": [],
             "annotated_spans": [{"start": 0, "end": 8, "label": "outcomes"}]}
            for i in range(5)]
    data = tmp_path / "train.json"
    data.write_text(dumps({"split": "train", "documents": docs}))
    audit = tmp_path / "audit.json"
    audit.write_text(dumps({"passed": True, "protected_overlap": 0,
                           "audited_training_sha256": digest(data.read_bytes()),
                           "training_pmids": [d["pmid"] for d in docs]}))

    class Encoder:
        def __init__(self, *args):
            self.manifest = {"dimension": 2, "synthetic_fixture": True}

        def load(self):
            pass

        def encode(self, text, sentence, entities):
            return np.asarray([[1, 1]] + [[int(e["start"] == 0)] * 2 for e in entities], dtype=np.float32)

    class Clinical:
        def __init__(self, *args):
            pass

        def load(self):
            pass

        def predict(self, sentences):
            return sentences

    class Parser:
        def parse(self, sentence):
            return []

    monkeypatch.setattr(module, "FrozenEncoder", Encoder)
    monkeypatch.setattr(module, "ClinicalModels", Clinical)
    monkeypatch.setattr(module, "parser_for", lambda _: Parser())
    monkeypatch.setattr(module, "outcome_candidates", lambda *args: [
        {"start": 0, "end": 8, "text": "pressure"},
        {"start": 9, "end": 15, "text": "random"}])
    monkeypatch.setattr(audit_v6, "upstream_clinical", lambda *args: {"synthetic_fixture": True})
    output = tmp_path / "model"
    record = train(data, "outcome", tmp_path, tmp_path / "cache", audit, output)
    assert record["numeric_features"]["rows"] == 10
    assert set(record["fold_assignments"].values()) == set(range(5))
    assert record["test_used_for_selection"] is False
    assert len(record["configurations"]) == 3
    assert json.loads((output / "manifest.json").read_bytes()) == record
    head = joblib.load(output / "outcome.joblib")
    assert head.probabilities([{"0": 1, "1": 1, "2": 1, "3": 1}])[0]["1"] > 0.5


def test_paired_comparison_rejects_changed_cohort_and_denominator():
    from scripts.compare_v5_v6 import paired_rows

    def report(pmid, tp, fp, fn):
        return {"metrics": {"relation": {}}, "per_paper": [
            {"pmid": pmid, "group_id": pmid, "relation": {"tp": tp, "fp": fp, "fn": fn}}]}

    baseline = report("fixture-1", 0, 1, 2)
    with pytest.raises(ValueError, match="same paper"):
        paired_rows(baseline, report("fixture-2", 1, 0, 1), iterations=100)
    with pytest.raises(ValueError, match="denominator"):
        paired_rows(baseline, report("fixture-1", 1, 0, 2), iterations=100)
    measured = paired_rows(baseline, report("fixture-1", 1, 0, 1), iterations=100)
    assert measured["relation"]["delta_percentage_points"]["f1"] == pytest.approx(200 / 3)
    assert measured["relation"]["v5"]["tp"] == 0
    assert measured["relation"]["v6"]["tp"] == 1


def test_pmc_only_identity_requires_both_official_absence_receipts(tmp_path):
    from nutri_literature.clinical_identity_v6 import auditable_identity

    row = {"pmid": "PMC123", "pmcid": "PMC123", "pmid_mapping_known": False,
           "identifier_resolution": "confirmed_pmc_only", "pmc_only_receipts": {}}
    assert not auditable_identity(row, tmp_path)
    ncbi = tmp_path / "ncbi.xml"
    ncbi.write_text('<eLinkResult><LinkSet><IdList><Id>123</Id></IdList></LinkSet></eLinkResult>')
    epmc = tmp_path / "epmc.json"
    epmc.write_text(dumps({"request": {"queryString": "PMC123"}, "resultList": {"result": [
        {"pmcid": "PMC123", "id": "PMC123", "source": "PMC"}]} }))
    row["pmc_only_receipts"] = {
        "ncbi": {"url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
                 "filename": ncbi.name, "sha256": digest(ncbi.read_bytes())},
        "europepmc": {"url": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                      "filename": epmc.name, "sha256": digest(epmc.read_bytes())}}
    assert auditable_identity(row, tmp_path) and row["pmid_mapping_known"] is False
    epmc.write_text(dumps({"request": {"queryString": "PMC123"}, "resultList": {"result": [
        {"pmcid": "PMC123", "id": "99", "source": "MED"}]} }))
    row["pmc_only_receipts"]["europepmc"]["sha256"] = digest(epmc.read_bytes())
    assert not auditable_identity(row, tmp_path)
