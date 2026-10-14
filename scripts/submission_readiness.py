"""Evidence-based submission gate; missing human decisions remain pending."""
import argparse
import json
import re
from pathlib import Path

from nutrigenomics.publication import validate_human_review
from nutrigenomics.source_updates import atomic_json

REQUIRED_TECHNICAL = ("food50_composition1200", "effect_preservation", "provenance", "interpretation_boundaries", "warm100", "concurrent4", "browser80", "offline_html", "exports", "clean_reproduction", "rollback")


def evaluate(metadata, expected_reviews, decisions, evidence):
    failures = validate_human_review(expected_reviews, decisions)
    if any(d.get("decision") == "exclude" for d in decisions):
        failures.append({"reason": "exclusions_require_rebuilding_and_rechecking_this_release"})
    types = metadata.get("counts", {}).get("effect_types", {})
    if not any("expression" in k for k in types) or not any("methyl" in k or "cpg" in k for k in types):
        failures.append({"reason": "public_expression_and_DNAm_results_required"})
    for name in REQUIRED_TECHNICAL:
        value = evidence.get("technical", {}).get(name, {})
        if value.get("passed") is not True or value.get("release_id") != metadata["release_id"] or not value.get("report_sha256"):
            failures.append({"reason": "technical_evidence_pending", "check": name})
    benchmark = evidence.get("sealed_benchmark", {})
    if benchmark.get("expert_gold_reviewed") is not True or benchmark.get("n_queries") != 30 or not benchmark.get("raw_results_sha256"):
        failures.append({"reason": "sealed_benchmark_pending"})
    for kind in ("data", "code"):
        if not re.fullmatch(r"10\.\d{4,9}/\S+", evidence.get(kind + "_version_doi", "")):
            failures.append({"reason": kind + "_version_doi_pending"})
    for field in ("authors_and_credit_confirmed", "official_affiliation_confirmed", "funding_and_conflicts_confirmed", "institutional_ethics_determination"):
        if not evidence.get(field):
            failures.append({"reason": field + "_pending"})
    if not evidence.get("public_https_url", "").startswith("https://"):
        failures.append({"reason": "public_https_pending"})
    return {"release_id": metadata["release_id"], "submission_ready": not failures,
            "expert_review_not_inferred": True, "failures": failures}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("metadata", "expected-reviews", "evidence", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--decisions", type=Path)
    a = p.parse_args()
    read = lambda p: json.loads(p.read_text(encoding="utf-8"))
    report = evaluate(read(a.metadata), read(a.expected_reviews), read(a.decisions) if a.decisions else [], read(a.evidence))
    atomic_json(a.output, report)
    print(json.dumps({"submission_ready": report["submission_ready"], "unresolved": len(report["failures"])}))
    raise SystemExit(0 if report["submission_ready"] else 2)
