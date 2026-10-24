"""Synthetic stored-event tests; these do not measure scientific accuracy."""

import copy
import json

import pytest
import test_api as api_fixtures

from nutri_literature.db import connect, digest, dumps, now
from nutri_literature.engines import engine_rules_version
from nutri_literature.review_eligibility import event_review_reasons

workspace = api_fixtures.workspace


@pytest.fixture
def v2_workspace(workspace):
    client, settings = workspace
    with connect(settings) as con:
        source = con.execute("SELECT * FROM sources WHERE paper_id=1").fetchone()
        text = source["text_content"]
        def mention(term, normalized=None):
            start = text.index(term)
            return {"text": term, "start": start, "end": start + len(term), "normalized_id": normalized}
        event = {
            "event_id": "synthetic-event-v2", "subject": "omega-3", "object": "HGNC:7876",
            "controller": mention("omega-3"), "theme": mention("NOS3", "HGNC:7876"),
            "trigger": {**mention("decreased"), "lemma": "decrease"},
            "outcome_type": "expression", "direction": "decrease", "negation": False,
            "speculation": False, "significance": "unknown", "statistical_significance": "unknown",
            "polarity": "positive", "species": "human", "study_model": "clinical",
            "conditions": {"arm_id": None, "comparator": None, "timepoint": None, "quantities": []},
            "evidence_quotes": [{"quote": text, "start": 0, "end": len(text)}],
            "status": "candidate", "triage_reasons": ["comparator_unknown"], "abstention_reasons": [],
        }
        abstention = {**copy.deepcopy(event), "event_id": "synthetic-abstained-v2", "status": "abstained",
                      "abstention_reasons": ["ambiguous_roles"]}
        result = {
            "engine_version": "heuristic-v2", "rules_version": engine_rules_version("heuristic-v2"),
            "summary": {"results": [{"text": text, "start": 0, "end": len(text)}]},
            "manifest": {"parser": {
                "available": True, "mode": "production", "name": "en_core_sci_sm", "version": "0.5.4",
                "spacy_version": "3.7.5", "scispacy_version": "0.6.2", "model_sha256": "a" * 64,
            }}, "abstentions": [abstention],
        }
        con.execute("INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?)", (
            "extraction-v2", 1, source["source_id"], digest(text), "heuristic-v2", "synthetic-dictionary-v2",
            dumps(result), now(), 1,
        ))
        payload = {"explicit_relation": True, "event": event,
                   "subject_mention": event["controller"], "object_mention": event["theme"]}
        con.execute("INSERT INTO assertions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "assertion-v2", "extraction-v2", 1, digest(dumps(payload)), "omega-3", "decreases", "HGNC:7876",
            text, 0, len(text), "synthetic-v2-rule", "candidate", dumps(payload),
        ))
    return client, settings


def test_engine_selection_and_event_provenance_are_separate(v2_workspace):
    client, _ = v2_workspace
    assert client.get("/api/v1/engines").json()["default"] == "heuristic-v1"
    assert client.get("/api/v1/evidence-map").json()["edges"][0]["assertion_id"] == "assertion-1"
    v2 = client.get("/api/v1/evidence-map?engine=heuristic-v2").json()
    assert v2["total"] == 1 and v2["edges"][0]["assertion_id"] == "assertion-v2"
    assert client.get("/api/v1/papers/1?engine=heuristic-v2").json()["extractions"][0]["engine_version"] == "heuristic-v2"
    assert client.get("/api/v1/papers?engine=bogus").status_code == 422
    events = client.get("/api/v1/events?engine=heuristic-v2").json()
    assert events["total"] == 2 and events["quality_status"] == "candidate_beta"
    assert len({event["event_id"] for event in events["items"]}) == 2
    quote = events["items"][0]["event"]["evidence_quotes"][0]
    assert quote["quote"][quote["start"]:quote["end"]] == quote["quote"]
    assert client.get("/api/v1/events?engine=heuristic-v2&status=abstained&reason=ambiguous_roles").json()["total"] == 1
    assert client.get("/api/v1/events?engine=heuristic-v2&status=abstained&offset=1").json()["items"] == []
    assert client.get("/api/v1/events?engine=heuristic-v2&reason=unrelated").json()["total"] == 0
    runs = client.get("/api/v1/runs?engine=heuristic-v2").json()
    assert runs["counts"]["summarized_papers"] == 1
    assert runs["engine_counts"]["heuristic-v1"]["summarized_papers"] == 3
    assert runs["engine_counts"]["heuristic-v2"]["abstained_events"] == 1


