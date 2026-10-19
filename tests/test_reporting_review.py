"""Claim/review boundaries are independently enforced, never generated approvals."""
import copy
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from nutrigenomics.reporting_review import (
    analysis_audit,
    audit_frozen_producer_sources,
    build_report,
    build_review_package,
    clinical_record_summary,
    digest,
    food_inventory_grain_audit,
    legacy_claim_ledger,
    prepare_review_records,
    read_science_records,
    validate_review_decisions,
)


def source():
    values = [
        ("S1", "study", {"id": "S1", "metadata": {"series": ["GSE1"], "primary_sources": [
            {"label": "Primary publication", "url": "https://example.org/paper"}]}}),
        ("E1", "exposure", {"id": "E1", "study_id": "S1", "amount": 800, "unit": "g"}),
        ("C1", "contrast", {"id": "C1", "study_id": "S1", "n_people": 7, "n_samples": 56}),
        ("R1", "analysis_run", {"id": "R1", "series_accession": "GSE1", "analysis_version": "raw-v1",
            "source_script_sha256": "a" * 64, "metadata": {"sample_count": 56, "independent_people": 7,
            "normalization": "TMM, voom", "design_formula": "~person + time", "multiple_testing": "BH within contrast"}}),
        ("END1", "clinical_endpoint_result", {"study_id": "S1", "endpoint": {"id": "LDL-C", "domain": "lipids"}, "value": None,
            "missing_reason": "numeric result not reported", "source": {"url": "https://example.org/paper#table2"}}),
        ("SOURCE", "bridge_source", {"id": "SOURCE"}),
    ]
    return {"science_records": [{"id": key, "kind": kind, "metadata_json": json.dumps(value)}
                               for key, kind, value in values]}


def contract(scope="public"):
    return {"schema_version": "nutriomics-release-contract-v1", "release_id": "actual-" + scope,
            "counts": {"node": 100, "edge": 200, "measurement": 80, "effect": 999,
                "composition_backed_foods": 50, "study_registry_records": 1,
                "science_kinds": {"study": 1, "exposure": 1, "contrast": 1, "analysis_run": 1,
                                  "clinical_endpoint_result": 1, "bridge_source": 1},
                "by_tier": {"predicted": 150, "observed_composition": 50}},
            "count_definitions": {"effect": "feature by contrast by version, not people"}}


def decision():
    row = prepare_review_records(source())[0]
    row.update(decision="accept", reviewer="실제 검수자", reviewer_type="human", reviewer_attestation=True,
               reviewed_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
               primary_paper_location="Methods, Table 2", reason="Original comparison checked")
    return row


def test_exact_sqlite_records_and_json_snapshot_produce_same_hash(tmp_path):
    path = tmp_path / "snapshot.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE science_record(id TEXT, kind TEXT, metadata_json TEXT)")
        for row in source()["science_records"]:
            db.execute("INSERT INTO science_record VALUES(?,?,?)", tuple(row[k] for k in ("id", "kind", "metadata_json")))
    before = path.read_bytes()
    assert read_science_records(path) == read_science_records(source())
    assert path.read_bytes() == before


def test_review_covers_final_clinical_records_without_fake_approvals(tmp_path):
    result = build_review_package(source(), tmp_path)
    assert result["records"] == 5 and result["expert_verified"] == 0
    assert result["by_kind"]["clinical_endpoint_result"] == 1
    rows = json.loads((tmp_path / "scientific-review.json").read_text(encoding="utf-8"))
    assert all(r["decision"] == "pending" and not r["reviewer_attestation"] for r in rows)
    assert all(r["record_sha256"] == digest(r["record"]) for r in rows)
    result = validate_review_decisions(source(), rows)
    assert result["expert_approved"] == 0 and not result["review_complete"]


@pytest.mark.parametrize("change", [
    {"reviewer_type": "ai"}, {"reviewer": "ChatGPT"}, {"reviewer_attestation": False},
    {"primary_paper_location": None}, {"reason": ""}, {"reviewed_at": "2026-10-19"},
    {"reviewed_at": "not-date"}, {"record_sha256": "0" * 64},
    {"decision": "correct", "proposed_correction": None},
])
def test_approval_guard_rejects_missing_identity_source_or_hash(change):
    row = decision(); row.update(change)
    with pytest.raises(ValueError): validate_review_decisions(source(), [row])


def test_review_decisions_are_hash_bound_and_only_count_submitted_actual_records():
    row = decision()
    result = validate_review_decisions(source(), [row])
    assert result["expert_approved"] == 1 and result["decisions"]["pending"] == 4
    changed = copy.deepcopy(source())
    parsed = json.loads(changed["science_records"][0]["metadata_json"]); parsed["title"] = "changed"
    changed["science_records"][0]["metadata_json"] = json.dumps(parsed)
    # R1 is first in normalized ordering; change its actual nested record instead.
    changed["science_records"][3]["metadata_json"] = json.dumps({"id": "R1", "new": True})
    with pytest.raises(ValueError): validate_review_decisions(changed, [row])
    with pytest.raises(ValueError): validate_review_decisions(source(), [row, row])


