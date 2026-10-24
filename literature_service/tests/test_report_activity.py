"""Recorded activity integration checks on synthetic evidence, never human gold."""

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from io import StringIO

import pytest
import test_api as api_fixtures
import test_reporting as report_fixtures

from nutri_literature.db import connect, digest
from nutri_literature.reporting import generate_report

settings = report_fixtures.settings
workspace = api_fixtures.workspace
START = "2026-09-30T00:00:00+00:00"
END = "2026-10-02T00:00:00+00:00"
OLD = "2026-09-01T00:00:00+00:00"
DURING = "2026-10-01T00:00:00+00:00"


def _old(settings):
    with connect(settings) as con:
        con.execute("UPDATE papers SET created_at=?,updated_at=?", (OLD, OLD))
        con.execute("UPDATE sources SET retrieved_at=?", (OLD,))
        con.execute("UPDATE extractions SET created_at=?", (OLD,))
        con.execute("UPDATE reviews SET created_at=?", (OLD,))


def _event(settings, kind, when):
    statements = {
        "created": "UPDATE papers SET created_at=?",
        "updated": "UPDATE papers SET updated_at=?",
        "source": "UPDATE sources SET retrieved_at=?",
        "extraction": "UPDATE extractions SET created_at=?",
        "review": "UPDATE reviews SET created_at=?",
    }
    with connect(settings) as con:
        con.execute(statements[kind], (when,))


def _report(settings):
    return generate_report(settings, START, END)


def _snapshot(settings, report):
    with connect(settings) as con:
        row = con.execute("SELECT snapshot_json FROM reports WHERE report_id=?", (report["report_id"],)).fetchone()
    return json.loads(row[0])


@pytest.mark.parametrize("kind", ["review", "extraction", "source"])
def test_old_paper_with_current_recorded_activity_is_included_without_rewriting_dates(settings, kind):
    _old(settings)
    _event(settings, kind, DURING)
    report = _report(settings)
    assert report["counts"]["papers_in_report"] == 1
    assert report["counts"]["new_papers"] == 0
    assert report["counts"]["changed_papers"] == 1
    assert report["counts"]["review_decisions"] == int(kind == "review")
    paper = _snapshot(settings, report)["papers"][0]
    assert paper["created_at"] == paper["updated_at"] == OLD
    with connect(settings) as con:
        row = con.execute("SELECT created_at,updated_at FROM papers WHERE paper_id=1").fetchone()
    assert tuple(row) == (OLD, OLD)


@pytest.mark.parametrize("kind", ["created", "updated", "source", "extraction", "review"])
@pytest.mark.parametrize("when,included", [
    ("2026-09-29T23:59:59.999999+00:00", False), (START, True), (DURING, True), (END, False),
])
def test_activity_window_is_start_inclusive_and_end_exclusive(settings, kind, when, included):
    _old(settings)
    _event(settings, kind, when)
    report = _report(settings)
    assert report["counts"]["papers_in_report"] == int(included)
    assert report["counts"]["review_decisions"] == int(kind == "review" and included)


def test_multiple_events_select_once_and_count_only_period_reviews(settings):
    _old(settings)
    for kind in ("source", "extraction", "review"):
        _event(settings, kind, DURING)
    with connect(settings) as con:
        for number, when in enumerate((OLD, START, END)):
            con.execute(
                "INSERT INTO reviews VALUES(?, 'a','reviewer','approved','',NULL,'fp',?)",
                (f"synthetic-extra-{number}", when),
            )
    report = _report(settings)
    assert report["counts"]["papers_in_report"] == 1
    assert report["counts"]["review_decisions"] == 2
    snapshot = _snapshot(settings, report)
    assert snapshot["schema_version"] == "literature-report-v2"
    assert len(snapshot["papers"][0]["assertions"][0]["reviews"]) == 4
    events = snapshot["papers"][0]["period_activity"]["reviews"]
    assert {r["review_id"] for r in events} == {"baseline-review", "synthetic-extra-1"}
    assert all(START <= r["created_at"] < END for r in events)
    assert len(snapshot["papers"][0]["period_activity"]["sources"]) == 1
    assert len(snapshot["papers"][0]["period_activity"]["extractions"]) == 1


def test_review_of_inactive_assertion_retains_its_original_source_identity(settings):
    _old(settings)
    old_text = "Vitamin D inhibits VDR. Synthetic former source."
    old_hash = digest(old_text)
    with connect(settings) as con:
        con.execute(
            "INSERT INTO sources SELECT 'old-source',paper_id,kind,?,?,'synthetic-old.xml',?,"
            "'[]',url,license,? FROM sources WHERE source_id='source'", (old_hash, old_hash, old_text, OLD),
        )
        con.execute(
            "INSERT INTO extractions SELECT 'old-extract',paper_id,'old-source',?,engine_version,"
            "dictionary_version,result_json,created_at,0 FROM extractions WHERE extraction_id='extract'", (old_hash,),
        )
        con.execute(
            "INSERT INTO assertions SELECT 'old-assertion','old-extract',paper_id,'old-fp',subject,predicate,"
            "object,quote,start,end,rule_id,'candidate',payload FROM assertions WHERE assertion_id='a'"
        )
        con.execute(
            "INSERT INTO reviews VALUES('synthetic-inactive-review','old-assertion','reviewer','rejected','',NULL,'old-fp',?)",
            (DURING,),
        )
    report = _report(settings)
    assert report["counts"]["papers_in_report"] == 1
    assert report["counts"]["review_decisions"] == 1
    paper = _snapshot(settings, report)["papers"][0]
    assert [a["assertion_id"] for a in paper["assertions"]] == ["a"]
    event = paper["period_activity"]["reviews"][0]
    assert event["assertion_id"] == "old-assertion" and event["extraction_id"] == "old-extract"
    assert event["source_id"] == "old-source" and event["source_hash"] == old_hash
    assert event["source_hash"] != paper["extraction"]["source_hash"]
    assert event["fingerprint"] == "old-fp" and event["decision"] == "rejected"


