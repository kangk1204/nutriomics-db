import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"literature_service"))
from nutri_literature.evidence_v8_native.common import atomic, digest, filehash, home, read
from nutri_literature.evidence_v8_native.data import load, parse_markers, prepare
from nutri_literature.evidence_v8_native.learning import labels_for
from nutri_literature.evidence_v8_native.runner import score_sets, select_threshold


def raw_row(task, label, pmid="1"):
    text = "β [C]A[/C] binds [G]B[/G]."
    plain, _ = parse_markers(text)
    return {"pmid": pmid, "task": task, "label": label, "text": text,
            "segments": [{"start": 10, "end": 10+len(plain), "text": plain}]}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("EVIDENCE_V8_NATIVE_HOME", str(tmp_path/"native"))
    monkeypatch.setenv("EVIDENCE_V8_HOME", str(tmp_path/"core"))
    return tmp_path


def source(runtime, rows):
    p = runtime/"source/train.json"
    labels = sorted({r["label"] for r in rows})
    for row in rows:
        row["target"] = labels.index(row["label"])
    atomic(p, {"rows": rows, "labels": labels, "heldout_labels_read": False})
    atomic(p.with_name("manifest.json"), {"native_tasks_only": True, "data_sha256": filehash(p), "identity_overlap": []})
    return p


def test_markers_preserve_unicode_and_generic_argument_roles():
    plain, spans = parse_markers("β [G]gene[/G] interacts with [C]drug[/C].")
    assert plain == "β gene interacts with drug."
    assert spans[0]["role"] == "arg1"
    assert spans[0]["text"] == "drug"
    assert spans[1]["role"] == "arg2"
    assert spans[1]["text"] == "gene"
    assert all(plain[s["start"]:s["end"]] == s["text"] for s in spans)


@pytest.mark.parametrize("text", ["[C]A[/C] B", "[C][G]B[/G][/C]", "[C][/C] [G]B[/G]", "[C]A[/C] [C]B[/C] [G]G[/G]"])
def test_invalid_marker_sequences_are_not_silently_dropped(text):
    with pytest.raises(ValueError):
        parse_markers(text)


def test_prepare_cpu_only_preserves_native_multi_labels_and_core(runtime):
    rows = [raw_row("cg", "cg:Binding/Theme"), raw_row("cg", "cg:Binding/Site"),
            raw_row("drugprot", "drugprot:NONE", "2"), raw_row("drugprot", "drugprot:INHIBITOR", "3")]
    original = os.environ["EVIDENCE_V8_HOME"]
    result = prepare(source(runtime, rows))
    value = load()
    assert len(value["pairs"]) == 3
    assert next(r for r in value["pairs"] if r["task"] == "cg")["labels"] == ["cg:Binding/Site", "cg:Binding/Theme"]
    assert result["scope"]["automatic_entity_condition"] == "not_measured"
    assert not result["scope"]["full_event_extraction"]
    assert not (home()/"gpu-ledger.json").exists()
    assert not (runtime/"core").exists()
    assert os.environ["EVIDENCE_V8_HOME"] == original


def test_prepare_rejects_false_train_only_receipt(runtime):
    p = source(runtime, [raw_row("drugprot", "drugprot:INHIBITOR")])
    value = read(p)
    atomic(p, {**value, "heldout_labels_read": True})
    with pytest.raises(ValueError):
        prepare(p)


def test_protected_pmid_blocks_native_prepare(runtime):
    p = source(runtime, [raw_row("drugprot", "drugprot:INHIBITOR")])
    protection = runtime/"protected.json"
    atomic(protection, {"pmids": ["1"], "normalized_text_hashes": []})
    with pytest.raises(ValueError, match="보호"):
        prepare(p, protection)


def test_cg_unannotated_pairs_are_not_given_none(runtime):
    with pytest.raises(ValueError, match="NONE"):
        prepare(source(runtime, [raw_row("cg", "cg:NONE")]))


def test_none_is_not_a_native_positive_head():
    value = {"labels": ["cg:Binding/Theme", "drugprot:NONE", "drugprot:INHIBITOR"]}
    assert labels_for(value, "drugprot") == ["drugprot:INHIBITOR"]
    assert labels_for(value, "cg") == ["cg:Binding/Theme"]


