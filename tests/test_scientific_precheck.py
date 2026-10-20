"""AI source checks cannot manufacture human review, gold answers or scalar effects."""
import copy
import hashlib
import json

import pytest

from nutrigenomics.reporting_review import digest
from nutrigenomics.scientific_precheck import (
    build_precheck,
    check_clinical_record,
    rebind_candidate_precheck,
    validate_proposals,
    write_precheck,
)


@pytest.fixture
def inputs(tmp_path):
    path = tmp_path / "primary.txt"
    path.write_text("Methods: seven participants, randomized crossover.\n", encoding="utf-8")
    proof = {"id": "P1", "path": "primary.txt", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
             "source_url": "https://primary.example/article", "location": "Methods, paragraph 1",
             "evidence_type": "primary_paper", "locator": {"kind": "lines", "start": 1, "end": 1},
             "contains": ["seven participants"]}
    record = {"id": "S1", "label": "curator label", "metadata": {"primary_sources": [{"url": "https://geo.example"}]},
              "publication_review_status": "pending"}
    rows = [{"record_kind": "study", "record_id": "S1", "record_sha256": digest(record),
             "record": record, "study_registry_id": "S1"}]
    facts = [{"fact_id": "S1:label", "record_kind": "study", "record_id": "S1",
              "record_sha256": digest(record), "field": "label", "candidate_value": "curator label"}]
    packet = {"study_id": "S1", "series": ["GSE1"], "bundle_id": "STUDY:S1",
              "question_ko": "탐색용으로 수록할까요?", "recommended_option": "한계 명시 후 수록",
              "options": ["한계 명시 후 수록", "보류"], "publication_scope_impact": "해당 연구",
              "findings": [{"statement_ko": "교차시험 원문 위치 확인", "proof_ids": ["P1"]}]}
    catalog = {"expected_records": 1, "expected_facts": 1, "proofs": [proof],
               "study_packets": [packet], "method_packets": [], "clinical_recipes": {}}
    return rows, facts, catalog, tmp_path


def test_complete_mapping_is_not_human_approval_or_fact_gold(inputs, tmp_path):
    records, facts, catalog, root = inputs
    before = copy.deepcopy(records)
    result = build_precheck(records, facts, catalog, root)
    assert records == before
    proposal = result["records"][0]
    assert proposal["decision"] == "pending" and proposal["reviewer"] is None
    assert proposal["reviewed_at"] is None and proposal["expert_verified"] is False
    assert result["summary"]["human_approvals_generated"] == 0
    assert result["facts"][0]["status"] == "provenance_or_registry_fact_not_primary_paper_gold"
    assert result["facts"][0]["not_gold_answer"] is True
    write_precheck(result, tmp_path / "result")
    assert (tmp_path / "result/HUMAN_DECISIONS_KO.csv").is_file()


@pytest.mark.parametrize("field,value", [("decision", "accept"), ("reviewer", "Professor"),
    ("reviewed_at", "2026-10-20"), ("expert_verified", True), ("scientific_approval", True),
    ("reviewer_attestation", True), ("expert_review_status", "expert_verified")])
def test_AI_output_cannot_carry_approval(inputs, field, value):
    records, facts, catalog, root = inputs
    result = build_precheck(records, facts, catalog, root)
    result["records"][0][field] = value
    proofs = {p["proof_id"]: p for p in result["verified_proofs"]}
    with pytest.raises(ValueError, match="approval"):
        validate_proposals(records, result["records"], proofs)


