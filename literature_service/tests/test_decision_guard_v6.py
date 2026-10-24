from copy import deepcopy
import pytest

from nutri_literature.decision_guard_v6 import apply, guard_comparisons, guard_relation_hypotheses


def test_guard_defers_unresolved_ids_but_preserves_full_review_hypothesis():
    hypothesis = {
        "type_selected": True,
        "chemical": {"text": "drug", "normalized_id": "MESH:D1"},
        "gene": {"text": "gene", "normalized_id": None},
        "evidence": {"quote": "Original."},
    }
    before = deepcopy(hypothesis)
    rows = guard_relation_hypotheses([hypothesis])
    assert hypothesis == before
    assert not rows[0]["type_selected"]
    assert rows[0]["raw_type_proposed"] and rows[0]["review_candidate_preserved"]
    assert rows[0]["evidence"] == before["evidence"]


def test_guarded_comparisons_preserve_different_doses_and_defer_same_arm():
    rows = [
        {"intervention_description": "placebo", "comparator_description": "the placebo group"},
        {"intervention_description": "drug 1 mg", "comparator_description": "drug 2 mg"},
    ]
    selected, deferred = guard_comparisons(rows)
    assert len(selected) == len(deferred) == 1
    assert selected[0]["intervention_description"] == "drug 1 mg"
    assert deferred[0]["reasons"] == ["same_intervention_and_comparator"]


def test_full_result_guard_preserves_original_and_clears_only_invalid_comparison_score():
    source = {
        "engine_version": "heuristic-v6",
        "rules_version": "frozen",
        "manifest": {"artifact_identity": "original"},
        "relation_hypotheses": [],
        "clinical_observations": [
            {
                "intervention": {"text": "placebo"},
                "comparator": {"text": "placebo"},
                "ico_evidence_priority_score": 0.99,
                "evidence_quotes": [{"quote": "Original text."}],
            }
        ],
    }
    before = deepcopy(source)
    new = apply(source)
    assert source == before
    assert new["manifest"]["upstream_artifact_identity"] == "original"
    assert new["manifest"]["artifact_identity"] != "original"
    record = new["clinical_observations"][0]
    assert record["ico_evidence_priority_score"] is None
    assert record["original_ico_evidence_priority_score"] == 0.99
    assert record["evidence_quotes"] == before["clinical_observations"][0]["evidence_quotes"]


def test_unresolved_relation_does_not_leak_into_automatic_assertion_map():
    source = {
        "engine_version": "heuristic-v6",
        "rules_version": "frozen",
        "manifest": {"artifact_identity": "original"},
        "relation_hypotheses": [],
        "clinical_observations": [],
        "assertions": [
            {
                "subject": "drug",
                "object": "gene",
                "quote": "Original evidence.",
                "payload": {
                    "subject_mention": {"kind": "component", "normalized_id": "MESH:D1"},
                    "object_mention": {"kind": "gene", "normalized_id": None},
                },
            }
        ],
    }
    before = deepcopy(source)
    new = apply(source)
    assert new["assertions"] == []
    assert new["review_assertions"][0]["quote"] == "Original evidence."
    assert new["review_assertions"][0]["guard_reasons"] == ["gene_identity_unresolved"]
    assert source == before
    assert apply(new) == new
    stale = deepcopy(new)
    stale["manifest"]["decision_guard"]["source_hashes"]["decision_guard_v6.py"] = "changed"
    with pytest.raises(ValueError, match="re-extract"):
        apply(stale)
