"""Evidence-based submission gate; missing human decisions remain pending."""
import argparse
import hashlib
import json
import re
from pathlib import Path

from nutrigenomics.publication import validate_human_review
from nutrigenomics.source_updates import atomic_json

REQUIRED_TECHNICAL = ("food50_composition1200", "effect_preservation", "provenance", "interpretation_boundaries", "warm100", "concurrent4", "browser80", "offline_html", "exports", "clean_reproduction", "rollback")


def _bound_report(value, root, *, path_key="report_path", hash_key="report_sha256"):
    """A declared check is insufficient: verify its stored evidence bytes."""
    if root is None or not value.get(path_key) or not value.get(hash_key):
        return None
    root = Path(root).resolve()
    path = (root / value[path_key]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != value[hash_key]:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        return None


def evaluate(metadata, expected_reviews, decisions, evidence, *, evidence_root=None):
    failures = validate_human_review(expected_reviews, decisions)
    if any(d.get("decision") == "exclude" for d in decisions):
        failures.append({"reason": "exclusions_require_rebuilding_and_rechecking_this_release"})
    types = metadata.get("counts", {}).get("effect_types", {})
    if not any("expression" in k for k in types) or not any("methyl" in k or "cpg" in k for k in types):
        failures.append({"reason": "public_expression_and_DNAm_results_required"})
    for name in REQUIRED_TECHNICAL:
        value = evidence.get("technical", {}).get(name, {})
        report = _bound_report(value, evidence_root)
        if (value.get("passed") is not True or value.get("release_id") != metadata["release_id"]
                or not report or report.get("passed") is not True
                or report.get("release_id") != metadata["release_id"]):
            failures.append({"reason": "technical_evidence_pending", "check": name})
    benchmark = evidence.get("sealed_benchmark", {})
    gold = _bound_report(benchmark, evidence_root, path_key="raw_results_path", hash_key="raw_results_sha256")
    if (benchmark.get("expert_gold_reviewed") is not True or benchmark.get("n_queries") != 30
            or not gold or gold.get("release_id") != metadata["release_id"]
            or gold.get("expert_gold_reviewed") is not True or gold.get("n_queries") != 30):
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
    report = evaluate(read(a.metadata), read(a.expected_reviews), read(a.decisions) if a.decisions else [], read(a.evidence), evidence_root=a.evidence.parent)
    atomic_json(a.output, report)
    print(json.dumps({"submission_ready": report["submission_ready"], "unresolved": len(report["failures"])}))
    raise SystemExit(0 if report["submission_ready"] else 2)