def test_record_and_fact_hash_drift_fail_closed(inputs):
    records, facts, catalog, root = inputs
    changed = copy.deepcopy(records); changed[0]["record"]["label"] = "new"
    with pytest.raises(ValueError, match="drift"):
        build_precheck(changed, facts, catalog, root)
    facts[0]["record_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="changed or absent"):
        build_precheck(records, facts, catalog, root)


def test_no_file_hash_or_packet_alone_can_be_a_source_free_judgment(inputs):
    records, facts, catalog, root = inputs
    catalog["proofs"][0]["contains"] = []
    with pytest.raises(ValueError, match="hash alone"):
        build_precheck(records, facts, catalog, root)
    catalog["proofs"][0]["contains"] = ["seven"]
    catalog["study_packets"][0]["findings"][0]["proof_ids"] = []
    with pytest.raises(ValueError, match="Source-free"):
        build_precheck(records, facts, catalog, root)


def test_source_anchor_and_byte_drift_fail_closed(inputs):
    records, facts, catalog, root = inputs
    catalog["proofs"][0]["contains"] = ["eighty participants"]
    with pytest.raises(ValueError, match="anchor is absent"):
        build_precheck(records, facts, catalog, root)
    (root / "primary.txt").write_text("changed")
    with pytest.raises(ValueError, match="Evidence hash drift"):
        build_precheck(records, facts, catalog, root)


def test_mechanical_patch_is_only_URL_and_never_approval(inputs):
    records, facts, catalog, root = inputs
    patch = {"record_kind": "study", "record_id": "S1", "old_record_sha256": records[0]["record_sha256"],
             "classification": "mechanical_source_link", "proof_ids": ["P1"], "reason": "official source link",
             "changes": [{"operation": "replace", "path": ["metadata", "primary_sources", 0, "url"],
                          "old_value": "https://geo.example", "new_value": "https://primary.example/article"}]}
    catalog["mechanical_patches"] = [patch]
    result = build_precheck(records, facts, catalog, root)
    corrected = result["corrections"]["patches"][0]
    assert corrected["new_record_sha256"] == digest(corrected["proposed_record"])
    assert corrected["proposed_record"]["publication_review_status"] == "pending"
    assert records[0]["record"]["metadata"]["primary_sources"][0]["url"] == "https://geo.example"
    patch["changes"][0]["path"] = ["publication_review_status"]
    with pytest.raises(ValueError, match="Only an existing primary-source URL"):
        build_precheck(records, facts, catalog, root)


def test_clinical_exact_cells_P_order_and_null_effect_are_protected(tmp_path):
    xml = '<article><table-wrap id="T1"><table><tr><td>BMI</td><td>30.63 (22.25–35.69)</td><td>0.033</td><td>0.226</td></tr></table></table-wrap></article>'
    (tmp_path / "paper.xml").write_text(xml, encoding="utf-8")
    recipe = {"format": "xml_table", "path": "paper.xml", "table_id": "T1", "row_label": "BMI",
              "observation_columns": [1], "p_columns": [2, 3], "dispersion": "range", "proof_ids": ["P"]}
    record = {"observations": [{"value": 30.63, "dispersion": {"kind": "range", "lower": 22.25, "upper": 35.69}}],
              "statistical_tests": [{"reported_label": "0.033", "p_value": 0.033}, {"reported_label": "0.226", "p_value": 0.226}],
              "clinical_effect_estimate": None, "standard_error": None, "confidence_interval": None}
    assert check_clinical_record(record, recipe, tmp_path)["status"] == "source_values_match"
    bad = copy.deepcopy(record); bad["statistical_tests"].reverse()
    with pytest.raises(ValueError, match="P label"):
        check_clinical_record(bad, recipe, tmp_path)
    bad = copy.deepcopy(record); bad["standard_error"] = 0
    with pytest.raises(ValueError, match="scalar clinical effect"):
        check_clinical_record(bad, recipe, tmp_path)
    bad = copy.deepcopy(record); bad["statistical_tests"][0]["p_value"] = 0
    with pytest.raises(ValueError, match="typed P value"):
        check_clinical_record(bad, recipe, tmp_path)


def test_additive_source_note_preserves_every_original_field_and_approval_state(inputs):
    records, facts, catalog, root = inputs
    note = {"actor": "AI_precheck", "expert_review_status": "pending", "scientific_approval": False,
            "source_fact": "Baseline and follow-up extraction methods differ", "source_locator": "Methods22",
            "source_file_sha256": catalog["proofs"][0]["sha256"], "source_url": "https://primary.example/article"}
    patch = {"record_kind": "study", "record_id": "S1", "old_record_sha256": records[0]["record_sha256"],
             "source_method_notes": [note], "proof_ids": ["P1"], "reason": "Additional method context, no invalidation"}
    catalog["method_note_proposals"] = [patch]
    result = build_precheck(records, facts, catalog, root)
    corrected = result["method_annotations"]["patches"][0]["proposed_record"]
    assert corrected["publication_review_status"] == "pending"
    assert corrected["metadata"]["primary_sources"] == records[0]["record"]["metadata"]["primary_sources"]
    assert set(corrected["metadata"]) - set(records[0]["record"]["metadata"]) == {"source_method_notes"}
    note["scientific_approval"] = True
    with pytest.raises(ValueError, match="explicit source-backed AI"):
        build_precheck(records, facts, catalog, root)


def test_written_proposals_are_JSON_and_do_not_create_expert_fields(inputs, tmp_path):
    result = build_precheck(*inputs)
    write_precheck(result, tmp_path / "out")
    saved = json.loads((tmp_path / "out/proposed-records.json").read_text(encoding="utf-8"))
    assert saved[0]["scientific_approval"] is False


def test_candidate_rebinding_preserves_frozen_lineage_without_human_approval(inputs):
    records, facts, catalog, root = inputs
    catalog["mechanical_patches"] = [{"record_kind": "study", "record_id": "S1",
        "old_record_sha256": records[0]["record_sha256"], "classification": "mechanical_source_link",
        "proof_ids": ["P1"], "reason": "Correct the original-source link",
        "changes": [{"operation": "replace", "path": ["metadata", "primary_sources", 0, "url"],
                     "old_value": "https://geo.example", "new_value": "https://primary.example/article"}]}]
    result = build_precheck(records, facts, catalog, root)
    patch = result["corrections"]["patches"][0]
    science = [{"kind": "study", "id": "S1", "record": patch["proposed_record"]},
               {"kind": "bridge_source", "id": "OTHER", "record": {"id": "OTHER"}}]
    rebound = rebind_candidate_precheck(result, science)
    assert rebound["records"][0]["record_sha256"] == patch["new_record_sha256"]
    assert rebound["records"][0]["frozen_record_sha256"] == records[0]["record_sha256"]
    assert rebound["facts"][0]["record_sha256"] == patch["new_record_sha256"]
    assert rebound["human_decision_bundles"][0]["record_mapping"][0]["record_sha256"] == patch["new_record_sha256"]
    assert rebound["candidate_binding"]["human_decisions_transferred"] == 0
    assert result["records"][0]["record_sha256"] == records[0]["record_sha256"]
    assert all(r["decision"] == "pending" and r["reviewer"] is None for r in rebound["records"])


def test_unexplained_candidate_drift_or_old_human_decision_cannot_be_rebound(inputs):
    records, facts, catalog, root = inputs
    result = build_precheck(records, facts, catalog, root)
    science = [{"kind": "study", "id": "S1", "record": copy.deepcopy(records[0]["record"])}]
    science[0]["record"]["label"] = "unexplained"
    with pytest.raises(ValueError, match="Unexplained candidate"):
        rebind_candidate_precheck(result, science)
    science[0]["record"] = records[0]["record"]
    result["records"][0]["decision"] = "accept"
    with pytest.raises(ValueError, match="cannot be transplanted"):
        rebind_candidate_precheck(result, science)
