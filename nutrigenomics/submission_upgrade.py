"""Source-bound upgrade proposals, without human approvals or sealed-query access."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

from nutrigenomics.reporting_review import (
    REVIEW_KINDS,
    digest,
    prepare_review_records,
    read_science_records,
)


def _bound_packet(packet: dict, row: dict) -> None:
    matches = [r for r in packet.get("record_mapping", [])
               if (r.get("record_kind"), str(r.get("record_id"))) == (row["record_kind"], row["record_id"])]
    if len(matches) != 1 or matches[0].get("record_sha256") != row["record_sha256"]:
        raise ValueError("Source packet refers to stale or absent record")


def checked_reference(reference: dict, workspace: Path) -> dict:
    """Verify the captured source, not its scientific or legal validity."""
    root = Path(workspace).resolve()
    if not re.fullmatch(r"[0-9a-f]{64}", reference.get("sha256", "")):
        raise ValueError("Source proof requires a valid SHA256")
    path = (root / reference["path"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Source proof outside workspace")
    actual = (hashlib.sha256(path.read_bytes()).hexdigest()
              if path.is_file() and path.stat().st_size <= 8 * 1024 * 1024 else None)
    resolution = "declared_capture"
    if actual != reference["sha256"]:
        # A working producer may evolve. Only an explicitly hash-addressed,
        # preserved original can stand in for its original captured bytes.
        frozen = root / "source_snapshots/review_evidence" / reference["sha256"] / path.name
        if (frozen.resolve().is_relative_to(root) and frozen.is_file() and not frozen.is_symlink()
                and frozen.stat().st_size <= 8 * 1024 * 1024):
            path = frozen
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            resolution = "preserved_original_hash_cache"
    if actual != reference["sha256"] or reference.get("locator_checked") is not True:
        raise ValueError("Source proof changed or locator was not checked")
    if not reference.get("source_url", "").startswith("https://") or not reference.get("location"):
        raise ValueError("Source proof requires URL and location")
    return {"url": reference["source_url"], "locator": reference["location"], "sha256": actual,
            "evidence_type": reference.get("evidence_type", "source_metadata"),
            "semantic_expert_approval": False, "source_resolution": resolution}


def build_source_classifications(science, packets: list[dict], workspace: Path) -> dict:
    records = read_science_records(science)
    packet_map = {p["task_id"]: p for p in packets}
    specifications = {
        "S05": {"series": "GSE219217", "classification": "exploratory_confounded_extraction_time",
                "flags": ["extraction_method_aligned_with_time", "uncontrolled_before_after"],
                "proof_ids": {"P:S05:extraction", "P:S05:controls"},
                "reason": "Extraction method changes with visit; dietary and extraction effects have not been separated. Retain explicitly exploratory observations, not primary dietary causal evidence."},
        "S01": {"series": "GSE127530", "classification": "supplement_mapping_unresolved",
                "flags": ["supplement_visit_mapping_unresolved", "rna_and_clinical_denominators_differ", "dha_formulation_amount_unresolved"],
                "proof_ids": {"GEO:S01", "P:S01"},
                "reason": "The five-person RNA subset does not establish supplement-specific visit attribution. Preserve within-visit meal responses without assigning blueberry/DHA-specific effects."},
    }
    studies = {r["record_id"]: r["record"] for r in records if r["record_kind"] == "study"}
    by_series = {acc: sid for sid, value in studies.items() for acc in value.get("metadata", {}).get("series", [])}
    output = []
    for sid, spec in specifications.items():
        packet = packet_map.get("STUDY:" + sid)
        if not packet:
            raise ValueError("Missing source study packet: " + sid)
        selected = [r for r in packet["source_references"] if r.get("proof_id", r.get("id")) in spec["proof_ids"]]
        selected_ids = [r.get("proof_id", r.get("id")) for r in selected]
        if len(selected_ids) != len(spec["proof_ids"]) or set(selected_ids) != spec["proof_ids"]:
            raise ValueError("Missing or duplicate required source proofs")
        references = [checked_reference(r, workspace) for r in selected]
        for row in records:
            value = row["record"]
            study_id = (row["record_id"] if row["record_kind"] == "study" else value.get("study_id"))
            study_id = study_id or by_series.get(value.get("series_accession"))
            if study_id != sid or row["record_kind"] not in {"study", "analysis_run", "contrast", "assertion"}:
                continue
            _bound_packet(packet, row)
            result = {k: row[k] for k in ["record_kind", "record_id", "record_sha256"]}
            result.update(classification=spec["classification"], source_checked=True,
                          primary_dietary_evidence_eligible=False, scientific_approval=False,
                          expert_review_status="pending", flags=spec["flags"],
                          source_references=references, reason=spec["reason"])
            if sid == "S01":
                result["supplement_attribution_established"] = False
            output.append(result)
    return {"schema_version": "nutriomics-source-classifications-v1", "automatic_expert_approval": False,
            "science_record_set_sha256": digest([[r["record_kind"], r["record_id"], r["record_sha256"]] for r in records]),
            "records": output}


def analysis_resolution_queue(science, packets: list[dict]) -> dict:
    """Produce evidence-based *proposals*, leaving the actual decision to a person."""
    rows = []
    lookup = {p["task_id"]: p for p in packets}
    for row in read_science_records(science):
        if row["record_kind"] != "analysis_run":
            continue
        record = row["record"]
        packet = lookup.get("METHOD:" + row["record_id"])
        if not packet:
            raise ValueError("Analysis lacks source-method packet")
        _bound_packet(packet, row)
        findings = packet.get("findings", [])
        reasons = [f.get("statement_ko", "") for f in findings]
        series = record.get("series_accession")
        if series == "GSE219217":
            proposal = "exploratory_only"
        elif any("미저장" in reason for reason in reasons):
            proposal = "reexecution_needed_for_complete_model_archive"
        else:
            proposal = "retain_with_method_review"
        rows.append({"record_id": row["record_id"], "record_sha256": row["record_sha256"],
                     "series_accession": series, "analysis_version": record.get("analysis_version"),
                     "proposed_action": proposal, "human_decision": "pending", "findings": findings,
                     "numerical_error_established": False, "scientific_approval": False,
                     "method_packet": packet["task_id"]})
    return {"schema_version": "nutriomics-analysis-resolution-proposals-v1", "analyses": rows,
            "counts": dict(Counter(r["proposed_action"] for r in rows)), "automatic_expert_approval": False,
            "scope": "Source-code/model-archive audit proposals; neither rerun completion nor scientific validity is inferred."}


def science_inventory(science) -> dict:
    rows = read_science_records(science)
    kinds = Counter(r["record_kind"] for r in rows)
    return {"records": len(rows), "by_kind": dict(kinds),
            "core_review_records": sum(n for k, n in kinds.items() if k in REVIEW_KINDS),
            "outside_core_by_kind": {k: n for k, n in kinds.items() if k not in REVIEW_KINDS},
            "counts_are_independent_people": False}


def build_review_dependencies(science, packets: list[dict], workspace: Path) -> dict:
    """Every core review covers its source proofs and related model/sample records."""
    expected = {(r["record_kind"], r["record_id"]): r for r in prepare_review_records(science)}
    accumulated = {key: {"source_proofs": {}, "related_records": {}} for key in expected}
    for packet in packets:
        mapping = packet.get("record_mapping", [])
        relevant = [m for m in mapping if (m["record_kind"], str(m["record_id"])) in expected]
        if not relevant:
            continue
        proofs = [checked_reference(p, workspace) for p in packet.get("source_references", [])]
        # A byte-identical preserved producer has the same scientific identity.
        # Moving verified bytes to the hash cache must not invalidate a review.
        for proof in proofs:
            proof.pop("source_resolution", None)
        for item in relevant:
            key = (item["record_kind"], str(item["record_id"]))
            if item["record_sha256"] != expected[key]["record_sha256"]:
                raise ValueError("Related source packet has stale record hashes")
            for proof in proofs:
                accumulated[key]["source_proofs"][digest(proof)] = proof
            for other in relevant:
                identity = (other["record_kind"], str(other["record_id"]))
                if other["record_sha256"] != expected[identity]["record_sha256"]:
                    raise ValueError("Related model/sample record changed")
                accumulated[key]["related_records"][identity] = {k: other[k] for k in ("record_kind", "record_id", "record_sha256")}
    rows = []
    for key, original in sorted(expected.items()):
        dependencies = {field: [value[k] for k in sorted(value)] for field, value in accumulated[key].items()}
        if not dependencies["source_proofs"]:
            raise ValueError("Core review lacks source dependencies")
        bound = {k: original[k] for k in ("record_kind", "record_id", "record_sha256")}
        bound["dependencies"] = dependencies
        bound["source_dependency_sha256"] = digest({"record_sha256": original["record_sha256"], "dependencies": dependencies})
        rows.append(bound)
    return {"schema_version": "nutriomics-review-dependencies-v1", "automatic_expert_approval": False, "records": rows}
