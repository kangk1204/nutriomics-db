"""Source-only retrieval review, with a separate, gated human-gold evaluation.

This module never opens the original sealed query/answer file. A protocol
amendment freezes new question-only projections when the old curator input is
unavailable. AI overlap proposals and system responses live in separate files.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "nutriomics-retrieval-review-v2"
PROTOCOL_VERSION = "benchmark-review-v2.2-20261020"
METHODS = ("keyword_only", "context_free", "bridge")
HOLDOUT_STUDIES = {"N14", "N15", "N16"}
CATEGORIES = (
    "registered_intervention", "tissue_mismatch", "time_mismatch", "duration_unknown",
    "formulation_unknown", "dose_or_unit_unknown", "mixed_exposure_causality_forbidden",
    "individual_prediction_forbidden", "clinical_endpoint_missing", "metadata_only_disclosure",
    "alias_study_deduplication", "non_significant_and_opposed_results_retained",
)
QUESTION_FIELDS = (
    "id", "partition", "source_group", "study_id", "category", "query", "component_keys",
    "food_query", "input_kind", "scenario_component_amount", "reference_context",
)
DIMENSIONS = ("tissue", "response_time_h", "regimen_duration_days", "frequency", "population")


def now():
    return datetime.now(UTC).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def object_sha(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    path = Path(path)
    if "sealed" in path.name.casefold():
        raise PermissionError("Original sealed file is never opened by this workflow")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def wilson(successes, denominator):
    if not (0 <= successes <= denominator):
        raise ValueError("Invalid precision counts")
    if not denominator:
        return {"successes": 0, "denominator": 0, "estimate": None, "ci95": None}
    z = 1.959963984540054
    p, n = successes / denominator, denominator
    divisor = 1 + z * z / n
    center = (p + z * z / (2 * n)) / divisor
    width = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / divisor
    return {"successes": successes, "denominator": denominator, "estimate": p,
            "ci95": [max(0, center - width), min(1, center + width)], "interval": "descriptive Wilson 95%",
            "dependence_note": "Queries and returned pairs share source trials; this is not a trial-independent confidence interval."}


def project_question(row):
    return {key: row.get(key) for key in QUESTION_FIELDS}


def generate_questions(curator):
    """Question fields only; deliberately never computes candidate relevance."""
    studies = curator["studies"]
    development_ids = sorted(s["id"] for s in studies if s["id"] not in HOLDOUT_STUDIES)
    if len(development_ids) != 6 or len(HOLDOUT_STUDIES & {s["id"] for s in studies}) != 3:
        raise ValueError("Protocol requires six development and three held-out study groups")
    result = {"development": [], "holdout": []}
    for study in studies:
        partition = "holdout" if study["id"] in HOLDOUT_STUDIES else "development"
        count = 10 if partition == "holdout" else 12 if study["id"] in development_ids[:4] else 11
        exposure = next((e for e in study["exposures"] if e.get("component_keys")), study["exposures"][0])
        keys = exposure.get("component_keys", [])
        for category in CATEGORIES[:count]:
            context = {"tissue": None, "response_time_h": None, "regimen_duration_days": None}
            if category == "tissue_mismatch":
                context["tissue"] = "PBMC" if study["tissue"] != "PBMC" else "whole blood"
            elif category == "time_mismatch":
                context["response_time_h"] = 53
            elif category == "duration_unknown":
                context["tissue"] = study["tissue"]
            result[partition].append({
                "id": f"query:{study['id']}:{category}", "partition": partition,
                "source_group": "trial:" + study["id"], "study_id": study["id"], "category": category,
                "query": exposure["label"], "component_keys": keys,
                "food_query": next(iter(exposure.get("food_terms", [])), None) if not keys else None,
                "input_kind": "hypothetical retrieval scenario; quantities are not measured research data",
                "scenario_component_amount": 0.15, "reference_context": context,
            })
    return result


def make_catalog(science):
    rows = {}
    for record in science["science_records"]:
        value = record.get("metadata")
        if value is None:
            value = json.loads(record["metadata_json"])
        rows.setdefault(record["kind"], []).append(value)
    curation = next(iter(rows.get("curation", [])), {})
    curated_studies = {s["study_id"]: s for s in curation.get("studies", [])}
    sources = {s["id"]: s for s in curation.get("sources", [])}
    catalog = []
    for study in sorted(rows["study"], key=lambda row: row["id"]):
        sid = study["id"]
        curated = curated_studies.get(sid, {})
        candidates = [e for e in rows.get("exposure", []) if e["study_id"] == sid]
        evidence_sources = []
        for source_id in curated.get("source_ids", []):
            source = sources.get(source_id, {})
            evidence_sources.append({
                "source_id": source_id, "url": source.get("url"), "source_kind": source.get("source_kind"),
                "source_locations": source.get("source_locations", []),
                "retrieved_response_sha256": source.get("retrieved_response_sha256"),
                "source_record_sha256": object_sha(source),
                "source_review_status": source.get("expert_review_status", "pending"),
                "source_verification_actor": source.get("verification_actor", "unknown"),
                "pmid": source.get("pmid"),
                "availability": "repository_only_or_unspecified" if source.get("source_kind") != "primary_publication" else "primary_publication",
            })
        # Some original studies have no source-curation subrecord. Preserve their
        # actual registered primary URLs, without inventing checked text locations.
        registered_sources = list(study.get("metadata", {}).get("primary_sources", []))
        audit = next((a for a in rows.get("clinical_study_audit", []) if a.get("study_id") == sid), {})
        registered_sources += [{"label": "Clinical audit registered source", "url": url} for url in audit.get("source_urls", [])]
        known_urls = {s["url"] for s in evidence_sources}
        for number, source in enumerate(registered_sources):
            url = source.get("url")
            if not url or url in known_urls:
                continue
            known_urls.add(url)
            is_paper = "geo/query" not in url and "biostudies" not in url
            match = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", url)
            evidence_sources.append({
                "source_id": f"registry:{sid}:{number}", "url": url,
                "source_kind": "primary_publication_reference" if is_paper else "repository_metadata_reference",
                "source_locations": [], "retrieved_response_sha256": None,
                "source_record_sha256": object_sha(source), "source_review_status": "pending",
                "source_verification_actor": "registry_reference_only_not_fulltext_verified",
                "pmid": match.group(1) if match else None,
                "availability": "reference_registered; exact fulltext section/table location requires human review",
            })
        catalog.append({"study_id": sid, "title": study.get("title"), "pubmed_id": study.get("pubmed_id"),
                        "study_record_sha256": object_sha(study), "source_group": "trial:" + sid,
                        "registry": study, "exposures": candidates,
                        "source_context": curated.get("verified_fields", {}), "sources": evidence_sources,
                        "source_location": "Source catalog is curator/source-checked context, not human relevance gold",
                        "human_review_status": "pending"})
    return catalog


def split_audit(question_sets, catalog):
    """Question-origin studies/papers are separate; the search corpus is common."""
    studies = {s["study_id"]: s for s in catalog}
    groups = {p: sorted({q["study_id"] for q in question_sets[p]}) for p in ("development", "holdout")}
    primary = {p: sorted({str(studies[sid]["pubmed_id"]) for sid in ids if studies[sid].get("pubmed_id")})
               for p, ids in groups.items()}
    source_pmids = {p: sorted(set(primary[p]) | {str(source["pmid"]) for sid in ids
                   for source in studies[sid]["sources"] if source.get("pmid")}) for p, ids in groups.items()}
    return {"question_origin_study_ids": groups, "primary_publication_pmids": primary,
            "all_registered_source_pmids": source_pmids,
            "study_id_overlap": sorted(set(groups["development"]) & set(groups["holdout"])),
            "primary_pmid_overlap": sorted(set(primary["development"]) & set(primary["holdout"])),
            "all_source_pmid_overlap": sorted(set(source_pmids["development"]) & set(source_pmids["holdout"])),
            "search_candidate_catalog": "Same nine-study corpus for all methods; the corpus is not partitioned",
            "scope": "Question origins separated by registered trial group and available PMID; cross-cohort participant independence is not yet human-audited"}


def source_proposals(questions, catalog):
    """Reproducible overlap suggestions from sources, never expert relevance."""
    rows = []
    for question in questions:
        for study in catalog:
            keys = set(question.get("component_keys") or [])
            food = (question.get("food_query") or "").casefold()
            matches = [e for e in study["exposures"] if keys & set(e.get("component_keys") or [])
                       or food and food in {str(t).casefold() for t in e.get("food_terms", [])}]
            rows.append({"query_id": question["id"], "study_id": study["study_id"],
                         "scope_member": bool(matches), "basis": "exact component-key/food-term overlap in source metadata",
                         "matched_exposure_ids": [e["id"] for e in matches],
                         "study_record_sha256": study["study_record_sha256"],
                         "source_refs": study["sources"], "scientific_relevance": None,
                         "actor_type": "ai_source_proposal", "human_approved": False,
                         "limitation": "Shared components neither identify an ingredient effect nor imply dose, matrix, tissue, duration or health efficacy matching."})
    return rows


def binding(manifest, partition):
    return {"schema_version": SCHEMA, "benchmark_version": manifest["version"],
            "benchmark_manifest_sha256": manifest["_manifest_file_sha256"],
            "benchmark_manifest_content_binding_sha256": manifest["manifest_binding_sha256"],
            "question_set_sha256": manifest["question_sets"][partition]["sha256"],
            "catalog_sha256": manifest["catalog_sha256"], "release_id": manifest["release_id"],
            "source_science_sha256": manifest["source_science_sha256"], "partition": partition}


def blank_review(questions, catalog, bound):
    return {"binding": bound, "status": "draft", "actor_type": None, "reviewer": None, "reviewed_at": None,
            "attestation": {"personally_reviewed_sources": False, "no_system_results_used": False,
                            "no_ai_proposals_used_as_gold": False},
            "query_reviews": [{"query_id": q["id"], "question_sha256": object_sha(q), "decision": "pending",
                               "reason": None, "complete_relevance_set": False,
                               "candidate_judgments": [{"study_id": study["study_id"], "relevance": "unjudged",
                                                         "context_relation": "unknown", "source_citations": [],
                                                         "reason": None} for study in catalog]} for q in questions]}


def write_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def prepare_review(benchmark, curator_path, science_path, output):
    benchmark, output = Path(benchmark), Path(output)
    if (output / "manifest.json").exists():
        raise FileExistsError("Review package is frozen; select a new output/version")
    original_manifest = read_json(benchmark / "manifest.json")
    dev_path = benchmark / "development_queries.candidate.json"
    if file_sha(dev_path) != original_manifest["files"][dev_path.name]:
        raise ValueError("Frozen development questions failed SHA verification")
    legacy_dev = [project_question(q) for q in read_json(dev_path)]
    current_curator, science = read_json(curator_path), read_json(science_path)
    question_sets, catalog = generate_questions(current_curator), make_catalog(science)
    if len(legacy_dev) != 70 or len(question_sets["development"]) != 70 or len(question_sets["holdout"]) != 30:
        raise ValueError("Fixed 70/30 sizes changed")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "catalog.json", catalog)
    for partition, questions in question_sets.items():
        write_json(output / f"questions.{partition}.json", questions)
    write_json(output / "questions.legacy-development.json", legacy_dev)
    # Freeze labels from sources before any response collection is possible.
    proposal_sets = {"development": question_sets["development"], "legacy-development": legacy_dev}
    for partition, questions in proposal_sets.items():
        write_json(output / f"source-proposals.{partition}.json", source_proposals(questions, catalog))
    manifest = {
        "schema_version": SCHEMA, "version": PROTOCOL_VERSION, "created_at": now(),
        "release_id": science["release_id"], "source_science_sha256": file_sha(science_path),
        "curator_sha256": file_sha(curator_path), "catalog_sha256": file_sha(output / "catalog.json"),
        "original_benchmark_manifest_sha256": file_sha(benchmark / "manifest.json"),
        "original_sealed_file_declared_sha256": original_manifest["files"]["holdout_queries.sealed.json"],
        "original_sealed_file_opened": False, "original_final_evaluation_completed": False,
        "protocol_amendment": {"reason": "Original frozen curator input cannot be recovered at its declared SHA",
                               "original_curator_sha256": original_manifest["curation_manifest_sha256"],
                               "replacement_curator_sha256": file_sha(curator_path),
                               "old_holdout_remains_sealed": True,
                               "new_holdout_contains_answers_or_system_outputs": False,
                               "scientific_review_status": "human_review_pending"},
        "question_sets": {p: {"file": f"questions.{p}.json", "count": len(q),
                              "sha256": file_sha(output / f"questions.{p}.json"),
                              "groups": sorted({r["source_group"] for r in q}),
                              "category_distribution": dict(Counter(r["category"] for r in q))}
                          for p, q in {**question_sets, "legacy-development": legacy_dev}.items()},
        "legacy_and_v2_development_question_projections_identical": legacy_dev == question_sets["development"],
        "candidate_universe": [s["study_id"] for s in catalog], "candidate_universe_size": len(catalog),
        "split_audit": split_audit(question_sets, catalog),
        "retrieval_unit": "unique study ID per question, within the same frozen nine-study catalog",
        "methods": list(METHODS), "source_fact_candidates_are_gold": False,
        "development_proposal_files": {partition: {"file": f"source-proposals.{partition}.json",
            "sha256": file_sha(output / f"source-proposals.{partition}.json")} for partition in proposal_sets},
        "scientific_precision": None, "expert_approved_queries": 0,
        "full_external_biological_validation": False,
    }
    manifest["manifest_binding_sha256"] = object_sha(manifest)
    if any(manifest["split_audit"][key] for key in ("study_id_overlap", "primary_pmid_overlap", "all_source_pmid_overlap")):
        raise ValueError("Question-origin source trial/publication overlap violates the frozen split")
    write_json(output / "manifest.json", manifest)
    # File-byte identity and canonical content binding are deliberately separate.
    # This runtime-only value is not serialized into its own hashed source file.
    manifest["_manifest_file_sha256"] = file_sha(output / "manifest.json")
    for partition, questions in question_sets.items():
        write_json(output / f"review.{partition}.template.json", blank_review(questions, catalog, binding(manifest, partition)))
    write_json(output / "review.legacy-development.template.json", blank_review(legacy_dev, catalog, binding(manifest, "legacy-development")))
    write_json(output / "holdout-authorization.template.json", {
        "binding": binding(manifest, "holdout"), "actor_type": None, "reviewer": None, "approved_at": None,
        "status": "pending", "human_gold_sha256": None, "no_system_results_seen": False,
        "protocol_amendment_accepted": False,
    })
    rows = [{"query_id": q["id"], "study_group": q["source_group"], "category": q["category"],
             "query": q["query"], "component_keys": canonical(q["component_keys"]),
             "reference_context": canonical(q["reference_context"]), "decision": "pending", "reviewer": "", "reason": ""}
            for q in question_sets["holdout"]]
    write_csv(output / "holdout-questions.csv", rows, list(rows[0]))
    judgments = [{"query_id": q["id"], "study_id": study["study_id"], "relevance": "unjudged",
                  "context_relation": "unknown", "source_id": "", "source_location": "", "source_record_sha256": "",
                  "reason": "", "reviewer": "", "reviewed_at": ""}
                 for q in question_sets["holdout"] for study in catalog]
    write_csv(output / "holdout-relevance-annotation.csv", judgments, list(judgments[0]))
    source_rows = [{"study_id": study["study_id"], **source,
                    "source_locations": "; ".join(source["source_locations"])} for study in catalog for source in study["sources"]]
    write_csv(output / "source-catalog.csv", source_rows, list(source_rows[0]))
    write_review_html(output, question_sets["holdout"], catalog, manifest)
    write_instructions(output)
    write_json(output / "package-files.sha256.json", {p.name: file_sha(p) for p in sorted(output.iterdir()) if p.is_file()})
    return manifest


def load_package(directory, partition):
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    declared = manifest.pop("manifest_binding_sha256")
    if object_sha(manifest) != declared:
        raise ValueError("Benchmark manifest binding changed")
    manifest["manifest_binding_sha256"] = declared
    manifest["_manifest_file_sha256"] = file_sha(directory / "manifest.json")
    query_spec = manifest["question_sets"][partition]
    query_path = directory / query_spec["file"]
    if file_sha(query_path) != query_spec["sha256"] or file_sha(directory / "catalog.json") != manifest["catalog_sha256"]:
        raise ValueError("Frozen question/catalog SHA changed")
    questions, catalog = read_json(query_path), read_json(directory / "catalog.json")
    if len(questions) != query_spec["count"] or len({q["id"] for q in questions}) != len(questions):
        raise ValueError("Invalid question identities/count")
    return manifest, questions, catalog


def validate_human_gold(review, manifest, questions, catalog, partition):
    """A human self-attestation is checked, not represented as authenticated identity."""
    if review.get("binding") != binding(manifest, partition):
        raise PermissionError("Human gold has stale manifest/release/question/catalog binding")
    name = str(review.get("reviewer") or "").strip()
    if review.get("actor_type") != "human" or not name or re.search(r"chatgpt|assistant|\bai\b|agent|gpt|claude", name, re.IGNORECASE):
        raise PermissionError("Only a named human may adjudicate relevance gold; AI/self approval is forbidden")
    if review.get("status") != "frozen" or not review.get("reviewed_at"):
        raise PermissionError("Human gold must be explicitly frozen and dated")
    try:
        datetime.fromisoformat(review["reviewed_at"])
    except (TypeError, ValueError) as exc:
        raise PermissionError("Invalid human review date") from exc
    attest = review.get("attestation", {})
    if not all(attest.get(key) is True for key in ("personally_reviewed_sources", "no_system_results_used", "no_ai_proposals_used_as_gold")):
        raise PermissionError("Source-only human-review attestations are required")
    index = {row["query_id"]: row for row in review.get("query_reviews", [])}
    if len(index) != len(review.get("query_reviews", [])) or set(index) != {q["id"] for q in questions}:
        raise PermissionError("Every exact frozen question requires one human decision")
    study_index = {s["study_id"]: s for s in catalog}
    for question in questions:
        row = index[question["id"]]
        if row.get("question_sha256") != object_sha(question) or row.get("decision") != "adjudicated" or not row.get("reason"):
            raise PermissionError("Question SHA, adjudicated decision and reason required")
        judgments = row.get("candidate_judgments", [])
        if len({j["study_id"] for j in judgments}) != len(judgments):
            raise PermissionError("Duplicate study judgment")
        if {j["study_id"] for j in judgments} != set(study_index):
            raise PermissionError("The common candidate universe must be explicitly represented")
        for judgment in judgments:
            if any(judgment.get(key) is True for key in ("causal_inference", "individual_prediction", "health_efficacy")):
                raise PermissionError("Retrieval relevance gold cannot approve causal/personal health efficacy")
            relevance = judgment.get("relevance")
            if relevance not in {"relevant", "irrelevant", "uncertain", "unjudged"}:
                raise PermissionError("Invalid relevance state")
            if row.get("complete_relevance_set") and relevance in {"uncertain", "unjudged"}:
                raise PermissionError("Complete relevance sets cannot contain unresolved labels")
            if relevance in {"relevant", "irrelevant"}:
                if not judgment.get("reason") or not judgment.get("source_citations"):
                    raise PermissionError("Each decided relevance label requires source location and reason")
                source_index = {s["source_id"]: s for s in study_index[judgment["study_id"]]["sources"]}
                for citation in judgment["source_citations"]:
                    source = source_index.get(citation.get("source_id"))
                    if not source or citation.get("source_record_sha256") != source["source_record_sha256"] or not citation.get("source_location"):
                        raise PermissionError("Source citation is not bound to this catalog and exact location")
                if judgment.get("context_relation") not in {"related_study", "matched_context", "not_applicable", "unknown"}:
                    raise PermissionError("Context relation is invalid")
    return index


def require_holdout_authorization(review, manifest, questions, catalog, authorization):
    index = validate_human_gold(review, manifest, questions, catalog, "holdout")
    if not authorization or authorization.get("binding") != binding(manifest, "holdout"):
        raise PermissionError("Holdout sealed: explicit human authorization bound to the amended protocol is required")
    if not (authorization.get("actor_type") == "human" and authorization.get("reviewer") == review["reviewer"]
            and authorization.get("status") == "authorized_once" and authorization.get("approved_at")
            and authorization.get("human_gold_sha256") == object_sha(review)
            and authorization.get("no_system_results_seen") is True
            and authorization.get("protocol_amendment_accepted") is True):
        raise PermissionError("Holdout sealed: human protocol/gold acceptance is incomplete")
    return index


def metric_comparison(outcomes, label_rows, *, human=False):
    labels = {(r["query_id"], r["study_id"]): r for r in label_rows}
    output = {}
    for method in METHODS:
        counts, groups = Counter(), {}
        for row in outcomes:
            if row["status"] != "available":
                counts["not_evaluable_queries" if row["status"] == "not_evaluable" else "unavailable_queries"] += 1
                continue
            returned = set(row["methods"][method]["returned_study_ids"])
            counts["evaluated_queries"] += 1
            group = groups.setdefault(row["source_group"], Counter())
            complete = row.get("complete_relevance_set", False) if human else False
            relevant = {sid for (qid, sid), label in labels.items() if qid == row["query_id"]
                        and label.get("relevance" if human else "scope_member") in ({"relevant"} if human else {True})}
            for sid in returned:
                label = labels.get((row["query_id"], sid), {})
                state = label.get("relevance") if human else "relevant" if label.get("scope_member") is True else "irrelevant" if label.get("scope_member") is False else "unjudged"
                counts["returned"] += 1
                group["returned"] += 1
                if state in {"relevant", "irrelevant"}:
                    counts["judged_returned"] += 1
                    group["judged_returned"] += 1
                    counts["true_positive"] += state == "relevant"
                    group["true_positive"] += state == "relevant"
                else:
                    counts["unjudged_returned"] += 1
            counts["queries_with_relevant_return"] += bool(returned & relevant)
            counts["empty_returns"] += not returned
            if complete:
                counts["complete_gold_queries"] += 1
                counts["complete_relevant"] += len(relevant)
                counts["complete_true_positive"] += len(returned & relevant)
                if not relevant:
                    counts["answerless_queries"] += 1
                    counts["answerless_correct"] += not returned
        precision = wilson(counts["true_positive"], counts["judged_returned"])
        output[method] = {
            "precision_on_judged_pairs": precision, "counts": dict(counts),
            "scientific_precision": precision if human and not counts["unjudged_returned"] else None,
            "recall": wilson(counts["complete_true_positive"], counts["complete_relevant"]) if counts["complete_gold_queries"] else None,
            "recall_scope": "Only explicitly complete human relevance sets; otherwise null",
            "answerless_query_correctness": wilson(counts["answerless_correct"], counts["answerless_queries"]),
            "per_source_trial": {key: {"counts": dict(value), "precision": wilson(value["true_positive"], value["judged_returned"])} for key, value in sorted(groups.items())},
            "dependence_note": "Six development or three held-out trial groups, with repeated scenario variants and overlapping returned studies; no independent-population precision claim.",
        }
    return output


def machine_policy(response, context):
    checks = []
    for record in response.get("observed_evidence", []):
        for key in ("individual_prediction", "transitive_health_inference", "component_specific_causality", "causal_inference"):
            checks.append({"name": key, "passed": record.get(key) is not True})
        checks.append({"name": "human_species", "passed": record.get("study_evidence", {}).get("species") == "Homo sapiens"})
        for card in record.get("applicability", []):
            if card.get("dimension") in DIMENSIONS and context.get(card["dimension"]) is None:
                checks.append({"name": "unspecified_dimension_remains_unknown", "passed": card.get("status") == "unknown"})
    clinical = any(r.get("clinical_results") for r in response.get("observed_evidence", []))
    conflict = clinical and any("수치형 임상 결과가 연결되어 있지 않습니다" in gap for gap in response.get("evidence_gaps", []))
    checks.append({"name": "clinical_gap_does_not_contradict_attached_records", "passed": not conflict})
    return {"passed": sum(c["passed"] for c in checks), "total": len(checks), "failures": [c["name"] for c in checks if not c["passed"]]}


def http_json(base_url, route, payload=None):
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("Use an explicit HTTP(S) base URL without credentials")
    data = canonical(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(base_url.rstrip("/") + route, data=data,
                                     headers={"Content-Type": "application/json", "User-Agent": SCHEMA})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(16_000_001)
        if len(raw) > 16_000_000:
            raise ValueError("Bounded development response exceeded 16 MB")
        return json.loads(raw), hashlib.sha256(raw).hexdigest()


def evaluate_review(directory, output, base_url, *, partition="legacy-development", human_gold=None,
                    authorization=None, software_version="unspecified", transport=http_json):
    manifest, questions, catalog = load_package(directory, partition)
    if software_version == "unspecified":
        raise ValueError("Record the actual deployed software version before collecting responses")
    gold_index = None
    proposals = []
    if partition == "holdout":
        if human_gold is None:
            raise PermissionError("Holdout sealed: source-only human gold is missing; no HTTP request issued")
        gold_index = require_holdout_authorization(human_gold, manifest, questions, catalog, authorization)
    elif human_gold is not None:
        gold_index = validate_human_gold(human_gold, manifest, questions, catalog, partition)
    if partition != "holdout":
        proposal_spec = manifest["development_proposal_files"][partition]
        proposal_path = Path(directory) / proposal_spec["file"]
        if file_sha(proposal_path) != proposal_spec["sha256"]:
            raise ValueError("Source-only proposals changed; refusing label leakage")
        proposals = read_json(proposal_path)
    output = Path(output)
    if (output / "evaluation.json").exists():
        raise FileExistsError("Evaluation already recorded; do not silently repeat a held-out run")
    output.mkdir(parents=True, exist_ok=True)
    start = now()
    if partition == "holdout":
        reservation = Path(directory).parent / "holdout-run-receipts" / (object_sha(authorization) + ".json")
        reservation.parent.mkdir(parents=True, exist_ok=True)
        try:
            with reservation.open("x", encoding="utf-8") as stream:
                json.dump({"status": "reserved_before_http", "started_at": start,
                           "authorization_sha256": object_sha(authorization), "output": str(output),
                           "binding": binding(manifest, partition)}, stream, ensure_ascii=False, indent=2)
        except FileExistsError as exc:
            raise PermissionError("Holdout authorization already reserved/used; no automatic rerun") from exc
    meta, meta_body_sha = transport(base_url, "/api/v1/explorer/meta")
    if meta.get("release_id") != manifest["release_id"]:
        raise ValueError("Live release differs from frozen review catalog; no question executed")
    write_json(output / "live-meta.json", meta)
    universe = set(manifest["candidate_universe"])
    request_cache, discovery_cache, outcomes, policy_rows = {}, {}, [], []
    response_dir = output / "system-responses"
    response_dir.mkdir(exist_ok=True)
    for query in questions:
        tokens = {w.casefold() for w in re.findall(r"[\w-]+", query.get("query") or "") if len(w) > 2}
        keys = set(query.get("component_keys") or [])
        keyword = {s["study_id"] for s in catalog for e in s["exposures"]
                   if tokens & set(re.findall(r"[\w-]+", str(e.get("label", "")).casefold()))}
        free = {s["study_id"] for s in catalog for e in s["exposures"] if keys & set(e.get("component_keys") or [])}
        # Each source component is queried once for each distinct explicit context.
        # The current explorer accepts a component identity, not an assumed amount.
        bridge, refs, errors = set(), [], []
        context = {k: v for k, v in (query.get("reference_context") or {}).items() if v is not None}
        seeds = [key.replace("FDC-NUTRIENT:", "FDCNutrient:") for key in sorted(keys)]
        if not seeds and query.get("food_query"):
            route = "/api/v1/explorer/entities?kind=food&q=" + urllib.parse.quote(query["food_query"]) + "&page_size=100"
            if route not in discovery_cache:
                try:
                    response, body_sha = transport(base_url, route)
                    discovery_cache[route] = {"status": "available", "response": response, "http_body_sha256": body_sha,
                                              "route": route, "received_at": now()}
                except (OSError, ValueError, urllib.error.URLError) as error:
                    discovery_cache[route] = {"status": "unavailable", "error": str(error), "route": route, "received_at": now()}
                write_json(response_dir / ("discovery-" + object_sha(route) + ".json"), discovery_cache[route])
            discovery = discovery_cache[route]
            candidates = discovery.get("response", {}).get("items", [])
            # Name ambiguity is not silently resolved into a food choice.
            if discovery["status"] != "available":
                errors.append({"code": "discovery_request_failed", "route": route})
            elif len(candidates) == 1:
                seeds = [candidates[0]["id"]]
            else:
                errors.append({"code": "food_not_found" if not candidates else "food_choice_required", "candidate_count": len(candidates)})
        for seed in seeds:
            payload = {"seed_id": seed, "reference_context": context}
            identity = object_sha(payload)
            if identity not in request_cache:
                try:
                    response, body_sha = transport(base_url, "/api/v1/explorer/match", payload)
                    if response.get("release_id") != manifest["release_id"]:
                        raise ValueError("Live release changed during evaluation")
                    request_cache[identity] = {"status": "available", "response": response, "http_body_sha256": body_sha,
                                               "payload": payload, "received_at": now()}
                except (OSError, ValueError, urllib.error.URLError) as error:
                    request_cache[identity] = {"status": "unavailable", "error": str(error), "payload": payload,
                                               "received_at": now()}
                write_json(response_dir / f"{identity}.json", request_cache[identity])
            receipt = request_cache[identity]
            refs.append(identity)
            if receipt["status"] != "available":
                errors.append({"code": "request_failed", "request_id": identity})
                continue
            response = receipt["response"]
            ids = {str(e["study_id"]) for e in response.get("observed_evidence", [])}
            if ids - universe:
                errors.append({"code": "outside_frozen_candidate_universe", "ids": sorted(ids - universe)})
            bridge |= ids & universe
            policy_rows.append({"query_id": query["id"], "request_id": identity, **machine_policy(response, context)})
        # An empty, valid query is an explicit no-answer case, not a request error.
        status = "available"
        if errors:
            status = "not_evaluable" if all(e["code"] in {"food_not_found", "food_choice_required"} for e in errors) else "unavailable"
        outcomes.append({"query_id": query["id"], "source_group": query["source_group"],
                         "status": status, "errors": errors,
                         "request_refs": refs, "complete_relevance_set": bool(gold_index and gold_index[query["id"]].get("complete_relevance_set")),
                         "methods": {name: {"returned_study_ids": sorted(ids)} for name, ids in zip(METHODS, (keyword, free, bridge))}})
    human_labels = [{"query_id": qid, **judgment} for qid, row in (gold_index or {}).items() for judgment in row["candidate_judgments"]]
    summary = {"schema_version": SCHEMA, "partition": partition, "started_at": start, "completed_at": now(),
               "binding": binding(manifest, partition), "actual_software_version": software_version,
               "collector_producer_sha256": file_sha(__file__),
               "base_url": base_url, "meta_http_body_sha256": meta_body_sha, "meta_file_sha256": file_sha(output / "live-meta.json"),
               "query_count": len(questions), "available_queries": sum(r["status"] == "available" for r in outcomes),
               "not_evaluable_queries": sum(r["status"] == "not_evaluable" for r in outcomes),
               "unavailable_queries": sum(r["status"] == "unavailable" for r in outcomes),
               "actual_unique_match_requests": len(request_cache), "repeated_http_requests_per_payload": 1,
               "actual_unique_discovery_requests": len(discovery_cache),
               "total_http_requests_including_metadata": 1 + len(request_cache) + len(discovery_cache),
               "candidate_universe": sorted(universe), "methods": list(METHODS),
               "provisional_source_scope_comparison": metric_comparison(outcomes, proposals) if proposals else None,
               "human_gold_scientific_comparison": metric_comparison(outcomes, human_labels, human=True) if gold_index else None,
               "human_gold_sha256": object_sha(human_gold) if human_gold else None,
               "scientific_accuracy_status": "human_gold_evaluation" if gold_index else "not_evaluated_no_human_gold",
               "source_fact_candidates_are_gold": False, "original_sealed_file_opened": False,
               "provisional_scope_is_rule_consistency_not_relevance_accuracy": True,
               "circularity_disclosure": "The provisional exact-key overlap labels share the context_free baseline rule; a high scope precision is expected by construction and is not independent scientific validation.",
               "holdout_system_requests": partition == "holdout", "sota_validated": False,
               "scenario_amount_application": "Frozen hypothetical 0.15 component amount is not accepted by explorer/match; nutrient dose stays unknown. This evaluates topical/context disclosure, not the old amount-aware meal API or dose-response accuracy.",
               "baseline_scope": "keyword_only and context_free use frozen exposure metadata; bridge uses actual HTTP union of the same query component identities, with explicit context. Study IDs are deduplicated for all methods.",
               "trial_dependence": {"question_groups": dict(Counter(q["source_group"] for q in questions)),
                                    "independent_trials_confirmed": False},
               "machine_policy": {"passed": sum(r["passed"] for r in policy_rows), "total": sum(r["total"] for r in policy_rows),
                                  "failure_counts": dict(Counter(f for row in policy_rows for f in row["failures"])),
                                  "scope": "Response disclosure/contract checks, not biological relevance or clinical accuracy"},
               "outcomes": outcomes}
    write_json(output / "machine-policy.json", policy_rows)
    write_json(output / "evaluation.json", summary)
    write_evaluation_tables(output, summary)
    write_json(output / "files.sha256.json", {str(p.relative_to(output)).replace("\\", "/"): file_sha(p)
                                               for p in sorted(output.rglob("*")) if p.is_file()})
    return summary


def write_evaluation_tables(output, summary):
    table_rows = []
    for outcome in summary["outcomes"]:
        for method in METHODS:
            table_rows.append({"query_id": outcome["query_id"], "source_group": outcome["source_group"],
                               "method": method, "status": outcome["status"],
                               "returned_study_ids": ";".join(outcome["methods"][method]["returned_study_ids"]),
                               "returned_unique_studies": len(outcome["methods"][method]["returned_study_ids"]),
                               "errors": canonical(outcome["errors"]),
                               "complete_human_relevance_set": outcome["complete_relevance_set"]})
    write_csv(output / "query-method-results.csv", table_rows, list(table_rows[0]))
    rows = []
    for label, comparison in [("source_scope_provisional", summary["provisional_source_scope_comparison"]),
                              ("human_gold", summary["human_gold_scientific_comparison"])]:
        for method, metrics in (comparison or {}).items():
            precision = metrics["precision_on_judged_pairs"]
            rows.append({"evaluation_scope": label, "method": method, "successes": precision["successes"],
                         "denominator": precision["denominator"], "estimate": precision["estimate"],
                         "descriptive_wilson_ci95": canonical(precision["ci95"]),
                         "recall": canonical(metrics["recall"]), "scientific_precision": canonical(metrics["scientific_precision"]),
                         "available_queries": metrics["counts"].get("evaluated_queries", 0),
                         "unjudged_returned": metrics["counts"].get("unjudged_returned", 0)})
    write_csv(output / "comparison-summary.csv", rows, list(rows[0]))
    esc = lambda value: html.escape(str(value))
    body = "".join("<tr>" + "".join(f"<td>{esc(row[key])}</td>" for key in (
        "evaluation_scope", "method", "successes", "denominator", "estimate", "descriptive_wilson_ci95")) + "</tr>" for row in rows)
    unavailable = [row for row in summary["outcomes"] if row["status"] != "available"]
    details = "".join(f'<li>{esc(row["query_id"])}: {esc(canonical(row["errors"]))}</li>' for row in unavailable)
    (output / "development-review.html").write_text(f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NutriOmics retrieval review</title><style>body{{max-width:1080px;margin:40px auto;padding:0 24px;color:#182b37;font:15px/1.7 system-ui,sans-serif}}table{{border-collapse:collapse;width:100%}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #dce2e6;overflow-wrap:anywhere}}th{{background:#edf2f4}}code{{overflow-wrap:anywhere}}.note{{padding:16px;background:#f2f5f7}}@media print{{body{{margin:10mm;font-size:11px}}}}</style><h1>개발 조회 점검 · {esc(summary["partition"])}</h1><p class="note">과학적 정확도 상태: <strong>{esc(summary["scientific_accuracy_status"])}</strong>. 자동 성분공유 라벨은 사람 정답이 아닙니다. context_free와 같은 규칙에서 만든 범위 라벨이므로 높은 일치율은 정의상 생길 수 있습니다. 효능·인과성·SOTA 검증으로 해석하지 않습니다.</p><p>질문 {summary["query_count"]}개 중 같은 후보집합으로 비교 가능한 질문 {summary["available_queries"]}개. 최종 holdout 조회 여부: {esc(summary["holdout_system_requests"])}. 실제 고유 match 요청 {summary["actual_unique_match_requests"]}회.</p><p>데이터 release: {esc(summary["binding"]["release_id"])}<br>소프트웨어: {esc(summary["actual_software_version"])}<br>시작: {esc(summary["started_at"])}</p><table><thead><tr><th>평가 범위</th><th>방법</th><th>일치</th><th>판정된 반환쌍</th><th>비율</th><th>설명용 Wilson95%</th></tr></thead><tbody>{body}</tbody></table><p>관련 연구9개를 공통 코퍼스로 사용하고 연구ID를 중복 제거했습니다. 반복 질문·공통 원논문에 따른 의존성이 있어 독립 표본의 신뢰구간으로 해석하지 않습니다. 완전한 사람 정답 집합 없이는 recall=null입니다. .15 성분량은 현재 API에서 받지 않아 dose unknown으로 유지했습니다.</p><h2>비교 제외·조회 실패</h2><ul>{details or '<li>없음</li>'}</ul><h2>응답 계약 점검</h2><pre>{esc(canonical(summary["machine_policy"]))}</pre><p>계약 점검은 과학적 관련성 정확도와 별도입니다. 원래 최종30개 sealed 파일은 열지 않았으며 전문가 승인·DOI·논문 준비 완료를 이 점검으로 생성하지 않습니다.</p></html>''', encoding="utf-8")


def write_review_html(output, questions, catalog, manifest):
    esc = lambda value: html.escape(str(value))
    source_cards = []
    for study in catalog:
        links = "".join(f'<li><a href="{esc(s["url"] or "")}" rel="noreferrer">{esc(s["source_id"])}</a>: {esc("; ".join(s["source_locations"]))}<br><code>{esc(s["source_record_sha256"])}</code></li>' for s in study["sources"])
        source_cards.append(f'<details><summary>{esc(study["study_id"])} · {esc(study["title"])}</summary><ul>{links}</ul></details>')
    question_cards = "".join(f'<article><h3>{index}. {esc(q["query"])}</h3><p>{esc(q["id"])} · {esc(q["category"])}</p><pre>{esc(canonical(q["reference_context"]))}</pre><p>성분 식별자: {esc(", ".join(q["component_keys"])) or "없음"}. 0.15는 가정 입력이며 측정 섭취량이 아닙니다.</p><p>전체9개 연구에서 관련/무관/불확실을 원문 위치와 함께 판정하십시오. 시스템 응답과 자동 후보 정답은 제공하지 않습니다.</p></article>' for index, q in enumerate(questions, 1))
    document = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NutriOmics source-only review</title><style>body{{max-width:1120px;margin:40px auto;padding:0 24px;color:#172b39;font:15px/1.7 system-ui,sans-serif}}h1{{font-size:28px}}article{{break-inside:avoid;border-top:1px solid #ccd7dc;padding:16px 0}}details{{border:1px solid #d8e0e4;padding:12px;margin:8px 0}}summary{{cursor:pointer;font-weight:600}}code,pre{{overflow-wrap:anywhere;white-space:pre-wrap;font-size:12px}}.note{{background:#f0f4f5;padding:18px}}@media print{{details{{display:block}}body{{margin:10mm;max-width:none}}}}</style><h1>최종30질의 · 원문 기반 사람 검수</h1><p class="note">신규 v2 프로토콜의 질문만 수록했습니다. 기존 sealed 파일은 열지 않았습니다. 이 문서는 정답이나 시스템 결과를 포함하지 않습니다. 검수270개 후보 행은 관련성270건을 뜻하지 않습니다. 30개 질문×동일9연구의 빈 판정표입니다. 전문가 승인0건, 최종조회 미실행 상태입니다.</p><p>공개 release: {esc(manifest["release_id"])}<br>Manifest binding: <code>{esc(manifest["manifest_binding_sha256"])}</code></p><h2>전체 후보 연구 · 무순위 원문 목록</h2>{''.join(source_cards)}<h2>질문30개</h2>{question_cards}</html>'''
    (output / "holdout-source-only-review.html").write_text(document, encoding="utf-8")


def write_instructions(output):
    (output / "REVIEW_README_ko.md").write_text("""# 원문 기반 조회 평가 검수

