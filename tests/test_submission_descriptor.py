"""Manuscript counts and review semantics follow the supplied release, not old drafts."""
import json

from scripts.write_public_data_descriptor import write


def test_descriptor_uses_current_counts_and_never_turns_review_tasks_into_gold(tmp_path):
    metadata = {"release_id": "actual-current-public", "counts": {
        "node_kinds": {"food": 5794}, "composition_backed_foods": 5793, "measurement": 368208, "edge": 1456072, "effect": 6254477,
        "by_tier": {"predicted": 1073409}, "science_kinds": {
            "study": 9, "exposure": 19, "contrast": 53, "assertion": 94, "analysis_run": 14,
            "clinical_endpoint_result": 44, "clinical_study_audit": 9}}}
    review = {"records": 263, "primary_paper_fact_candidates": 200, "expert_verified": 0}
    mp, rp, output = tmp_path / "metadata.json", tmp_path / "review.json", tmp_path / "draft.md"
    mp.write_text(json.dumps(metadata), encoding="utf-8")
    rp.write_text(json.dumps(review), encoding="utf-8")
    result = write(mp, rp, output)
    text = output.read_text(encoding="utf-8")
    assert result["submission_ready"] is False and result["release_id"] == "actual-current-public"
    assert "53 contrasts" in text and "| Analysis records | 14 |" in text
    assert "| Clinical summary candidates | 44 |" in text
    assert "| Composition-linked foods | 5,793 |" in text
    assert "| Foods | 5,794 |" in text
    assert "263 record-level review tasks" in text
    assert "0 record-level tasks have recorded human approval" in text
    assert "Pending tasks do not represent completed decisions or gold labels" in text
    assert "source reproducibility" in text and "not substitutes for the final validation package" in text
    assert "standard error of paired delta-beta summaries" in text
    assert "Fixed 1 kb region tests remain distinct from discovered or validated DMRs" in text
    assert "final scientific retrieval accuracy is currently not evaluated" in text.lower()


def test_descriptor_does_not_hardcode_nine_studies_or_previous_analysis_counts(tmp_path):
    metadata = {"release_id": "different-release", "counts": {
        "node_kinds": {"food": 2}, "measurement": 4, "edge": 7, "effect": 11,
        "science_kinds": {"study": 2, "exposure": 3, "contrast": 5, "assertion": 6, "analysis_run": 4}}}
    review = {"records": 20, "fact_candidates": 12, "expert_verified": 0}
    mp, rp, output = tmp_path / "m.json", tmp_path / "r.json", tmp_path / "d.md"
    mp.write_text(json.dumps(metadata), encoding="utf-8")
    rp.write_text(json.dumps(review), encoding="utf-8")
    write(mp, rp, output)
    text = output.read_text(encoding="utf-8")
    assert "from the 2 study records" in text
    assert "| Analysis records | 4 |" in text
    assert "| Clinical summary candidates | 0 |" in text
    assert "61 contrasts" not in text and "17 analysis" not in text