def test_new_paper_updated_after_period_remains_in_new_paper_export(settings):
    _old(settings)
    _event(settings, "created", DURING)
    _event(settings, "updated", END)
    report = _report(settings)
    assert report["counts"]["new_papers"] == report["counts"]["papers_in_report"] == 1
    assert report["counts"]["changed_papers"] == 0


def test_snapshot_is_current_at_generation_and_previous_files_stay_frozen(settings):
    _old(settings)
    _event(settings, "review", DURING)
    first = _report(settings)
    before = {kind: Path(path).read_bytes() for kind, path in first["paths"].items()}
    snapshot_path = Path(first["paths"]["html"]).parent / "snapshot.json"
    frozen_json = snapshot_path.read_bytes()
    with connect(settings) as con:
        frozen_row = dict(con.execute("SELECT * FROM reports WHERE report_id=?", (first["report_id"],)).fetchone())
    with connect(settings) as con:
        con.execute("UPDATE assertions SET status='rejected' WHERE assertion_id='a'")
        con.execute(
            "INSERT INTO reviews VALUES('synthetic-after-end','a','reviewer','rejected','',NULL,'fp',?)", (END,)
        )
    second = _report(settings)
    assert second["counts"]["review_decisions"] == 1
    snapshot = _snapshot(settings, second)
    assert snapshot["temporal_basis"]["state"] == "current_saved_state_at_generation"
    assert snapshot["papers"][0]["assertions"][0]["display_status"] == "rejected"
    assert len(snapshot["papers"][0]["period_activity"]["reviews"]) == 1
    for kind in ("markdown", "html"):
        text = Path(second["paths"][kind]).read_text(encoding="utf-8")
        assert snapshot["temporal_basis"]["note"] in text
        assert "baseline-review" in text and "synthetic-after-end" not in text
    assert {kind: Path(path).read_bytes() for kind, path in first["paths"].items()} == before
    assert snapshot_path.read_bytes() == frozen_json
    with connect(settings) as con:
        assert dict(con.execute("SELECT * FROM reports WHERE report_id=?", (first["report_id"],)).fetchone()) == frozen_row
    assert digest(frozen_row["snapshot_json"]) == frozen_row["snapshot_hash"]
    assert snapshot["quality"]["status"] == "not_evaluated"
    assert all(snapshot["quality"][metric] is None for metric in ("precision", "recall", "f1"))


@pytest.mark.parametrize("decision", ["approved", "corrected", "rejected"])
def test_api_review_on_old_paper_appears_in_activity_report_without_metadata_update(workspace, decision):
    client, settings = workspace
    _old(settings)
    current = datetime.now(UTC)
    body = {"assertion_id": "assertion-1", "decision": decision}
    if decision == "corrected":
        body.update(comment="Synthetic correction only", corrected_payload={
            "subject": "omega-3", "predicate": "affects", "object": "HGNC:7876",
        })
    response = client.post("/api/v1/reviews", headers=api_fixtures.login(client), json=body)
    assert response.status_code == 200
    report = generate_report(settings, (current - timedelta(minutes=1)).isoformat(),
                             (current + timedelta(minutes=1)).isoformat())
    assert report["counts"]["papers_in_report"] == 1
    assert report["counts"]["review_decisions"] == 1
    assert report["counts"]["approved_relations"] == int(decision != "rejected")
    snapshot = _snapshot(settings, report)
    assert snapshot["papers"][0]["period_activity"]["reviews"][0]["decision"] == decision
    assert snapshot["papers"][0]["updated_at"] == OLD
    listing = client.get("/api/v1/reports").json()["items"]
    registered = next(item for item in listing if item["report_id"] == report["report_id"])
    assert registered["snapshot"]["schema_version"] == "literature-report-v2"
    download = client.get(f"/api/v1/reports/{report['report_id']}/download/csv")
    assert download.status_code == 200 and download.content == Path(report["paths"]["csv"]).read_bytes()
    rows = list(csv.DictReader(StringIO(download.text.lstrip("\ufeff"))))
    assert len(rows) == 2
    assert all(row["state_time_basis"] == "current_saved_state_at_generation" for row in rows)
    assert all(row["activity_period_start"] == snapshot["period_start"] for row in rows)
    assert all(row["activity_period_end"] == snapshot["period_end"] for row in rows)


def test_recorded_review_activity_does_not_promote_nonexplicit_historical_approval(settings):
    _old(settings)
    _event(settings, "review", DURING)
    with connect(settings) as con:
        con.execute("UPDATE assertions SET payload=? WHERE assertion_id='a'", ('{"explicit_relation":"false"}',))
    report = _report(settings)
    assert report["counts"]["papers_in_report"] == report["counts"]["review_decisions"] == 1
    assert report["counts"]["approved_relations"] == 0
    assert _snapshot(settings, report)["papers"][0]["assertions"][0]["display_status"] == "needs_review"