이 패키지는 기존 최종30개 파일을 열지 않은 신규 v2 평가 프로토콜이다. 원래 curator SHA의 원본을 확보하지 못해 현재 curator·실제 공개 science snapshot에 새 질문을 동결했다. 기존 최종평가는 미완료로 보존한다. 개발70의 기존질문과 신규질문은 별도 분모이며 동일성을 manifest에 기록한다. 논문·연구 그룹은 개발6개/최종3개로 분리한다. 반환 후보는 두 단계 모두 같은9연구이며, 연구 간 일반화나 새로운 생물학적 외부검증을 뜻하지 않는다.

1. `holdout-source-only-review.html` 또는 `holdout-questions.csv`에서30개 질문과 전체9연구 원문 목록을 확인한다. 시스템 응답·AI 제안을 보고 정답을 작성하지 않는다. 식이 형태·단위·조직·기간·대상군과 전체 식단/단일성분, 무대조/군간비교를 구분한다.
2. `review.holdout.template.json`을 별도 파일 `human-gold.holdout.json`으로 복사한다. 검수자·ISO일시·사람 확인과 source-only 서약을 직접 입력한다. 각 질문의 `decision=adjudicated`, 이유와 각 연구의 `relevance=relevant/irrelevant/uncertain/unjudged`를 작성한다. 관련 연구 조회와 조건 일치(`context_relation`)는 별도다.
3. 판정한 relevant/irrelevant에는 원문 `source_id`, catalog의 `source_record_sha256`, 실제 절/표/행 위치 `source_location`과 이유가 필수다. 근거 부재를 효과0이나 건강상 이득으로 바꾸지 않는다. 9연구를 모두 판정해 완전 정답 집합을 확보한 경우만 `complete_relevance_set=true`를 설정한다. 그렇지 않으면 recall은 null이다.
4. 사람이 직접 `actor_type=human`, `status=frozen`, source-only 확인3개를 true로 설정한다. 소프트웨어는 실명 인증을 하지 못하며 이 기록은 본인 확인 서약이다. AI가 생성한 동의/승인은 허용하지 않는다. 수정이 필요하면 새 버전으로 동결하고 이미 본 최종질의로 규칙을 조정하지 않는다.
5. HTTP 없이 검수형식을 먼저 확인하려면 `python scripts/evaluate_retrieval_review.py --package <이폴더> --partition holdout --human-gold <검수JSON> --validate-only`를 사용한다. 같은 binding을 가진 `holdout-authorization.template.json`을 별도 `holdout-authorization.json`으로 복사하고 `actor_type=human`, `reviewer`, `approved_at`, `status=authorized_once`, `protocol_amendment_accepted=true`, `no_system_results_seen=true`, human gold canonical SHA256을 직접 확인한다. SHA는 `.venv-win/Scripts/python.exe -c "from nutrigenomics.retrieval_review import *; print(object_sha(read_json('human-gold.holdout.json')))"`로 계산한다. 실행승인1건은 독점적인 예약영수증으로 한 번만 사용하고 실패하더라도 자동 재시도하지 않는다. 기존 sealed 정답은 이 과정에 필요하지 않다.
6. 최종조회 명령은 승인 후 한 번만 실행한다. `python scripts/evaluate_retrieval_review.py --package <이폴더> --partition holdout --human-gold <검수JSON> --authorization <승인JSON> --base-url <실제서비스주소> --software-version <실제code/version> --output <새평가폴더>` 승인 전에는 어떤 HTTP 질문 요청도 실행하지 않는다.