def test_previously_validated_human_decision_is_preserved_but_not_invented(tmp_path):
    human = decision()
    manifest = build_review_package(source(), tmp_path, decisions=[human])
    rows = json.loads((tmp_path / "scientific-review.json").read_text(encoding="utf-8"))
    assert manifest["expert_verified"] == 1
    assert sum(r["decision"] == "accept" for r in rows) == 1
    assert sum(r["decision"] == "pending" for r in rows) == 4


def test_metadata_audit_does_not_sum_people_or_assert_model_validity():
    result = analysis_audit(source())
    assert result["analysis_count"] == 1
    run = result["analyses"][0]
    assert run["n_people_model"] == 7 and run["n_assays_model"] == 56
    assert run["dependency_structure_declared"]
    assert "within_contrast_fdr_only" in {f["code"] for f in run["findings"]}
    assert result["expert_review_status"] == "pending" and not result["scientific_approval_generated"]


def test_fixed_regions_and_beta_uncertainty_remain_distinct():
    data = source()
    value = json.loads(data["science_records"][3]["metadata_json"])
    value["analysis_version"] = "methylation-regions-fixed1000bp-v1"
    value["metadata"].update(normalization="M-values limma, delta-beta", multiple_testing=None)
    data["science_records"][3]["metadata_json"] = json.dumps(value)
    row = analysis_audit(data)["analyses"][0]
    assert row["result_label"] == "fixed_1kb_region_tests_not_validated_dmrs"
    assert not row["beta_scale_uncertainty_declared"]
    assert "beta_uncertainty_scale_unconfirmed" in {f["code"] for f in row["findings"]}


def test_producer_audit_requires_exact_bytes_before_recording_method_locations(tmp_path):
    scripts = tmp_path / "scripts"; scripts.mkdir()
    path = scripts / "producer.R"
    path.write_text('design <- model.matrix(~subject_id + time)\nq_value <- p.adjust(p, method="BH")\n', encoding="utf-8")
    data = source(); value = json.loads(data["science_records"][3]["metadata_json"])
    value["source_script_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    data["science_records"][3]["metadata_json"] = json.dumps(value)
    report = audit_frozen_producer_sources(data, tmp_path)
    assert report["exact_source_matches"] == 1
    row = report["analyses"][0]
    assert row["method_source_locations"]["repeated_person_and_model"]["locations"][0]["line"] == 1
    path.write_text('design <- model.matrix(~time)\n', encoding="utf-8")
    report = audit_frozen_producer_sources(data, tmp_path)
    assert report["exact_source_matches"] == 0
    assert report["analyses"][0]["actual_source_sha256"] is None
    assert not report["analyses"][0]["method_source_locations"]["repeated_person_and_model"]["locations"]


def test_clinical_summary_preserves_omnibus_adjustment_and_iauc_units():
    from pathlib import Path
    config = json.loads((Path(__file__).resolve().parents[1] / "config/clinical_endpoints.v1.json").read_text(encoding="utf-8"))
    data = {"science_records": [{"id": r["id"], "kind": "clinical_endpoint_result", "record": r} for r in config["records"]]}
    result = clinical_record_summary(data)
    assert result["human_approved_records"] == 0
    assert result["unit_caveats"] and all(r["canonical_unit"] is None for r in result["unit_caveats"])
    scope = result["statistical_scope_examples"][0]["statistical_tests"]
    assert any(t["comparison_scope"].startswith("omnibus") and t["p_value"] == 0.03 for t in scope)
    assert any(t["comparison_scope"] == "T1 versus T2" and t["adjusted"] and t["p_value"] == 0.081 for t in scope)
    assert any(t["comparison_scope"] == "T1 versus T2" and not t["adjusted"] and t["p_value"] == 0.027 for t in scope)


def test_report_consumes_canonical_counts_and_separates_historical_tests(tmp_path):
    receipt = {"source_file": "past-tests.json", "source_sha256": "b" * 64,
               "reference_date": "2026-10-17", "label": "Historical fixed case test", "status": "passed_on_that_release"}
    result = build_report(internal_contract=contract("internal"), public_contract=contract(),
        science_source=source(), output=tmp_path / "report", workspace=tmp_path,
        validation_receipts=[receipt], generated_at="2026-10-19T00:00:00+00:00")
    assert not result["journal_submission_ready"]
    assert result["review"]["expert_approved"] == 0
    assert result["validation_receipts"][0]["reference_date"] == "2026-10-17"
    assert result["public_release"]["release_id"] == "actual-public"
    text = (tmp_path / "report/NutriOmics_성과및최종보완보고서.html").read_text(encoding="utf-8")
    assert "999" in text and "2026-10-17" in text and "window.print()" in text
    assert result["sealed_query_answers_accessed"] is False


def test_report_rejects_stale_contract_and_undated_receipt(tmp_path):
    kwargs = {"internal_contract": contract("internal"), "public_contract": contract(), "science_source": source(),
              "output": tmp_path, "workspace": tmp_path}
    bad = contract(); bad["counts"]["science_kinds"]["analysis_run"] = 17
    with pytest.raises(ValueError): build_report(**{**kwargs, "public_contract": bad})
    with pytest.raises(ValueError): build_report(**kwargs, validation_receipts=[{"status": "passed"}])
    stale = {**source(), "release_id": "old-public"}
    with pytest.raises(ValueError): build_report(**{**kwargs, "science_source": stale})


def test_report_rejects_tampered_validation_receipt_file(tmp_path):
    (tmp_path / "actual-test.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="receipt content"):
        build_report(internal_contract=contract("internal"), public_contract=contract(), science_source=source(),
            output=tmp_path / "report", workspace=tmp_path,
            validation_receipts=[{"source_file": "actual-test.json", "source_sha256": "c" * 64,
                                  "reference_date": "2026-10-19"}])


def test_historical_values_are_never_promoted_to_current_release_achievement(tmp_path):
    path = tmp_path / "final_2026/deliverables/전체검증_준비본_20261003_v2.json"
    path.parent.mkdir(parents=True)
    path.write_text('"55,379(2024.8)"\n"510,620(2024.8)"\n"AUC 0.88 is not validated"\n', encoding="utf-8")
    ledger = legacy_claim_ledger(tmp_path)
    assert all(not r["current_release_achievement"] for r in ledger)
    assert ledger[0]["source_matches"][0]["line"] == 1
    assert ledger[0]["source_type"].endswith("not_original_data_proof")


def test_food_inventory_grain_change_preserves_current_distinct_measurement_count(tmp_path):
    baseline = tmp_path / "outputs/publication/2026-10-17/extensions/CANDIDATE_INTEGRATION_COMPLETION.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"verified_at": "2026-10-17T00:00:00Z",
        "validated_food_records": {"internal": 5845, "public": 5794}}), encoding="utf-8")
    internal, public = contract("internal"), contract()
    internal["counts"].update(composition_backed_foods=5844, by_kind={"food": 5850})
    public["counts"].update(composition_backed_foods=5793, by_kind={"food": 5794})
    result = food_inventory_grain_audit(tmp_path, internal, public)
    assert result["scope"]["internal"]["prior_validated_food_records"] == 5845
    assert result["scope"]["internal"]["composition_record_linked_foods"] == 5844
    assert result["scope"]["internal"]["food_nodes_without_composition_records"] == 6
    assert result["scope"]["public"]["composition_record_linked_foods"] == 5793
    assert result["scope"]["public"]["food_nodes_without_composition_records"] == 1
    assert result["prior_source_sha256"] == hashlib.sha256(baseline.read_bytes()).hexdigest()


