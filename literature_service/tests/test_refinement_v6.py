import json

import pytest

from nutri_literature.clinical_refinement_v6 import distinct_arms, MemoEncoder
from nutri_literature.refinement_v6 import (
    automatic_relation_eligible,
    literal_matches,
    read_training,
    selected_types,
    tokens,
)


def test_exact_role_alignment_preserves_unicode_and_dose():
    text = "α cells received DSG 1%; the comparator was placebo."
    sequence = tokens(text)
    matches = literal_matches(sequence, ["DSG 1%", "placebo", "DSG 2%"])
    surfaces = [text[sequence[a]["start"] : sequence[b - 1]["end"]] for a, b in matches]
    assert surfaces == ["DSG 1", "placebo"]
    assert "DSG 2" not in surfaces


def test_unresolved_candidates_remain_review_only():
    h = {
        "gene": {"normalized_id": None, "payload": {"automatic_selected": True}},
        "chemical": {"normalized_id": "MESH:D00001"},
    }
    assert automatic_relation_eligible(h) == (False, ["gene_identity_unresolved"])
    h["gene"]["payload"]["provided_native_ids"] = ["123"]
    assert automatic_relation_eligible(h, oracle=True)[0]
    assert not automatic_relation_eligible(h, oracle=False)[0]


def test_boundary_rejected_gene_is_not_an_automatic_relation():
    h = {
        "gene": {"normalized_id": "NCBIGene:123", "payload": {"automatic_selected": False}},
        "chemical": {"normalized_id": "MESH:D00001"},
    }
    assert not automatic_relation_eligible(h, require_selected_span=True)[0]
    assert automatic_relation_eligible(h, require_selected_span=False)[0]


def test_joint_type_competition_respects_none_and_threshold():
    assert selected_types({"NONE": 0.7, "Bind": 0.3}, 0.2) == []
    assert selected_types({"Association": 0.7, "Bind": 0.2, "NONE": 0.1}, 0.6) == ["Association"]
    assert selected_types({"Association": 0.7, "NONE": 0.3}, 0.8) == []
    assert selected_types({"Association|Bind": 0.9, "NONE": 0.1}, 0.8) == ["Association", "Bind"]


def test_same_arm_is_rejected_but_dose_comparison_survives():
    assert not distinct_arms("placebo", "the placebo group")
    assert not distinct_arms("alcohol", "alcohol")
    assert distinct_arms("drug 1 mg", "drug 2 mg")
    assert distinct_arms("DSG 1%", "placebo")


def test_memo_encoder_separates_context_entities_and_calls_exact_encoder():
    class Encoder:
        def __init__(self):
            self.calls = 0

        def encode(self, text, sentence, entities=()):
            self.calls += 1
            return (text, sentence, entities)

    base = Encoder()
    wrapper = MemoEncoder(base)
    sentence = {"start": 0, "end": 3, "text": "abc"}
    first = wrapper.encode("abc", sentence)
    assert wrapper.encode("abc", sentence) is first
    wrapper.encode("abc", sentence, ({"start": 0, "end": 1},))
    wrapper.encode("xyz", {**sentence, "text": "xyz"})
    assert base.calls == 3


def test_role_training_rejects_development_or_wrong_source(tmp_path):
    data = tmp_path / "data.json"
    audit = tmp_path / "audit.json"
    data.write_text(json.dumps({"split": "development", "documents": []}))
    audit.write_text(json.dumps({"passed": True, "protected_overlap": 0}))
    with pytest.raises(ValueError, match="audited official training"):
        read_training(data, audit)