def test_native_metrics_use_full_supplied_positive_denominator():
    rows = [{"pmid": "1", "key": "a", "labels": ["drugprot:INHIBITOR"]},
            {"pmid": "2", "key": "b", "labels": ["drugprot:NONE"]},
            {"pmid": "3", "key": "c", "labels": ["drugprot:ACTIVATOR"]}]
    labels = ["drugprot:INHIBITOR", "drugprot:ACTIVATOR"]
    scores = [{"types": [.9, 0.], "exists": .9}, {"types": [.9, 0.], "exists": .1}, {"types": [0., 0.], "exists": .9}]
    result = score_sets(rows, scores, labels, .5, "drugprot")
    assert (result["tp"], result["fp"], result["fn"]) == (1, 0, 1)
    assert not result["full_end_to_end_event_score"]
    assert result["per_type"]["drugprot:ACTIVATOR"]["fn"] == 1


def test_cg_no_negative_exist_gate_is_invented():
    rows = [{"pmid": "1", "key": "a", "labels": ["cg:Binding/Theme"]}]
    scores = [{"types": [.9], "exists": 0.}]
    assert score_sets(rows, scores, ["cg:Binding/Theme"], .5, "cg")["tp"] == 1


def test_prepared_native_table_tampering_is_detected(runtime):
    prepare(source(runtime, [raw_row("drugprot", "drugprot:INHIBITOR")]))
    p = home()/"data/train.json"
    v = read(p)
    atomic(p, {**v, "labels": ["changed"]})
    with pytest.raises(ValueError):
        load()


def test_frozen_linear_native_head_really_fits_and_masks_unseen_type(runtime):
    np = pytest.importorskip("numpy")
    pytest.importorskip("sklearn")
    from nutri_literature.evidence_v8_native.learning import linear_fit, linear_score
    features = home()/"features/synthetic-unit"
    features.mkdir(parents=True)
    x = np.asarray([[float(i < 5), float(i >= 5), i/10] for i in range(10)], dtype=np.float32)
    np.save(features/"features.npy", x)
    atomic(features/"manifest.json", {"sha256": filehash(features/"features.npy"), "synthetic_unit_only": True})
    rows = [{"labels": ["drugprot:INHIBITOR"] if i < 5 else ["drugprot:NONE"]} for i in range(10)]
    labels = ["drugprot:INHIBITOR", "drugprot:ACTIVATOR"]
    model = linear_fit(rows, labels, features, list(range(10)), 1., home()/"models/unit-linear")
    predictions = linear_score(features, list(range(10)), model)
    assert predictions[0]["types"][0] > predictions[-1]["types"][0]
    assert all(p["types"][1] == 0 for p in predictions)
    assert read(model/"model.json")["support"] == [True, False]


def test_fold_totals_preserve_native_gold_denominator():
    from nutri_literature.evidence_v8_native.runner import sum_metrics
    result = sum_metrics([{"tp": 2, "fp": 1, "fn": 3}, {"tp": 1, "fp": 0, "fn": 4}])
    assert (result["tp"], result["fp"], result["fn"]) == (3, 1, 7)
    assert result["recall"] == .3


def test_evaluation_requires_complete_predictions_before_gold(runtime, monkeypatch):
    from nutri_literature.evidence_v8_native import runner
    monkeypatch.setattr(runner, "verify_freeze", lambda: {})
    with pytest.raises(FileNotFoundError, match="prediction-receipt"):
        runner.evaluate()


def test_cg_loss_does_not_train_an_unannotated_relation_exists_head():
    torch = pytest.importorskip("torch")
    from nutri_literature.evidence_v8_native.network import native_loss
    types = torch.tensor([[0., 0.]], requires_grad=True)
    exists = torch.tensor([0.], requires_grad=True)
    value = native_loss({"types": types, "exists": exists}, [[1., 0.]], "cg", torch.tensor([1., 0.]), torch.ones(2))
    value.backward()
    assert exists.grad is None
    assert types.grad[0, 0] < 0
    assert types.grad[0, 1] == 0


def test_drugprot_none_trains_relation_absence_without_remapping():
    torch = pytest.importorskip("torch")
    from nutri_literature.evidence_v8_native.network import native_loss
    types = torch.tensor([[0., 0.]], requires_grad=True)
    exists = torch.tensor([0.], requires_grad=True)
    value = native_loss({"types": types, "exists": exists}, [[0., 0.]], "drugprot", torch.ones(2), torch.ones(2))
    value.backward()
    assert exists.grad[0] > 0


def test_bounded_native_window_keeps_all_argument_markers():
    pytest.importorskip("torch")
    from nutri_literature.evidence_v8_native.network import MARKERS, bounded
    class Tok:
        sep_token_id = 9999
        def convert_tokens_to_ids(self, values):
            return [9000+MARKERS.index(v) for v in values]
    ids = list(range(1000))
    ids[20:23] = [9000, 42, 9001]
    ids[900:903] = [9002, 43, 9003]
    selected = bounded(ids, Tok(), 128)
    assert len(selected) <= 128
    assert all(v in selected for v in (9000, 9001, 9002, 9003))
