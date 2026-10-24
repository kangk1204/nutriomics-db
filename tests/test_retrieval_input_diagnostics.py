"""Synthetic development-only diagnostics; no gold labels or real HTTP calls."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nutrigenomics.retrieval_input_diagnostics import (
    development_inputs,
    diagnose_development_inputs,
    registered_alias_match,
    sha256,
)


@pytest.fixture
def review(tmp_path):
    directory = tmp_path / "review"
    directory.mkdir()
    rows = [{"id": "dev-1", "partition": "development", "source_group": "trial:S03",
             "food_query": "포도씨 추출물", "component_keys": []},
            {"id": "dev-2", "partition": "development", "source_group": "trial:S03",
             "food_query": "포도씨 추출물", "component_keys": []}]
    raw = json.dumps(rows).encode()
    (directory / "questions.development.json").write_bytes(raw)
    manifest = {"release_id": "candidate", "question_sets": {"development": {
        "file": "questions.development.json", "sha256": sha256(raw), "count": 2,
        "groups": ["trial:S03"]}}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    # Intentionally invalid. Any attempt to open this file is an error.
    (directory / "questions.holdout.json").write_text("DO NOT OPEN")
    return directory


@pytest.fixture
def registry():
    return {"id": "S03:original", "study_id": "S03", "label": "Source extract",
            "food_terms": ["포도씨 추출물", "grape seed extract"], "amount": 200,
            "unit": "mg", "duration_days": 56, "dose_basis": "assigned mixture per day"}


def transport(registry, *, release="actual", exact=True):
    calls = []

    def fetch(route, payload=None):
        calls.append((route, payload))
        if route.endswith("/meta"):
            result = {"service_build_id": "code-1"}
        elif route.startswith("/api/v1/interventions"):
            result = {"items": [registry]}
        elif "/entities?" in route:
            result = {"items": [], "total": 0}
        elif "/nodes/" in route:
            result = {"id": "Exposure:S03:original", "kind": "diet"}
        elif route.endswith("/match"):
            result = {"observed_evidence": [{"study_id": "S03", "exposure": {"id": "S03:original"},
                       "mapping_identity": {"status": "same_record" if exact else "related_food"}}]}
        else:
            raise AssertionError(route)
        result["release_id"] = release
        return result, json.dumps(result).encode()
    return fetch, calls


def test_only_development_file_is_opened(review, monkeypatch):
    original = Path.read_bytes
    opened = []

    def guarded(path):
        opened.append(path.name)
        assert "holdout" not in path.name
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", guarded)
    _, rows, binding = development_inputs(review)
    assert len(rows) == binding["question_count"] == 2
    assert opened == ["manifest.json", "questions.development.json"]


def test_rejects_altered_projection_before_http(review):
    (review / "questions.development.json").write_text("[]")
    with pytest.raises(ValueError, match="hash mismatch"):
        development_inputs(review)


def test_rejects_holdout_path_even_when_manifest_points_to_it(review):
    path = review / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["question_sets"]["development"]["file"] = "questions.holdout.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(PermissionError, match="development projection"):
        development_inputs(review)


def test_rejects_mislabeled_partition(review):
    path = review / "questions.development.json"
    rows = json.loads(path.read_text())
    rows[0]["partition"] = "holdout"
    raw = json.dumps(rows).encode()
    path.write_bytes(raw)
    manifest_path = review / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["question_sets"]["development"]["sha256"] = sha256(raw)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(PermissionError, match="Every input"):
        development_inputs(review)


def test_original_source_aliases_only(registry):
    assert registered_alias_match(registry, "포도씨추출물")
    assert registered_alias_match(registry, "GRAPE SEED EXTRACT")
    assert not registered_alias_match(registry, "cocoa product")
    assert not registered_alias_match(registry, "")
    assert not registered_alias_match(registry, "treat obesity")


def test_explicit_exposure_is_separate_from_food_input(review, registry, tmp_path):
    fetch, calls = transport(registry)
    result = diagnose_development_inputs(review, tmp_path / "output", "http://unused",
        expected_release_id="actual", software_version="code-test", selected_exposure_ids=(registry["id"],),
        transport=fetch)
    assert len(calls) == result["request_count"] == 5
    outcome = result["outcomes"][0]
    assert outcome["legacy_development_scenarios"] == 2
    assert outcome["exact_food_discovery"]["total"] == 0
    assert "kind=food_record" in outcome["exact_food_discovery"]["route"]
    assert outcome["explicit_selection_traces"][0]["exact_record_trace"] is True
    assert outcome["exposure_selection_does_not_satisfy_original_food_input"] is True
    assert calls[-1][1] == {"seed_id": "Exposure:S03:original", "reference_context": {}}
    assert result["human_gold_scientific_precision"] is None
    assert result["scientific_recall"] is None
    assert result["holdout_requests"] == 0
    assert result["human_approval_created"] is False
    for request in result["requests"]:
        assert sha256((tmp_path / "output" / request["response_file"]).read_bytes()) == request["http_body_sha256"]


def test_discovery_does_not_automatically_select_candidate(review, registry, tmp_path):
    fetch, calls = transport(registry)
    result = diagnose_development_inputs(review, tmp_path / "output", "http://unused",
        expected_release_id="actual", software_version="test", transport=fetch)
    assert len(calls) == 3
    assert result["outcomes"][0]["explicit_selection_traces"] == []
    assert result["outcomes"][0]["registered_exposure_candidates"][0]["id"] == registry["id"]


def test_rejects_unknown_explicit_id(review, registry, tmp_path):
    fetch, calls = transport(registry)
    with pytest.raises(ValueError, match="absent"):
        diagnose_development_inputs(review, tmp_path / "output", "http://unused",
            expected_release_id="actual", software_version="test", selected_exposure_ids=("invented",), transport=fetch)
    assert len(calls) == 2


def test_release_mismatch_stops_before_discovery(review, registry, tmp_path):
    fetch, calls = transport(registry, release="wrong")
    with pytest.raises(ValueError, match="Live release"):
        diagnose_development_inputs(review, tmp_path / "output", "http://unused",
            expected_release_id="actual", software_version="test", transport=fetch)
    assert len(calls) == 1


def test_related_food_is_not_exact_original_exposure(review, registry, tmp_path):
    fetch, _ = transport(registry, exact=False)
    result = diagnose_development_inputs(review, tmp_path / "output", "http://unused",
        expected_release_id="actual", software_version="test", selected_exposure_ids=(registry["id"],), transport=fetch)
    assert result["outcomes"][0]["explicit_selection_traces"][0]["exact_record_trace"] is False


def test_cannot_overwrite_old_receipts(review, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError, match="Preserve"):
        diagnose_development_inputs(review, output, "http://unused", expected_release_id="actual", software_version="test")