def test_html_cannot_execute_source_content(tmp_path):
    data = source(); value = json.loads(data["science_records"][0]["metadata_json"])
    value["title"] = "</script><script>alert(1)</script>"
    data["science_records"][0]["metadata_json"] = json.dumps(value)
    build_review_package(data, tmp_path)
    text = (tmp_path / "scientific-review.html").read_text(encoding="utf-8")
    assert "</script><script>alert(1)" not in text
    assert "\\u003c/script" in text


def test_review_ui_survives_new_session_and_rejects_stale_import(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for standalone review UI logic verification")
    build_review_package(source(), tmp_path)
    text = (tmp_path / "scientific-review.html").read_text(encoding="utf-8")
    script = re.search(r"<script>([\s\S]*)</script>", text).group(1)
    program = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const store=new Map();
function session(code){const els=new Map();const el=()=>({value:'',checked:false,children:[],textContent:'',replaceChildren(...c){this.children=c},append(c){this.children.push(c)}});const context={console,Date,Map,Set,JSON,Object,Number,Blob:globalThis.Blob,URL,setTimeout,alert:m=>{throw Error(m)},localStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)},document:{getElementById:id=>{if(!els.has(id))els.set(id,el());return els.get(id)},createElement:el}};vm.createContext(context);vm.runInContext(code,context);return context;}
const code=__SCRIPT__;
let one=session(code);
vm.runInContext(`$('reviewer').value='Named human reviewer';$('attest').checked=true;$('location').value='Table 2';$('reason').value='Source comparison inspected';decide('accept')`,one);
assert.equal(vm.runInContext('rows[0].decision',one),'accept');
assert.equal(store.size,1);
const two=session(code);
assert.equal(vm.runInContext('rows[0].decision',two),'accept');
assert.equal(vm.runInContext('rows[0].reviewer',two),'Named human reviewer');
assert.throws(()=>vm.runInContext(`applySaved([{...savedRows()[0],record_sha256:'changed'}])`,two));
assert.throws(()=>vm.runInContext(`applySaved([{...savedRows()[0],reviewer_type:'ai'}])`,two));
assert.equal(vm.runInContext('rows[0].decision',two),'accept');
'''.replace("__SCRIPT__", json.dumps(script))
    result = subprocess.run([node, "-e", program], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
