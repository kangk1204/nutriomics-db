"""Synthetic approval policy checks; these are not expert accuracy measurements."""

import json
from pathlib import Path

import pytest
import test_api as api_fixtures

from nutri_literature.db import connect, now
from nutri_literature.reporting import generate_report

workspace = api_fixtures.workspace

INVALID_PAYLOADS = [
    pytest.param("{}", id="missing"),
    pytest.param('{"explicit_relation":false}', id="boolean_false"),
    pytest.param('{"explicit_relation":null}', id="null_flag"),
    pytest.param('{"explicit_relation":0}', id="zero"),
    pytest.param('{"explicit_relation":1}', id="integer_one"),
    pytest.param('{"explicit_relation":1.0}', id="float_one"),
    pytest.param('{"explicit_relation":-1}', id="negative_integer"),
    pytest.param('{"explicit_relation":"false"}', id="string_false"),
    pytest.param('{"explicit_relation":"true"}', id="string_true"),
    pytest.param('{"explicit_relation":[true]}', id="list_flag"),
    pytest.param('{"explicit_relation":{"value":true}}', id="object_flag"),
    pytest.param("null", id="null_payload"),
    pytest.param("true", id="scalar_payload"),
    pytest.param("[true]", id="array_payload"),
    pytest.param("{broken-json", id="malformed_payload"),
    pytest.param(json.dumps('{"explicit_relation":true}'), id="nested_object_string"),
]


def _payload(settings, encoded):
    with connect(settings) as con:
        con.execute("UPDATE assertions SET payload=? WHERE assertion_id='assertion-1'", (encoded,))


def _review_body(decision):
    body = {"assertion_id": "assertion-1", "decision": decision}
    if decision == "corrected":
        body.update(
            comment="Synthetic source-anchored correction only",
            corrected_payload={"subject": "omega-3", "predicate": "affects", "object": "HGNC:7876"},
        )
    return body


def _history(settings):
    with connect(settings) as con:
        assertion = dict(con.execute("SELECT * FROM assertions WHERE assertion_id='assertion-1'").fetchone())
        reviews = [dict(row) for row in con.execute("SELECT * FROM reviews WHERE assertion_id='assertion-1'")]
    return assertion, reviews


def _report(settings):
    return generate_report(settings, "2026-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")


def _snapshot(settings, report):
    with connect(settings) as con:
        value = con.execute("SELECT snapshot_json FROM reports WHERE report_id=?", (report["report_id"],)).fetchone()[0]
    return json.loads(value)


@pytest.mark.parametrize("encoded", INVALID_PAYLOADS)
@pytest.mark.parametrize("decision", ["approved", "corrected"])
def test_nonboolean_payload_cannot_create_approved_review(workspace, encoded, decision):
    client, settings = workspace
    _payload(settings, encoded)
    before = _history(settings)
    response = client.post("/api/v1/reviews", headers=api_fixtures.login(client), json=_review_body(decision))
    assert response.status_code == 409
    assert _history(settings) == before


@pytest.mark.parametrize("encoded", INVALID_PAYLOADS)
def test_historical_approval_with_nonboolean_flag_is_not_displayed_or_counted(workspace, encoded):
    client, settings = workspace
    _payload(settings, encoded)
    with connect(settings) as con:
        con.execute("UPDATE assertions SET status='approved' WHERE assertion_id='assertion-1'")
        con.execute(
            "INSERT INTO reviews VALUES('synthetic-legacy-review','assertion-1','reviewer_a','approved','',NULL,?,?)",
            ("synthetic-fingerprint-1", now()),
        )
    before = _history(settings)
    detail = client.get("/api/v1/papers/1").json()["assertions"][0]
    assert detail["status"] == "approved"  # preserve historical data
    assert detail["display_status"] == "needs_review"
    assert client.get("/api/v1/evidence-map?status=approved").json()["total"] == 0
    report = _report(settings)
    assert report["counts"]["approved_relations"] == 0
    assert report["counts"]["review_decisions"] == 1
    snapshot = _snapshot(settings, report)
    assertion = next(p for p in snapshot["papers"] if p["paper_id"] == 1)["assertions"][0]
    assert assertion["display_status"] == "needs_review"
    assert snapshot["quality"]["status"] == "not_evaluated"
    assert all(snapshot["quality"][key] is None for key in ("precision", "recall", "f1"))
    assert _history(settings) == before


@pytest.mark.parametrize("decision", ["approved", "corrected"])
def test_literal_true_keeps_matching_approval_consistent_and_frozen(workspace, decision):
    client, settings = workspace
    response = client.post("/api/v1/reviews", headers=api_fixtures.login(client), json=_review_body(decision))
    assert response.status_code == 200
    detail = client.get("/api/v1/papers/1").json()["assertions"][0]
    assert detail["display_status"] == "approved"
    mapping = client.get("/api/v1/evidence-map?status=approved").json()
    assert mapping["total"] == 1 and mapping["edges"][0]["is_reviewed"] is True
    first = _report(settings)
    assert first["counts"]["approved_relations"] == 1
    snapshot = _snapshot(settings, first)
    assertion = next(p for p in snapshot["papers"] if p["paper_id"] == 1)["assertions"][0]
    assert assertion["quote"] == detail["quote"]
    assert assertion["effective"]["predicate"] == ("affects" if decision == "corrected" else "decreases")
    frozen = {kind: Path(path).read_bytes() for kind, path in first["paths"].items()}
    _payload(settings, '{"explicit_relation":"false"}')
    assert _report(settings)["counts"]["approved_relations"] == 0
    assert {kind: Path(path).read_bytes() for kind, path in first["paths"].items()} == frozen


@pytest.mark.parametrize("encoded", ["{}", '{"explicit_relation":"false"}', "true", "{broken-json"])
def test_rejection_of_nonexplicit_evidence_remains_available(workspace, encoded):
    client, settings = workspace
    _payload(settings, encoded)
    response = client.post("/api/v1/reviews", headers=api_fixtures.login(client), json=_review_body("rejected"))
    assert response.status_code == 200
    assertion, reviews = _history(settings)
    assert assertion["payload"] == encoded and assertion["status"] == "rejected"
    assert len(reviews) == 1 and reviews[0]["decision"] == "rejected"


def test_nonexplicit_candidate_remains_a_candidate_without_claiming_approval(workspace):
    client, settings = workspace
    _payload(settings, '{"explicit_relation":false}')
    detail = client.get("/api/v1/papers/1").json()["assertions"][0]
    assert detail["display_status"] == "candidate"
    mapping = client.get("/api/v1/evidence-map").json()
    assert mapping["total"] == 1 and mapping["edges"][0]["is_reviewed"] is False
    assert _report(settings)["counts"]["approved_relations"] == 0
