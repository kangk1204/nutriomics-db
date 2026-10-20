"""Hash-bound, source-backed AI proposals; never human scientific approvals.

This module has no database writer, network client, R runner or evaluation-gold reader.
It checks explicit evidence locators and reports the unassessed remainder.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

from nutrigenomics.reporting_review import digest, read_science_records

SCHEMA = "nutriomics-scientific-AI-precheck-v1"
ACTIONS = {"retain_with_scope_proposal", "correct_proposal", "unconfirmed_proposal"}
APPROVAL_FIELDS = {"expert_verified", "scientific_approval", "reviewer_attestation"}


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _text(value: str) -> str:
    return " ".join(value.split())


def _pointer(value: Any, pointer: list) -> Any:
    for key in pointer:
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def verify_proof(proof: dict, root: Path) -> dict:
    """Verify bytes and an explicit locator; no automatic semantic endorsement."""
    path = root / proof["path"]
    data = path.read_bytes()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Precheck evidence must be a bounded metadata/source file")
    if hashlib.sha256(data).hexdigest() != proof["sha256"]:
        raise ValueError(f"Evidence hash drift: {proof['id']}")
    if not proof.get("source_url") or not proof.get("location"):
        raise ValueError("Evidence requires source URL and exact location")
    text = data.decode("utf-8")
    locator = proof["locator"]
    if locator["kind"] == "json_pointer":
        selected = json.dumps(_pointer(json.loads(text), locator["path"]), ensure_ascii=False)
    elif locator["kind"] == "xml_id":
        doc = ET.fromstring(data)
        nodes = [node for node in doc.iter() if node.get("id") == locator["id"]]
        if len(nodes) != 1:
            raise ValueError("Missing or ambiguous XML location")
        selected = " ".join(nodes[0].itertext())
    elif locator["kind"] == "lines":
        lines = text.splitlines()
        begin, end = locator["start"], locator["end"]
        if begin < 1 or end < begin or end > len(lines):
            raise ValueError("Invalid source line bounds")
        selected = "\n".join(lines[begin - 1:end])
    elif locator["kind"] == "text":
        selected = text
    elif locator["kind"] == "rendered_text_json":
        selected = json.loads(text)
        if not isinstance(selected, str):
            raise ValueError("Rendered primary evidence must be a recorded text string")
        selected = re.sub(r"(?m)^L\d+(?:@P[\d-]+)?:\s*", "", selected)
    else:
        raise ValueError("Unknown source locator")
    selected = _text(selected)
    for needle in proof.get("contains", []):
        if _text(needle).casefold() not in selected.casefold():
            raise ValueError(f"Source anchor is absent: {proof['id']}: {needle}")
    if not proof.get("contains"):
        raise ValueError("A file hash alone is not a source-backed finding")
    parent_receipts = []
    for parent in proof.get("derived_from", []):
        raw = (root / parent["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != parent["sha256"]:
            raise ValueError("Derived evidence parent hash drift")
        parent_receipts.append({"path": parent["path"], "sha256": parent["sha256"]})
    return {"id": proof["id"], "proof_id": proof["id"], "path": proof["path"],
            "sha256": proof["sha256"], "locator": proof["locator"],
            "source_url": proof["source_url"], "location": proof["location"],
            "evidence_type": proof["evidence_type"], "locator_checked": True,
            "raw_source_redistribution": "not_granted_by_this_precheck",
            "selected_text_sha256": hashlib.sha256(selected.encode()).hexdigest(),
            "derived_parent_receipts": parent_receipts, "semantic_expert_approval": False}


def validate_proposals(records: list[dict], proposals: list[dict], proofs: dict) -> None:
    expected = {(r["record_kind"], str(r["record_id"])): r for r in records}
    if len(expected) != len(records):
        raise ValueError("Duplicate input record identity")
    seen = set()
    for row in proposals:
        key = (row["record_kind"], str(row["record_id"]))
        if key not in expected or key in seen:
            raise ValueError("Unknown or duplicate proposal identity")
        seen.add(key)
        original = expected[key]
        if (digest(original["record"]) != original["record_sha256"] or
                row["record_sha256"] != original["record_sha256"]):
            raise ValueError("Record hash drift")
        if (row.get("decision") != "pending" or row.get("expert_review_status") != "pending" or row.get("reviewer") is not None or
                row.get("reviewed_at") is not None or
                any(row.get(k) is not False for k in APPROVAL_FIELDS)):
            raise ValueError("AI precheck cannot create human approval or reviewer/date")
        if row.get("actor") != "AI_precheck" or row.get("proposed_action") not in ACTIONS:
            raise ValueError("Unknown AI proposal state")
        if not row.get("proof_ids") or not all(p in proofs for p in row["proof_ids"]):
            raise ValueError("Source-free proposal is forbidden")
        for finding in row["findings"]:
            if not finding.get("proof_ids") or not all(p in proofs for p in finding["proof_ids"]):
                raise ValueError("Source-free finding is forbidden")
    if seen != set(expected):
        raise ValueError("Every core record must map to one proposal")


_NUMBER = r"[+-]?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?"
_RANGE = re.compile(rf"({_NUMBER})\s*\(\s*({_NUMBER})\s*[-–−]\s*({_NUMBER})\s*\)")


def _triplet(cell: str, sd: bool = False) -> list[float]:
    if sd:
        values = re.findall(_NUMBER, cell)
        if len(values) != 2:
            raise ValueError("Expected source mean (SD) cell")
        return list(map(float, values))
    match = _RANGE.fullmatch(_text(cell))
    if match is None:
        raise ValueError("Expected source median (range/IQR) cell")
    return list(map(float, match.groups()))


def check_clinical_record(record: dict, recipe: dict, root: Path) -> dict:
    """Compare exact source table cells and P-label order; never derive an effect."""
    if recipe["format"] == "xml_table":
        doc = ET.parse(root / recipe["path"]).getroot()
        table = next(n for n in doc.iter() if n.get("id") == recipe["table_id"])
        rows = [[_text(" ".join(c.itertext())) for c in row] for row in table.findall(".//tr")]
        matched = [r for r in rows if r and r[0] == recipe["row_label"]]
        if len(matched) != 1:
            raise ValueError("Clinical source row missing or ambiguous")
        source_row = matched[0]
        cells = [source_row[i] for i in recipe["observation_columns"]]
        triples = [_triplet(c, recipe["dispersion"] == "SD") for c in cells]
        p_labels = [source_row[i] for i in recipe["p_columns"]]
    elif recipe["format"] == "pdf_text_row":
        text = _text((root / recipe["path"]).read_text(encoding="utf-8"))
        section = text.split(recipe["row_start"], 1)[1].split(recipe["row_end"], 1)[0]
        matches = list(_RANGE.finditer(section))
        if len(matches) != 4:
            raise ValueError("Expected four order-stratified iAUC cells")
        triples = [list(map(float, m.groups())) for m in matches]
        p_labels = re.findall(_NUMBER, section[matches[-1].end():])
        if len(p_labels) != 2:
            raise ValueError("Expected separate carry-over and intervention P labels")
        if section[:matches[0].start()].strip() != "6" or section[matches[1].end():matches[2].start()].strip() != "7":
            raise ValueError("Crossover order-group sizes differ")
    else:
        raise ValueError("Unknown clinical source recipe")
    expected = []
    for obs in record["observations"]:
        dispersion = obs["dispersion"]
        expected.append([obs["value"], dispersion["value"]] if dispersion["kind"] == "SD"
                        else [obs["value"], dispersion["lower"], dispersion["upper"]])
    if expected != triples:
        raise ValueError("Clinical numeric cell or observation order differs from source")
    labels = [t["reported_label"] for t in record["statistical_tests"]]
    normalize = lambda s: _text(s).replace("–", "-").replace("−", "-")
    if len(labels) != len(p_labels):
        raise ValueError("Clinical test-scope count differs from source")
    for index, (a, b) in enumerate(zip(labels, p_labels, strict=True)):
        if (a == "not_reported" and normalize(b) == "-" and
                record["statistical_tests"][index].get("p_value") is None):
            continue
        if normalize(a) != normalize(b):
            try:
                if float(a) == float(b):
                    continue
            except ValueError:
                pass
            raise ValueError("Clinical P label or test-column order differs from source")
        numeric_p = record["statistical_tests"][index].get("p_value")
        try:
            source_p = float(normalize(b))
        except ValueError:
            source_p = None
        if numeric_p != source_p:
            raise ValueError("Clinical typed P value differs from its original source cell")
    if any(record.get(k) is not None for k in ("clinical_effect_estimate", "standard_error", "confidence_interval")):
        raise ValueError("A source summary cannot become a derived scalar clinical effect")
    if record.get("genomic_mediation_demonstrated") is True or record.get("individual_prediction_available") is True:
        raise ValueError("Clinical summaries do not establish genomic mediation or individual prediction")
    return {"check": "exact_table_cells_and_reported_P_column_order", "status": "source_values_match",
            "observations_checked": len(triples), "test_labels_checked": len(p_labels),
            "unit_and_test_semantic_approval": False, "proof_ids": recipe["proof_ids"]}


def build_precheck(records: list[dict], facts: list[dict], catalog: dict, root: Path) -> dict:
    """Create complete, source-backed proposed review without promoting any record."""
    if len(records) != catalog["expected_records"] or len(facts) != catalog["expected_facts"]:
        raise ValueError("Frozen review or fact count changed")
    if len({p["id"] for p in catalog["proofs"]}) != len(catalog["proofs"]):
        raise ValueError("Duplicate evidence identity")
    verified = {p["id"]: verify_proof(p, root) for p in catalog["proofs"]}
    studies = {p["study_id"]: p for p in catalog["study_packets"]}
    methods = {p["analysis_version"]: p for p in catalog["method_packets"]}
    series = {s: p["study_id"] for p in studies.values() for s in p["series"]}
    by_key = {(r["record_kind"], str(r["record_id"])): r for r in records}
    if len(by_key) != len(records):
        raise ValueError("Duplicate frozen record")
    proposed = []
    for row in records:
        if digest(row["record"]) != row["record_sha256"]:
            raise ValueError("Record content drift")
        value = row["record"]
        sid = row.get("study_registry_id") or value.get("study_id") or series.get(value.get("series_accession"))
        if row["record_kind"] == "study":
            sid = str(value["id"])
        if sid not in studies:
            raise ValueError("Core record lacks an explicit study decision packet")
        packet = studies[sid]
        analysis_version = value.get("analysis_version")
        if not analysis_version and value.get("contrast_id"):
            linked = by_key.get(("contrast", str(value["contrast_id"])), {})
            analysis_version = linked.get("record", {}).get("analysis_version")
        method = methods.get(analysis_version)
        findings = copy.deepcopy(packet["findings"])
        if method and row["record_kind"] in {"analysis_run", "contrast", "assertion"}:
            findings.extend(copy.deepcopy(method["findings"]))
        clinical = None
        if row["record_kind"] == "clinical_endpoint_result":
            recipe = catalog["clinical_recipes"].get(row["record_id"])
            if recipe is None:
                raise ValueError("Clinical record lacks an exact source recipe")
            clinical = check_clinical_record(value, recipe, root)
            findings.append(clinical)
        proof_ids = sorted({p for f in findings for p in f["proof_ids"]})
        proposed.append({"record_kind": row["record_kind"], "record_id": row["record_id"],
            "record_sha256": row["record_sha256"], "study_registry_id": sid,
            "actor": "AI_precheck", "decision": "pending", "reviewer": None,
            "reviewed_at": None, "expert_review_status": "pending", "expert_verified": False, "scientific_approval": False,
            "reviewer_attestation": False, "proposed_action": "retain_with_scope_proposal",
            "proposal_scope": "Only explicit findings are checked; unlisted field semantics remain unassessed",
            "proof_ids": proof_ids, "source_locations": [verified[p] for p in proof_ids],
            "decision_bundle_ids": [packet["bundle_id"]] + ([method["bundle_id"]] if method else []),
            "findings": findings, "clinical_exact_cell_check": clinical,
            "unassessed": ["human semantic validity", "causal/health benefit interpretation",
                           "individual response prediction", "unlisted fields"]})
    validate_proposals(records, proposed, verified)
    by_proposal = {(r["record_kind"], r["record_id"]): r for r in proposed}
    for rule in catalog.get("record_proposals", []):
        key = (rule["record_kind"], rule["record_id"])
        if key not in by_key or rule["record_sha256"] != by_key[key]["record_sha256"]:
            raise ValueError("Record-specific proposal hash drift")
        if rule["proposed_action"] not in ACTIONS:
            raise ValueError("Unknown record-specific proposal action")
        proposal = by_proposal[key]
        proposal["proposed_action"] = rule["proposed_action"]
        proposal["findings"].extend(copy.deepcopy(rule["findings"]))
        proposal["proof_ids"] = sorted({p for f in proposal["findings"] for p in f["proof_ids"]})
        proposal["source_locations"] = [verified[p] for p in proposal["proof_ids"]]
    fact_proposals = []
    for fact in facts:
        key = (fact["record_kind"], str(fact["record_id"]))
        if key not in by_key or fact["record_sha256"] != by_key[key]["record_sha256"]:
            raise ValueError("Fact candidate refers to a changed or absent record")
        value = by_key[key]["record"]
        try:
            observed = _pointer(value, fact["field"].split("."))
        except KeyError:
            observed = None
        if observed != fact["candidate_value"]:
            raise ValueError("Fact candidate value drift")
        parent = by_proposal[key]
        method_fact = fact["record_kind"] == "analysis_run"
        internal = fact["field"] in {"id", "study_id", "analysis_version", "exposure_ids", "label", "status", "layer", "inference", "limitations", "assertion", "endpoint", "organism", "source_location"}
        status = ("provenance_or_registry_fact_not_primary_paper_gold" if internal else
                  "producer_or_analysis_audit_fact_not_primary_paper_gold" if method_fact else
                  "clinical_numeric_source_cells_checked_semantics_pending" if fact["field"] in {"observations", "statistical_tests"} else
                  "mapped_to_source_packet_field_semantics_unassessed")
        fact_proposals.append({**{k: fact[k] for k in ("fact_id", "record_kind", "record_id", "record_sha256", "field", "candidate_value")},
            "actor": "AI_precheck", "status": status, "expert_verified": False,
            "review_status": "pending", "primary_paper_location": None,
            "proposed_primary_paper_locations": [p["location"] for p in parent["source_locations"]
                                                   if p["evidence_type"].startswith("primary")],
            "source_locations": parent["source_locations"], "proof_ids": parent["proof_ids"],
            "not_gold_answer": True, "field_semantics_human_review_required": True})
    patches = []
    for patch in catalog.get("mechanical_patches", []):
        if patch.get("classification") != "mechanical_source_link":
            raise ValueError("This precheck only proposes mechanically verified source-link patches")
        key = (patch["record_kind"], patch["record_id"])
        row = by_key[key]
        if row["record_sha256"] != patch["old_record_sha256"]:
            raise ValueError("Correction refers to stale record")
        changed = copy.deepcopy(row["record"])
        for edit in patch["changes"]:
            if (edit["operation"] != "replace" or len(edit["path"]) != 4 or
                    edit["path"][:2] != ["metadata", "primary_sources"] or
                    edit["path"][-1] != "url"):
                raise ValueError("Only an existing primary-source URL may be mechanically corrected")
            parent = _pointer(changed, edit["path"][:-1])
            key_part = int(edit["path"][-1]) if isinstance(parent, list) else edit["path"][-1]
            if parent[key_part] != edit["old_value"]:
                raise ValueError("Correction old value mismatch")
            parent[key_part] = edit["new_value"]
        if not patch["proof_ids"] or not all(p in verified for p in patch["proof_ids"]):
            raise ValueError("Source-free correction is forbidden")
        patches.append({**patch, "new_record_sha256": digest(changed), "proposed_record": changed,
                        "expert_review_status": "pending", "scientific_approval": False})
        by_proposal[key]["proposed_action"] = "correct_proposal"
    method_notes = []
    for patch in catalog.get("method_note_proposals", []):
        key = (patch["record_kind"], patch["record_id"])
        row = by_key[key]
        if row["record_sha256"] != patch["old_record_sha256"]:
            raise ValueError("Source-method annotation record hash drift")
        if not patch["proof_ids"] or not all(p in verified for p in patch["proof_ids"]):
            raise ValueError("Source-free method annotation forbidden")
        notes = copy.deepcopy(patch["source_method_notes"])
        for note in notes:
            if (note.get("actor") != "AI_precheck" or note.get("expert_review_status") != "pending" or
                    note.get("scientific_approval") is not False or not note.get("source_fact") or
                    not note.get("source_locator") or not note.get("source_file_sha256")):
                raise ValueError("Method note must remain explicit source-backed AI annotation")
            if (note["source_file_sha256"] not in {verified[p]["sha256"] for p in patch["proof_ids"]} or
                    note.get("source_url") not in {verified[p]["source_url"] for p in patch["proof_ids"]}):
                raise ValueError("Method note hash/URL must bind its exact source proof")
        changed = copy.deepcopy(row["record"])
        original_metadata = copy.deepcopy(changed.get("metadata", {}))
        if "source_method_notes" in original_metadata:
            raise ValueError("Do not overwrite an existing source-method annotation")
        changed["metadata"] = {**original_metadata, "source_method_notes": notes}
        method_notes.append({"classification": "additive_source_method_annotation",
            "record_kind": key[0], "record_id": key[1], "old_record_sha256": row["record_sha256"],
            "new_record_sha256": digest(changed), "proposed_record": changed,
            "changes": [{"operation": "replace", "path": ["metadata"], "old_value": original_metadata,
                         "new_value": changed["metadata"]}], "proof_ids": patch["proof_ids"],
            "reason": patch["reason"], "expert_review_status": "pending", "scientific_approval": False})
    bundles = []
    for packet in [*catalog["study_packets"], *catalog["method_packets"]]:
        if packet["recommended_option"] not in packet["options"]:
            raise ValueError("Human task recommendation must be one explicit option")
        linked = [{"record_kind": p["record_kind"], "record_id": p["record_id"], "record_sha256": p["record_sha256"]}
                  for p in proposed if packet["bundle_id"] in p["decision_bundle_ids"]]
        source_ids = sorted({p for f in packet["findings"] for p in f["proof_ids"]})
        bundles.append({**copy.deepcopy(packet), "task_id": packet["bundle_id"],
                        "title_ko": packet["question_ko"], "source_references": [verified[p] for p in source_ids],
                        "proposed_handling": packet["recommended_option"],
                        "recommended_choice": packet["recommended_option"],
                        "effects_on_publication": packet["publication_scope_impact"],
                        "record_mapping": linked, "record_ids_and_hashes": linked,
                        "decision": "pending", "reviewer": None, "reviewed_at": None,
                        "expert_verified": False, "actor": "AI_precheck"})
    validate_proposals(records, proposed, verified)
    return {"schema_version": SCHEMA, "actor": "AI_precheck", "scientific_approval": False,
            "expert_verified": False, "records": proposed, "facts": fact_proposals,
            "human_decision_bundles": bundles, "verified_proofs": list(verified.values()),
            "corrections": {"schema_version": "nutriomics-scientific-correction-proposals-v1",
                            "automatic_expert_approval": False, "patches": patches},
            "method_annotations": {"schema_version": "nutriomics-scientific-correction-proposals-v1",
                                   "automatic_expert_approval": False, "patches": method_notes},
            "summary": {"records": len(proposed), "facts": len(fact_proposals),
                        "record_mapping_coverage": 1.0, "coverage_definition": "identity-to-packet mapping, not scientific accuracy", "study_bundles": len(studies),
                        "method_bundles": len(methods), "clinical_exact_cell_records": sum(p["clinical_exact_cell_check"] is not None for p in proposed),
                        "by_kind": dict(Counter(p["record_kind"] for p in proposed)),
                        "fact_statuses": dict(Counter(p["status"] for p in fact_proposals)),
                        "human_approvals_generated": 0, "biological_accuracy_measured": False,
                        "sealed_query_answers_accessed": False}}


def rebind_candidate_precheck(result: dict, authoritative_science: Any) -> dict:
    """Bind AI checks to exact candidate bytes; transfer no human decision."""
    candidate_rows = read_science_records(authoritative_science)
    candidate = {(r["record_kind"], r["record_id"]): r for r in candidate_rows}
    original = {(r["record_kind"], r["record_id"]): r for r in result["records"]}
    patches = result["corrections"]["patches"] + result["method_annotations"]["patches"]
    by_patch = {(p["record_kind"], p["record_id"]): p for p in patches}
    if len(by_patch) != len(patches):
        raise ValueError("Duplicate correction lineage")
    new_hashes, changed = {}, []
    for key, row in original.items():
        if (row["decision"] != "pending" or row.get("reviewer") is not None or
                row.get("reviewed_at") is not None or any(row.get(k) is not False for k in APPROVAL_FIELDS)):
            raise ValueError("Human decisions cannot be transplanted across record hashes")
        if key not in candidate:
            raise ValueError("Candidate lacks a required review record")
        target = candidate[key]
        actual = digest(target["record"])
        patch = by_patch.get(key)
        if patch:
            if (patch["old_record_sha256"] != row["record_sha256"] or
                    digest(patch["proposed_record"]) != patch["new_record_sha256"] or
                    actual != patch["new_record_sha256"] or
                    target["record"] != patch["proposed_record"]):
                raise ValueError("Candidate correction payload/hash lineage mismatch")
            changed.append({"record_kind": key[0], "record_id": key[1],
                            "old_record_sha256": row["record_sha256"], "new_record_sha256": actual,
                            "classification": patch["classification"]})
        elif actual != row["record_sha256"]:
            raise ValueError("Unexplained candidate record hash drift")
        new_hashes[key] = actual
    if not set(by_patch).issubset(original):
        raise ValueError("Correction lineage contains a non-review record")
    rebound = copy.deepcopy(result)
    for row in rebound["records"]:
        key = (row["record_kind"], row["record_id"])
        row["frozen_record_sha256"] = original[key]["record_sha256"]
        row["record_sha256"] = new_hashes[key]
        row["binding"] = "exact_authoritative_candidate_payload"
        row["mechanical_annotation_or_link_applied"] = key in by_patch
        if key in by_patch:
            row["original_proposed_action"] = row["proposed_action"]
            row["proposed_action"] = "retain_with_scope_proposal"
    for fact in rebound["facts"]:
        key = (fact["record_kind"], fact["record_id"])
        try:
            current_value = _pointer(candidate[key]["record"], fact["field"].split("."))
        except KeyError:
            current_value = None
        if current_value != fact["candidate_value"]:
            raise ValueError("A corrected candidate fact needs explicit new source checking")
        fact["frozen_record_sha256"] = original[key]["record_sha256"]
        fact["record_sha256"] = new_hashes[key]
    for bundle in rebound["human_decision_bundles"]:
        for field in ("record_mapping", "record_ids_and_hashes"):
            for row in bundle[field]:
                key = (row["record_kind"], row["record_id"])
                row["frozen_record_sha256"] = original[key]["record_sha256"]
                row["record_sha256"] = new_hashes[key]
        bundle["review_binding"] = "exact_authoritative_candidate_payload; no human decision transferred"
        bundle["decision"], bundle["reviewer"], bundle["reviewed_at"] = "pending", None, None
        bundle["expert_verified"] = False
    rebound["candidate_binding"] = {"status": "exact_payloads_checked", "changed_records": changed,
                                    "unchanged_review_records": len(original) - len(changed),
                                    "authoritative_science_records": len(candidate_rows),
                                    "human_decisions_transferred": 0}
    rebound["summary"]["candidate_binding"] = rebound["candidate_binding"]
    return rebound


def write_precheck(result: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    values = {"proposed-records.json": result["records"], "proposed-facts.json": result["facts"],
              "human-decisions.json": result["human_decision_bundles"],
              "verified-proof-locations.json": result["verified_proofs"],
              "correction-patch-proposals.json": result["corrections"],
              "additive-method-note-proposals.json": result["method_annotations"],
              "publication-correction-bundle.json": {
                  "schema_version": "nutriomics-scientific-correction-proposals-v1",
                  "automatic_expert_approval": False,
                  "patches": result["corrections"]["patches"] + result["method_annotations"]["patches"]},
              "summary.json": {"schema_version": SCHEMA, **result["summary"], "scientific_approval": False}}
    if "candidate_binding" in result:
        values["candidate-binding-ledger.json"] = result["candidate_binding"]
    for name, value in values.items():
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    with (output / "HUMAN_DECISIONS_KO.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fields = ["task_id", "title_ko", "recommended_choice", "options", "effects_on_publication", "proof_ids", "mapped_records", "decision", "reviewer", "reviewed_at"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in result["human_decision_bundles"]:
            writer.writerow({**{k: row.get(k) for k in fields},
                "options": json.dumps(row["options"], ensure_ascii=False),
                "proof_ids": ";".join(sorted({p for f in row["findings"] for p in f["proof_ids"]})),
                "mapped_records": len(row["record_mapping"])})
    text = ["# 교수님 판단이 필요한 과학적 쟁점", "", "이 문서는 AI 사전점검 제안입니다. 사람 승인·검수자·검수일을 생성하지 않았습니다.",
            f"{len(result['records'])}개 기록을 연구·분석 묶음에 대응시켰습니다. 묶음 승인은 아직 기록별 판정을 대신하지 않습니다.", ""]
    for row in result["human_decision_bundles"]:
        text += [f"## {row['bundle_id']}", "", row["question_ko"], "", f"추천안: {row['recommended_option']}", "",
                 "선택지: " + " / ".join(row["options"]), "", "수록 영향: " + row["publication_scope_impact"], ""]
        for finding in row["findings"]:
            text += ["- " + finding["statement_ko"] + " (근거: " + ", ".join(finding["proof_ids"]) + ")"]
        text += ["", f"대응 기록: {len(row['record_mapping'])}개. 원문 위치·URL·해시는 verified-proof-locations.json에 있습니다.", ""]
    (output / "HUMAN_DECISIONS_KO.md").write_text("\n".join(text), encoding="utf-8")