def test_v2_review_correction_preserves_source_and_original_event(v2_workspace):
    client, settings = v2_workspace
    response = client.post("/api/v1/reviews", headers=api_fixtures.login(client), json={
        "assertion_id": "assertion-v2", "decision": "corrected", "comment": "Synthetic semantic correction",
        "corrected_payload": {"subject": "omega-3", "predicate": "decreases", "object": "HGNC:7876",
                              "speculation": True, "significance": "not_significant"},
    })
    assert response.status_code == 200
    edge = client.get("/api/v1/evidence-map?engine=heuristic-v2&status=approved").json()["edges"][0]
    assert edge["effective_event"]["speculation"] is True
    assert edge["payload"]["event"]["speculation"] is False
    with connect(settings) as con:
        saved = json.loads(con.execute("SELECT payload FROM assertions WHERE assertion_id='assertion-v2'").fetchone()[0])
        assert saved["event"]["speculation"] is False
    assert client.get("/api/v1/evidence-map?engine=heuristic-v1&status=approved").json()["total"] == 0


@pytest.mark.parametrize("change", ["span", "role", "flag", "abstention", "parser"])
def test_v2_approval_fails_closed_for_invalid_events_or_parser(v2_workspace, change):
    client, settings = v2_workspace
    with connect(settings) as con:
        if change == "parser":
            result = json.loads(con.execute("SELECT result_json FROM extractions WHERE extraction_id='extraction-v2'").fetchone()[0])
            result["manifest"]["parser"]["model_sha256"] = "unrecorded"
            con.execute("UPDATE extractions SET result_json=? WHERE extraction_id='extraction-v2'", (dumps(result),))
        else:
            payload = json.loads(con.execute("SELECT payload FROM assertions WHERE assertion_id='assertion-v2'").fetchone()[0])
            if change == "span":
                payload["event"]["evidence_quotes"][0]["quote"] = "Invented synthetic evidence"
            elif change == "role":
                payload["event"]["subject"] = "Another entity"
            elif change == "flag":
                payload["event"]["negation"] = "false"
            else:
                payload["event"]["abstention_reasons"] = ["ambiguous_roles"]
            con.execute("UPDATE assertions SET payload=? WHERE assertion_id='assertion-v2'", (dumps(payload),))
    assert client.post("/api/v1/reviews", headers=api_fixtures.login(client), json={
        "assertion_id": "assertion-v2", "decision": "approved",
    }).status_code == 409
    assert client.get("/api/v1/evidence-map?engine=heuristic-v2&status=approved").json()["total"] == 0


def test_v2_invalid_conditions_and_source_edits_cannot_be_corrections(v2_workspace):
    client, _ = v2_workspace
    headers = api_fixtures.login(client)
    base = {"subject": "omega-3", "predicate": "decreases", "object": "HGNC:7876"}
    for invalid in ({"quote": "New source"}, {"negation": "false"}, {"direction": "unrecognized"}, {"significance": {}},
                    {"conditions": {"comparator": {"text": "placebo", "start": 0, "end": 7}}}):
        assert client.post("/api/v1/reviews", headers=headers, json={
            "assertion_id": "assertion-v2", "decision": "corrected", "comment": "Synthetic correction",
            "corrected_payload": {**base, **invalid},
        }).status_code == 422


def test_background_events_are_visible_without_approvable_assertions(v2_workspace):
    client, settings = v2_workspace
    with connect(settings) as con:
        payload = json.loads(con.execute("SELECT payload FROM assertions WHERE assertion_id='assertion-v2'").fetchone()[0])
        background = {**copy.deepcopy(payload["event"]), "event_id": "synthetic-background-v2", "status": "background",
                      "abstention_reasons": ["background_or_prior_statement"]}
        result = json.loads(con.execute("SELECT result_json FROM extractions WHERE extraction_id='extraction-v2'").fetchone()[0])
        result["events"] = [payload["event"], background]
        con.execute("UPDATE extractions SET result_json=? WHERE extraction_id='extraction-v2'", (dumps(result),))
    events = client.get("/api/v1/events?engine=heuristic-v2&status=background").json()
    assert events["total"] == 1 and events["items"][0]["assertion_id"] is None
    assert "event_abstained" in events["items"][0]["review_eligibility_reasons"]
    assert client.get("/api/v1/events?engine=heuristic-v2&status=candidate").json()["total"] == 1
    assert client.get("/api/v1/events?engine=heuristic-v2&status=all").json()["total"] == 3


def test_legacy_payload_does_not_require_event_fields():
    assert event_review_reasons({"explicit_relation": True}, "synthetic", "heuristic-v1") == []