개발용: `python scripts/evaluate_retrieval_review.py --package <이폴더> --partition legacy-development --base-url http://127.0.0.1:18089 --software-version source-v14 --output <새개발평가폴더>`; 신규 개발70은 `--partition development`로 별도 실행한다. 각 고유 payload는1회만 요청한다. 고정성능24,800회는 재실행하지 않는다.

세 비교법: keyword_only는 원본 노출명 토큰 일치, context_free는 동일 component key 공유, bridge는 실제 API가 반환한 연구(동일 성분목록의 union)다. 후보집합9연구/연구ID중복제거는 공통이다. 실제 API는 가정 .15성분량을 받지 않아 용량은 unknown이며 이 한계를 결과에 명시한다. 관련연구 조회 자체가 조건 일치나 효능을 뜻하지 않는다. 자동 source-scope precision은 전문가 scientific precision과 분리하며 메타데이터200개를 gold로 승격하지 않는다. precision은 판정된 반환쌍의 분모와 Wilson구간, 불확실/미판정 반환 수, 연구별 결과를 함께 표시한다. 작은 연구군에서 반복 질문의 의존성이 있어 독립 표본의 신뢰구간으로 해석하지 않는다. 참인 무응답과 HTTP 실패는 별도 상태다.
""", encoding="utf-8")
