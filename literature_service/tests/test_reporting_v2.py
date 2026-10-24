"""Frozen engine-specific reports from synthetic records, not domain evaluation."""

import csv
import json
from pathlib import Path

import pytest
import test_api as api_fixtures
import test_api_v2 as fixtures

from nutri_literature.db import connect, dumps
from nutri_literature.reporting import generate_report

workspace = fixtures.workspace
v2_workspace = fixtures.v2_workspace
START = "2026-01-01T00:00:00+00:00"
END = "2100-01-01T00:00:00+00:00"


def snapshot(settings, report):
    with connect(settings) as con:
        return json.loads(con.execute("SELECT snapshot_json FROM reports WHERE report_id=?", (report["report_id"],)).fetchone()[0])


def test_report_selects_requested_engine_despite_newer_other_engine(v2_workspace):
    client, settings = v2_workspace
    v1 = generate_report(settings, START, END)
    assert v1["engine_version"] == "heuristic-v1"
    assert v1["counts"]["summarized_papers"] == 3
    first = snapshot(settings, v1)
    paper = next(p for p in first["papers"] if p["paper_id"] == 1)
    assert paper["extraction"]["engine_version"] == "heuristic-v1"
    assert paper["assertions"][0]["assertion_id"] == "assertion-1"
    v2 = generate_report(settings, START, END, engine="heuristic-v2")
    second = snapshot(settings, v2)
    assert second["engine_version"] == "heuristic-v2"
    assert v2["counts"]["summarized_papers"] == 1
    assert v2["counts"]["relation_candidates"] == 1
    assert v2["counts"]["abstained_events"] == 1
    assert second["engine_counts"]["heuristic-v1"]["summarized_papers"] == 3
    assert second["engine_counts"]["heuristic-v2"]["summarized_papers"] == 1
    assert second["quality"]["release_status"] == "candidate_beta"
    assert second["quality"]["domain_accuracy"] == "not_measured"
    assert all(second["quality"][metric] is None for metric in ("precision", "recall", "f1"))
    assert second["papers"][0]["engine_comparison"]["heuristic-v2"]["manifest"]["parser"]["model_sha256"] == "a" * 64
    with pytest.raises(ValueError, match="Unknown"):
        generate_report(settings, START, END, engine="unknown")


def test_v1_approval_does_not_transfer_and_v2_correction_is_exported(v2_workspace):
    client, settings = v2_workspace
    headers = api_fixtures.login(client)
    assert client.post("/api/v1/reviews", headers=headers, json={"assertion_id": "assertion-1", "decision": "approved"}).status_code == 200
    assert generate_report(settings, START, END)["counts"]["approved_relations"] == 1
    assert generate_report(settings, START, END, engine="heuristic-v2")["counts"]["approved_relations"] == 0
    correction = {"subject": "omega-3", "predicate": "decreases", "object": "HGNC:7876",
                  "speculation": True, "significance": "not_significant"}
    assert client.post("/api/v1/reviews", headers=headers, json={
        "assertion_id": "assertion-v2", "decision": "corrected", "comment": "Synthetic semantic correction",
        "corrected_payload": correction,
    }).status_code == 200
    report = generate_report(settings, START, END, engine="heuristic-v2")
    frozen = snapshot(settings, report)
    assert report["counts"]["approved_relations"] == 1
    assert report["counts"]["review_decisions"] == 1
    assertion = frozen["papers"][0]["assertions"][0]
    assert assertion["effective_event"]["speculation"] is True
    assert assertion["effective_event"]["statistical_significance"] == "not_significant"
    assert json.loads(assertion["payload"])["event"]["speculation"] is False
    for kind in ("markdown", "html"):
        contents = Path(report["paths"][kind]).read_text(encoding="utf-8")
        assert "heuristic-v1:" in contents and "heuristic-v2:" in contents
        assert "not_significant" in contents and "후보 검수용 베타" in contents
        assert "en_core_sci_sm" in contents
    with Path(report["paths"]["csv"]).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    relation = next(row for row in rows if row["record_type"] == "relation_candidate")
    assert relation["speculation"] == "True" and relation["significance"] == "not_significant"
    assert relation["model_sha256"] == "a" * 64
    assert json.loads(relation["engine_counts"])["heuristic-v2"]["approved_relations"] == 1
    assert relation["release_status"] == "candidate_beta"


@pytest.mark.parametrize("change", ["model", "rules", "source", "event"])
def test_v2_staleness_invalidates_current_approval_and_preserves_previous_files(v2_workspace, change):
    client, settings = v2_workspace
    assert client.post("/api/v1/reviews", headers=api_fixtures.login(client), json={
        "assertion_id": "assertion-v2", "decision": "approved",
    }).status_code == 200
    first = generate_report(settings, START, END, engine="heuristic-v2")
    before = {kind: Path(path).read_bytes() for kind, path in first["paths"].items()}
    if change == "model":
        artifact = settings.home / "engine-artifacts" / "heuristic-v2.json"
        artifact.parent.mkdir()
        artifact.write_text(dumps({"model_sha256": "b" * 64}), encoding="utf-8")
    else:
        with connect(settings) as con:
            if change == "source":
                con.execute("UPDATE sources SET text_content='Changed synthetic source' WHERE paper_id=1")
            elif change == "event":
                payload = json.loads(con.execute("SELECT payload FROM assertions WHERE assertion_id='assertion-v2'").fetchone()[0])
                payload["event"]["evidence_quotes"][0]["quote"] = "Unanchored synthetic text"
                con.execute("UPDATE assertions SET payload=? WHERE assertion_id='assertion-v2'", (dumps(payload),))
            else:
                result = json.loads(con.execute("SELECT result_json FROM extractions WHERE extraction_id='extraction-v2'").fetchone()[0])
                result["rules_version"] = "retired-rules"
                con.execute("UPDATE extractions SET result_json=? WHERE extraction_id='extraction-v2'", (dumps(result),))
    second = generate_report(settings, START, END, engine="heuristic-v2")
    assert second["counts"]["approved_relations"] == 0
    assert snapshot(settings, second)["papers"][0]["assertions"][0]["display_status"] == "needs_review"
    assert {kind: Path(path).read_bytes() for kind, path in first["paths"].items()} == before


def test_other_engine_only_review_does_not_enter_selected_engine_activity(v2_workspace):
    client, settings = v2_workspace
    headers = api_fixtures.login(client)
    with connect(settings) as con:
        for table, fields in (("papers", ("created_at", "updated_at")), ("sources", ("retrieved_at",)), ("extractions", ("created_at",))):
            for field in fields:
                con.execute(f"UPDATE {table} SET {field}='2025-01-01T00:00:00+00:00'")
    assert client.post("/api/v1/reviews", headers=headers, json={"assertion_id": "assertion-v2", "decision": "approved"}).status_code == 200
    assert generate_report(settings, START, END, engine="heuristic-v1")["counts"]["papers_in_report"] == 0
    v2 = generate_report(settings, START, END, engine="heuristic-v2")
    assert v2["counts"]["papers_in_report"] == v2["counts"]["review_decisions"] == 1
