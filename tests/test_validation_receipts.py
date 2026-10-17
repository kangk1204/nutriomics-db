import copy

import pytest

from nutrigenomics.validation_receipts import valid_receipt


def receipt(name, metrics):
    return {"schema_version": 1, "check_type": name, "release_id": "r",
            "database_sha256": "a" * 64, "code_sha256": "b" * 64,
            "verified_at": "2026-10-17T00:00:00+00:00", "passed": True, "metrics": metrics}


def valid(r):
    return valid_receipt(r, r["check_type"], "r", "a" * 64, 6254477, "b" * 64)


def test_effect_preservation_requires_complete_denominator_and_source_comparison():
    r = receipt("effect_preservation", {"checked_rows": 6254477, "mismatches": 0, "source_compared": True})
    assert valid(r)
    for changed in [{"checked_rows": 100}, {"mismatches": 1}, {"source_compared": False}, {"mismatches": False}]:
        assert not valid({**r, "metrics": r["metrics"] | changed})
    assert not valid({**r, "database_sha256": "c" * 64})
    assert not valid({**r, "check_type": "clean_reproduction"})


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, 1.1])
def test_p95_is_recomputed_from_100_valid_raw_samples(duration):
    sample = {"status": 200, "release_id": "r", "seconds": duration}
    r = receipt("warm100", {"all_frozen_cases_present": True, "cases": [
        {"id": "one", "kind": "general", "samples": [sample] * 100}]})
    r["case_sha256"] = "d" * 64
    assert not valid(r)


def test_actual_browser_and_all_four_offline_actions_are_required():
    r = receipt("offline_html", {"actual_browser": True, "network_blocked": True,
                                "external_requests": 0, "search": True, "selection": True,
                                "zoom": True, "evidence": True})
    assert valid(r)
    assert not valid({**r, "metrics": r["metrics"] | {"actual_browser": False}})
    assert not valid({**r, "metrics": r["metrics"] | {"external_requests": 1}})


def test_all_14_analysis_runs_must_have_real_execution_and_comparison_status():
    runs = [{"series_accession": "GSE1", "analysis_version": str(i), "executed": True,
             "comparison_passed": True, "unexplained_differences": 0} for i in range(14)]
    r = receipt("clean_reproduction", {"public_files_only_service_build": True, "analyses": runs})
    assert valid(r)
    changed = copy.deepcopy(r)
    changed["metrics"]["analyses"][3]["executed"] = False
    assert not valid(changed)
