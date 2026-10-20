"""Clinical co-navigation must not be described as missing or causal."""
import json
import sqlite3

import pytest
from test_explorer_api import explorer as explorer  # noqa: F401
from test_explorer_scientific_api import _science


@pytest.mark.parametrize("connected", [set(), {"T1"}, {"T1", "T2"}])
def test_matching_gaps_follow_returned_clinical_context(explorer, monkeypatch, connected):
    from nutrigenomics import explorer_scientific

    _science(explorer)
    with sqlite3.connect(explorer["db"]) as db:
        original = json.loads(db.execute(
            "SELECT metadata_json FROM science_record WHERE kind='exposure'"
        ).fetchone()[0])
        second = {**original, "id": "second-yogurt", "study_id": "T2"}
        for kind, value in [("study", {"id": "T2", "title": "Second trial"}), ("exposure", second)]:
            db.execute("INSERT INTO science_record VALUES (?,?,?)", (value["id"], kind, json.dumps(value)))

    def clinical_context(_db, study_id):
        if study_id not in connected:
            return []
        return [{"id": "clinical:" + study_id, "study_id": study_id, "effect": 0,
                 "ci_low": None, "ci_high": None, "effect_unit": None,
                 "expert_review_status": "pending", "context": {"comparison": "baseline_summary"}}]

    monkeypatch.setattr(explorer_scientific, "_clinical_results", clinical_context)
    response = explorer["client"].post("/api/v1/explorer/match", json={"seed_id": "food:rice", "intake_g": 150})
    assert response.status_code == 200
    result = response.json()
    summary = result["clinical_context_summary"]
    assert summary["studies_with_connected_clinical_evidence"] == sorted(connected)
    assert summary["studies_without_connected_clinical_evidence"] == sorted({"T1", "T2"} - connected)
    assert summary["mediation_established"] is False
    assert len(result["observed_evidence"]) == 2
    for card in result["observed_evidence"]:
        assert card["clinical_results"] == clinical_context(None, card["study_id"])
        assert card["study_evidence"]["expert_review_status"] == "pending"
        assert card["causal_inference"] is False
    gaps = " ".join(result["evidence_gaps"])
    assert "수치형 임상 결과가 연결되어 있지 않습니다" not in gaps
    if connected:
        assert "임상 근거가 연결되어 있지 않습니다" not in gaps
        assert "매개했다는 증거가 아닙니다" in gaps
        assert ("일부 연구에만" in gaps) == (connected != {"T1", "T2"})
    else:
        assert "미연결은 임상 지표 미측정이나 효과 0을 뜻하지 않으며" in gaps

