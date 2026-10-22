"""Development-only catalog/explicit-exposure diagnostics, never a gold evaluation."""
from __future__ import annotations

import hashlib
import json
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _words(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def registered_alias_match(record: dict, query: str) -> bool:
    """Use only actually registered names, not biological synonym inference."""
    aliases = record.get("food_terms") or []
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except ValueError:
            aliases = [aliases]
    values = [record.get("id"), record.get("study_id"), record.get("label"),
              record.get("formulation"), *(aliases if isinstance(aliases, list) else [])]
    term = _words(query)
    return bool(term) and any(term in _words(value)
                              or term.replace(" ", "") in _words(value).replace(" ", "")
                              for value in values)


def development_inputs(directory: str | Path) -> tuple[dict, list[dict], dict]:
    """Only the development projection is opened; the holdout file is never read."""
    directory = Path(directory)
    manifest_bytes = (directory / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    spec = manifest.get("question_sets", {}).get("development", {})
    if spec.get("file") != "questions.development.json":
        raise PermissionError("Only the named development projection may be diagnosed")
    question_bytes = (directory / "questions.development.json").read_bytes()
    if sha256(question_bytes) != spec.get("sha256"):
        raise ValueError("Development question projection hash mismatch")
    questions = json.loads(question_bytes)
    if len(questions) != spec.get("count") or any(row.get("partition") != "development" for row in questions):
        raise PermissionError("Every input must belong to the frozen development partition")
    groups = {row.get("source_group") for row in questions}
    if groups != set(spec.get("groups", [])):
        raise ValueError("Development study groups differ from manifest")
    return manifest, questions, {"manifest_sha256": sha256(manifest_bytes),
                                "development_questions_sha256": sha256(question_bytes),
                                "question_count": len(questions), "source_groups": sorted(groups)}


def _transport(base_url: str, route: str, payload: dict | None = None) -> tuple[dict, bytes]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(base_url.rstrip("/") + route, data=body,
                                     headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw), raw


def diagnose_development_inputs(
    directory: str | Path, output: str | Path, base_url: str, *,
    expected_release_id: str, software_version: str, selected_exposure_ids: tuple[str, ...] = (),
    transport: Callable | None = None,
) -> dict:
    """One request per distinct input; optional exact exposure IDs simulate UI selection.

    A food query and a research exposure are different inputs. This diagnostic never
    substitutes the latter into the frozen benchmark or computes relevance metrics.
    """
    manifest, questions, binding = development_inputs(directory)
    output = Path(output)
    if output.exists():
        raise FileExistsError("Preserve prior diagnostic receipts; choose a new output directory")
    output.mkdir(parents=True)
    fetch = transport or (lambda route, payload=None: _transport(base_url, route, payload))
    requests = []

    def call(route: str, payload: dict | None = None) -> dict:
        value, raw = fetch(route, payload)
        request_id = sha256(json.dumps([route, payload], sort_keys=True).encode())
        (output / f"response-{request_id}.json").write_bytes(raw)
        requests.append({"route": route, "payload": payload, "http_body_sha256": sha256(raw),
                         "response_file": f"response-{request_id}.json"})
        if value.get("release_id") != expected_release_id:
            raise ValueError("Live release differs from explicitly expected diagnostic release")
        return value

    meta = call("/api/v1/explorer/meta")
    registry = call("/api/v1/interventions?limit=1000&offset=0")
    records = registry.get("items")
    if not isinstance(records, list) or registry.get("total", len(records)) > len(records):
        raise ValueError("Intervention registry is incomplete or malformed")
    selected = set(selected_exposure_ids)
    registered = {str(record["id"]): record for record in records}
    if selected - registered.keys():
        raise ValueError("Selected exposure ID is absent from the actual live registry")
    groups: dict[str, list[dict]] = {}
    for query in questions:
        if not query.get("component_keys") and query.get("food_query"):
            groups.setdefault(query["food_query"], []).append(query)
    outcomes = []
    probed = {}
    for query, rows in sorted(groups.items()):
        route = "/api/v1/explorer/entities?" + urllib.parse.urlencode({
            "kind": "food_record", "q": query, "page_size": "100", "tier": "all",
            "domain": "all", "include_extracted": "false"})
        foods = call(route)
        matches = [record for record in records if registered_alias_match(record, query)]
        traces = []
        for record in matches:
            identifier = str(record["id"])
            if identifier not in selected:
                continue
            if identifier not in probed:
                node_id = "Exposure:" + identifier
                node = call("/api/v1/explorer/nodes/" + urllib.parse.quote(node_id, safe=""))
                if node.get("id") != node_id or node.get("kind") not in {"diet", "exposure"}:
                    raise ValueError("Selected exposure does not resolve to the original exposure node")
                payload = {"seed_id": node_id, "reference_context": {}}
                result = call("/api/v1/explorer/match", payload)
                evidence = result.get("observed_evidence") or []
                exact = [item for item in evidence if item.get("study_id") == record["study_id"]
                         and item.get("mapping_identity", {}).get("status") == "same_record"
                         and item.get("exposure", {}).get("id") == identifier]
                probed[identifier] = {"exposure_id": identifier, "node_id": node_id,
                                      "study_id": record["study_id"], "explicit_test_selection": True,
                                      "exact_record_trace": bool(exact), "food_grams_submitted": False,
                                      "returned_study_ids": sorted({item.get("study_id") for item in evidence}),
                                      "dose": {key: record.get(key) for key in
                                               ("amount", "unit", "duration_days", "dose_basis")}}
            traces.append(probed[identifier])
        outcomes.append({"food_query": query, "question_ids": [row["id"] for row in rows],
                         "legacy_development_scenarios": len(rows),
                         "exact_food_discovery": {"route": route, "total": foods.get("total"),
                                                  "returned_ids": [item.get("id") for item in foods.get("items", [])]},
                         "registered_exposure_candidates": [{"id": record["id"], "study_id": record["study_id"],
                                                              "label": record.get("label")} for record in matches],
                         "explicit_selection_traces": traces,
                         "exposure_selection_does_not_satisfy_original_food_input": True,
                         "composition_or_food_dose_invented": False})
    result = {"schema_version": "nutriomics-development-input-diagnostic-v1",
              "completed_at": datetime.now(UTC).isoformat(), "binding": binding,
              "review_catalog_release_id": manifest.get("release_id"),
              "actual_release_id": meta["release_id"], "actual_software_version": software_version,
              "actual_service_build_id": meta.get("service_build_id"),
              "producer_sha256": sha256(Path(__file__).read_bytes()), "base_url": base_url,
              "request_count": len(requests), "requests": requests, "outcomes": outcomes,
              "scope": "Development input availability and exact-record trace; not a relevance or health efficacy evaluation",
              "human_gold_scientific_precision": None, "scientific_recall": None,
              "frozen_questions_modified": False, "holdout_file_opened": False,
              "holdout_requests": 0, "human_approval_created": False}
    _write(output / "input-diagnostic.json", result)
    return result
