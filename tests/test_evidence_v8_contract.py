"""Scientific invariants: no benchmark-answer shortcuts or silent success."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"literature_service"))
from nutri_literature.evidence_v8.common import (POLICY, OwnerBusy, atomic, digest,
    gpu_process_allowed, groups, home, metric, owner, read, textkey, validate_gpu, wilson)
from nutri_literature.evidence_v8.data import (CorpusGuard, id_supervision, source_only)
from nutri_literature.evidence_v8.learning import apply_id, id_policy, species_compatible
from nutri_literature.evidence_v8.relations import accepted, paper_prediction, semantic_labels, select_policy
from nutri_literature.evidence_v8.retrieval import candidate_menu


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("EVIDENCE_V8_HOME", str(tmp_path))
    return tmp_path


def mention():
    return {"kind": "gene", "start": 0, "end": 3, "text": "CAT", "previous_id": "wrong",
            "supported_taxa": [9606], "candidate_ids": ["correct", "wrong"]}


def row():
    return {"pmid": "1", "text": "CAT changed.", "mention": mention(), "menu": [None, "correct", "wrong"],
            "menu_taxa": [None, 9606, 9606], "gold_ids": ["correct"], "valid_weight": 1.,
            "valid_span": True, "retrieval_miss": False}


def enabled():
    return {"enabled": True, "threshold": .8, "margin": .1}


def test_candidate_menu_is_identical_and_old_id_preserved():
    m = {"candidate_ids": [str(i) for i in range(200)], "previous_id": "old"}
    assert len(candidate_menu(m)) == 51
    assert candidate_menu(m)[0] is None
    assert candidate_menu(m)[-1] == "old"
    assert candidate_menu(m) == candidate_menu(json.loads(json.dumps(m)))


def test_gold_is_never_inserted_into_menu():
    d = {"text": "CAT", "entities": [{"kind": "gene", "start": 0, "end": 3, "native_ids": ["unretrieved"]}]}
    m = mention()
    result = id_supervision(d, m, candidate_menu(m))
    assert "unretrieved" not in candidate_menu(m)
    assert result["retrieval_miss"]
    assert result["rank_weight"] == 0
    assert result["valid_target"] == result["valid_weight"] == 1


def test_false_mention_trains_nil_and_validity_separately():
    result = id_supervision({"text": "CAT", "entities": []}, mention(), [None, "wrong"])
    assert result["targets"] == [1., 0.]
    assert result["rank_weight"] == 1
    assert result["valid_target"] == 0


def test_missing_gene_annotation_is_not_negative():
    d = {"text": "CAT", "entities": [], "relation_annotation_complete": False, "gene_annotation_complete": False}
    result = id_supervision(d, mention(), [None, "id"])
    assert result["rank_weight"] == result["valid_weight"] == 0


def test_protected_dapt_pmid_pmcid_and_normalized_text():
    guard = CorpusGuard({"pmids": ["1"], "pmcids": ["PMC2"], "text_hashes": [textkey("Known text")]},
                        [{"pmid": "3", "text": "Official training paper"}])
    assert guard.reason({"pmid": "1", "text": "new"}) == "protected_pmid"
    assert guard.reason({"pmid": "4", "pmcid": "PMC2", "text": "new"}) == "protected_pmcid"
    assert guard.reason({"pmid": "4", "text": "  KNOWN   text "}) == "protected_text"
    assert guard.reason({"pmid": "3", "text": "new"}) == "protected_pmid"
    assert guard.reason({"pmid": "4", "text": "unknown"}) is None


@pytest.mark.parametrize("name", ["NVIDIA H200", "NVIDIA RTX A6000"])
def test_excluded_gpu_rejected(name):
    with pytest.raises(RuntimeError):
        validate_gpu({"name": name, "uuid": "GPU-a", "total_mib": "16384", "free_mib": "16000"})


def test_blacklisted_uuid_and_low_free_memory_rejected():
    r = {"name": "NVIDIA GeForce RTX 5060 Ti", "uuid": "GPU-a", "total_mib": "16384", "free_mib": "16000"}
    with pytest.raises(RuntimeError):
        validate_gpu(r, ["GPU-a"])
    with pytest.raises(RuntimeError):
        validate_gpu({**r, "free_mib": "1000"})
    validate_gpu(r)


def test_only_explicit_verified_small_desktop_process_is_allowed():
    process = {"pid": "123", "gpu_uuid": "GPU-a", "used_mib": "38"}
    policy = {"desktop_allowlist": [{"executable": "/usr/bin/ptyxis", "maximum_mib": 128}]}
    lookup = lambda pid: "/usr/bin/ptyxis"
    resolve = lambda path: path
    assert gpu_process_allowed(process, "GPU-a", policy, 999, lookup, resolve)
    assert not gpu_process_allowed(process, "GPU-a", {}, 999, lookup, resolve)
    assert not gpu_process_allowed({**process, "used_mib": "129"}, "GPU-a", policy, 999, lookup, resolve)
    assert not gpu_process_allowed(process, "GPU-a", policy, 999, lambda pid: "/usr/bin/python3", resolve)
    assert not gpu_process_allowed({**process, "used_mib": "1"}, "GPU-a", policy, 999, lambda pid: "/usr/bin/python3", resolve)
    assert not gpu_process_allowed({**process, "used_mib": "[N/A]"}, "GPU-a", policy, 999, lookup, resolve)


def test_desktop_policy_cannot_whitelist_python_or_large_memory():
    process = {"pid": "123", "gpu_uuid": "GPU-a", "used_mib": "1"}
    assert not gpu_process_allowed(process, "GPU-a", {"desktop_allowlist": [{"executable": "/usr/bin/python3", "maximum_mib": 128}]},
                                   999, lambda pid: "/usr/bin/python3", lambda path: path)
    assert not gpu_process_allowed(process, "GPU-a", {"desktop_allowlist": [{"executable": "/usr/bin/ptyxis", "maximum_mib": 256}]},
                                   999, lambda pid: "/usr/bin/ptyxis", lambda path: path)


def test_fold_groups_connect_family_text_and_pmcid():
    docs = [{"pmid": str(i), "text": f"paper {i}"} for i in range(20)]
    docs[0]["pmcid"] = docs[1]["pmcid"] = "PMC1"
    docs[1]["study_family"] = docs[2]["study_family"] = "NCT1"
    docs[3]["text"] = docs[2]["text"]
    f = groups(docs, 5, "seed")
    assert len({f[str(i)] for i in range(4)}) == 1
    assert f == groups(list(reversed(docs)), 5, "seed")


def test_source_only_rejects_labels_and_keeps_unicode():
    d = {"pmid": "1", "text": "β CAT —"}
    assert source_only(d) == d
    with pytest.raises(ValueError):
        source_only({**d, "entities": []})


def test_existing_id_preserved_if_policy_disabled():
    r = row()
    decision = apply_id(r, {"probabilities": [.01, .98, .01], "validity": .99}, {**enabled(), "enabled": False})
    assert decision["selected_id"] == "wrong"
    assert decision["proposed_id"] == "correct"


@pytest.mark.parametrize("taxa,candidate_taxon", [([], 9606), ([9606, 10090], 9606), ([9606], 10090), ([7955], 7955)])
def test_species_conflict_blocks_id_changes(taxa, candidate_taxon):
    r = row()
    r["mention"]["supported_taxa"] = taxa
    r["menu_taxa"][1] = candidate_taxon
    assert not species_compatible(r, 1)
    assert apply_id(r, {"probabilities": [.01, .98, .01], "validity": .99}, enabled())["selected_id"] == "wrong"


def test_unambiguous_supported_species_can_change_id():
    assert apply_id(row(), {"probabilities": [.01, .98, .01], "validity": .99}, enabled())["selected_id"] == "correct"


def test_nil_withdrawal_uses_own_policy_and_invalidity_gate():
    p = {"link": enabled(), "withdraw": {**enabled(), "enabled": False}}
    s = {"probabilities": [.98, .01, .01], "validity": .01}
    assert apply_id(row(), s, p)["selected_id"] == "wrong"
    p["withdraw"] = enabled()
    assert apply_id(row(), s, p)["selected_id"] is None
    assert apply_id(row(), {**s, "validity": .99}, p)["selected_id"] == "wrong"


def test_wilson_minimum_gate_does_not_enable_49_perfect_changes():
    rows = [row() for _ in range(49)]
    scores = [{"probabilities": [.005, .99, .005], "validity": .99} for _ in rows]
    assert not id_policy(rows, scores)["enabled"]
    assert wilson(50, 50) > .9
    assert id_policy(rows+[row()], scores+[scores[0]])["enabled"]


def test_semantic_relation_is_independent_of_wrong_automatic_id():
    d = {"entities": [{"kind": "chemical", "start": 0, "end": 1, "native_ids": ["C1"]},
                       {"kind": "gene", "start": 2, "end": 3, "native_ids": ["G1"]}],
         "relations": [{"entity1": "C1", "entity2": "G1", "predicate": "Bind"}]}
    c = {"kind": "chemical", "mentions": [{"start": 0, "end": 1, "previous_id": "bad"}]}
    g = {"kind": "gene", "mentions": [{"start": 2, "end": 3, "previous_id": "bad2"}]}
    assert semantic_labels(d, c, g) == ["Bind"]


def test_unknown_type_never_becomes_association():
    r = {"pmid": "1", "ids": ["C1", "G1"]}
    scores = [{"exists": .99, "types": [0.]*8}]
    assert accepted([r], scores, .5) == set()


def test_relation_policy_uses_full_gold_denominator_including_no_candidates():
    d = {"pmid": "1", "entities": [{"kind": "chemical", "native_ids": ["C1"]}, {"kind": "gene", "native_ids": ["G1"]}],
         "relations": [{"entity1": "C1", "entity2": "G1", "predicate": "Bind"}]}
    p = select_policy([], [], [d])
    assert p["full_gold_denominator"] == 1
    assert p["fn"] == 1
    assert not p["passed"]


def test_owner_does_not_erase_running_job(runtime):
    with owner():
        original = (runtime/"owner.json").read_bytes()
        with pytest.raises(OwnerBusy):
            with owner():
                pass
        assert (runtime/"owner.json").read_bytes() == original
    assert not (runtime/"owner.json").exists()


def test_immutable_results_refuse_overwrite(runtime):
    p = runtime/"x.json"
    atomic(p, {"a": 1}, True)
    atomic(p, {"a": 1}, True)
    with pytest.raises(ValueError):
        atomic(p, {"a": 2}, True)


def test_dapt_not_ready_cannot_read_candidate_documents(runtime):
    from nutri_literature.evidence_v8.auxiliary import corpus_contract
    atomic(runtime/"corpus/manifest.json", {"status": "needs_language_verification", "training_ready": False,
           "candidate_files": [{"path": "documents.jsonl", "sha256": "unused"}]})
    with pytest.raises(FileNotFoundError):
        corpus_contract(runtime/"corpus")


def test_loss_mask_does_not_turn_retrieval_miss_into_nil():
    torch = pytest.importorskip("torch")
    from nutri_literature.evidence_v8.networks import listwise_loss
    logits = torch.tensor([1., 2., 3.], requires_grad=True)
    valid = torch.tensor([0., 0., 0.], requires_grad=True)
    loss = listwise_loss(logits, [1., 0., 0.], 0., valid, 1., 1.)
    loss.backward()
    assert torch.equal(logits.grad, torch.zeros(3))
    assert valid.grad[0] < 0


def test_document_pooling_is_invariant_to_view_order():
    torch = pytest.importorskip("torch")
    from nutri_literature.evidence_v8.networks import relation_loss
    torch.manual_seed(7)
    output = {"exists": torch.randn(3), "types": torch.randn(3, 8), "direction": torch.randn(3, 3), "evidence": torch.randn(3)}
    r = {"exists": True, "semantic_types": ["Bind"], "direction_target": 1, "evidence_targets": None}
    a = relation_loss(output, r)
    b = relation_loss({k: v.flip(0) for k, v in output.items()}, r)
    assert torch.allclose(a, b)


def test_ner_annotation_scope_masks_other_entity_type():
    from nutri_literature.evidence_v8.ner import annotation_scope
    assert annotation_scope({"gene_annotation_complete": True}) == [True, False]
    assert annotation_scope({"gene_annotation_complete": False}) == [False, True]


def test_ner_crossfit_never_predicts_training_paper(monkeypatch):
    from nutri_literature.evidence_v8 import ner
    docs = [{"pmid": str(i), "text": f"unique paper {i}"} for i in range(20)]
    calls = []
    def fake_infer(train, held):
        a, b = {d["pmid"] for d in train}, {d["pmid"] for d in held}
        assert not a & b
        calls.append((a, b))
        return [{"pmid": d["pmid"]} for d in held]
    monkeypatch.setattr(ner, "infer", fake_infer)
    rows = ner.crossfit(docs)
    assert len(calls) == 5
    assert {r["pmid"] for r in rows} == {d["pmid"] for d in docs}


def test_ner_rejects_normalized_duplicate_before_model_fit(runtime):
    from nutri_literature.evidence_v8 import ner
    with pytest.raises(ValueError, match="원문/연구가족"):
        ner.infer([{"pmid": "1", "text": "A  paper"}], [{"pmid": "2", "text": "a paper"}])


def test_paper_prediction_preserves_unicode_quotes_and_does_not_confirm_events():
    document = {"pmid": "1", "text": "β CAT — binds."}
    endpoints = [{"kind": "chemical", "start": 0, "end": 1, "text": "β"},
                 {"kind": "gene", "start": 2, "end": 5, "text": "CAT"}]
    span = {"start": 0, "end": len(document["text"]), "text": document["text"]}
    rows = [{"pmid": "1", "ids": ["C1", "G1"], "endpoints": endpoints, "views": [{"segments": [span]}]}]
    scores = [{"exists": .99, "types": [0., .99, 0., 0., 0., 0., 0., 0.], "direction": [.34, .33, .33]}]
    out = paper_prediction(document, rows, scores, .5)
    assert out["relations"][0]["evidence"] == [span]
    assert out["relations"][0]["predicate"] == "Bind"
    assert out["relations"][0]["subject"] is None
    assert out["relations"][0]["direct_evidence_confirmed"] is False


def test_evaluate_does_not_read_gold_before_all_predictions(runtime, monkeypatch):
    from nutri_literature.evidence_v8 import runner
    monkeypatch.setattr(runner, "verify_freeze", lambda: {})
    with pytest.raises(FileNotFoundError, match="manifest"):
        runner.evaluate()


def test_predict_rejects_gold_source_before_inference(runtime, monkeypatch):
    from nutri_literature.evidence_v8 import relations
    monkeypatch.setattr(relations, "documents", lambda name: [{"pmid": "1", "text": "CAT", "entities": []}])
    with pytest.raises(ValueError, match="정답"):
        relations.build_rows("development", {})


def test_clinical_missing_annotation_category_is_not_negative():
    from nutri_literature.evidence_v8.auxiliary import clinical_rows
    d = {"pmid": "1", "text": "Patients were enrolled.",
         "sections": [{"label": "abstract", "start": 0, "end": 23}],
         "annotated_spans": [], "annotated_labels": ["participants"]}
    rows = list(clinical_rows(d))
    assert rows
    assert set(rows[0]["labels"]) == {"participants"}
    assert set(rows[0]["labels"]["participants"]) == {"O"}
    with pytest.raises(ValueError, match="annotated_labels"):
        list(clinical_rows({k: v for k, v in d.items() if k != "annotated_labels"}))


def test_local_saved_evaluation_requires_explicit_remote_verification_receipt(runtime):
    from nutri_literature.evidence_v8.runner import evaluate_saved
    with pytest.raises(FileNotFoundError, match="export-verification"):
        evaluate_saved()


def test_saved_export_detects_changed_source_without_full_weight_download(runtime):
    from nutri_literature.evidence_v8.common import filehash, sources
    from nutri_literature.evidence_v8.runner import verify_saved_export
    atomic(runtime/"freeze.json", {"files": {"fits/x/model.safetensors": "remote-only-weight-hash"}, "code": sources()})
    atomic(runtime/"data/development-sources.json", {"documents": []})
    receipt = {"format": "v8-source-only-verified-export/1", "full_remote_freeze_verified": True,
        "development_labels_included": False, "model_weights_included": False,
        "freeze_sha256": filehash(runtime/"freeze.json"), "code": sources(),
        "verified_frozen_files": read(runtime/"freeze.json")["files"],
        "verified_upstream_files": {"ner/models/x/model.safetensors": "remote-only-ner-hash"},
        "export_files": {"freeze.json": filehash(runtime/"freeze.json"),
                         "data/development-sources.json": filehash(runtime/"data/development-sources.json")}}
    atomic(runtime/"export-verification.json", receipt)
    assert verify_saved_export()["full_remote_freeze_verified"]
    atomic(runtime/"data/development-sources.json", {"documents": [{"pmid": "changed"}]})
    with pytest.raises(ValueError, match="해시"):
        verify_saved_export()
