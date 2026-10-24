"""Lossless saved-read projections, large withheld pagination, and stale gates."""

import copy
import json

import pytest
import test_api as api_fixtures
import test_api_v2 as fixtures

from nutri_literature.db import connect, digest, dumps, initialize, now
from nutri_literature.engines import engine_rules_version

workspace = fixtures.workspace
v2_workspace = fixtures.v2_workspace


def populate(settings, count=1000):
    with connect(settings) as con:
        result = json.loads(con.execute("SELECT result_json FROM extractions WHERE extraction_id='extraction-v2'").fetchone()[0])
        event = copy.deepcopy(result["abstentions"][0])
        event.pop("event_id")
        result["abstentions"] = [{**event, "abstention_reasons": ["ambiguous_roles" if i % 2 == 0 else "species_unknown"]} for i in range(count)]
        result["events"] = [{**event, "status": "background", "abstention_reasons": ["background_or_prior_statement"]} for _ in range(count // 2)]
        con.execute("UPDATE extractions SET result_json=? WHERE extraction_id='extraction-v2'", (dumps(result),))


def test_large_withheld_pages_have_exact_counts_reasons_and_stable_ids(v2_workspace):
    client, settings = v2_workspace
    populate(settings)
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM withheld_events WHERE extraction_id='extraction-v2'").fetchone()[0] == 1500
    page = client.get("/api/v1/events?engine=heuristic-v2&status=abstained&reason=ambiguous_roles&offset=498&limit=5").json()
    assert page["total"] == 500
    assert [item["event_id"] for item in page["items"]] == ["extraction-v2:abstention:996", "extraction-v2:abstention:998"]
    across = client.get("/api/v1/events?engine=heuristic-v2&offset=1000&limit=3").json()
    assert across["total"] == 1501  # one assertion then 1000 withheld and 500 background
    assert [item["event_id"] for item in across["items"]] == ["extraction-v2:abstention:999", "extraction-v2:abstention:1000", "extraction-v2:abstention:1001"]
    assert client.get("/api/v1/events?engine=heuristic-v2&status=background&reason=background_or_prior_statement").json()["total"] == 500
    assert client.get("/api/v1/events?engine=heuristic-v2&reason=absent").json()["total"] == 0
    assert client.get("/api/v1/events?engine=heuristic-v2&offset=1501").json()["items"] == []


@pytest.mark.parametrize("change", ["dictionary", "model", "source", "rules", "inactive"])
def test_materialized_pages_and_run_counts_obey_current_integrity_gates(v2_workspace, change):
    client, settings = v2_workspace
    populate(settings, 200)
    assert client.get("/api/v1/events?engine=heuristic-v2").json()["total"] == 301
    if change == "model":
        artifact = settings.home / "engine-artifacts" / "heuristic-v2.json"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_text(dumps({"model_sha256": "b" * 64}), encoding="utf-8")
    else:
        with connect(settings) as con:
            if change == "dictionary":
                con.execute("INSERT INTO engine_profiles(engine_version,rules_version,dictionary_version,enabled,updated_at) VALUES(?,?,?,?,?)", ("heuristic-v2", engine_rules_version("heuristic-v2"), "another-dictionary", 1, now()))
            elif change == "source":
                con.execute("UPDATE sources SET text_content='Changed synthetic source' WHERE paper_id=1")
            elif change == "inactive":
                con.execute("UPDATE extractions SET active=0 WHERE extraction_id='extraction-v2'")
            else:
                result = json.loads(con.execute("SELECT result_json FROM extractions WHERE extraction_id='extraction-v2'").fetchone()[0])
                result["rules_version"] = "obsolete"
                con.execute("UPDATE extractions SET result_json=? WHERE extraction_id='extraction-v2'", (dumps(result),))
    assert client.get("/api/v1/events?engine=heuristic-v2").json()["total"] == 0
    assert client.get("/api/v1/evidence-map?engine=heuristic-v2").json()["total"] == 0
    assert client.get("/api/v1/runs?engine=heuristic-v2").json()["counts"]["summarized_papers"] == 0


def test_projection_updates_removes_old_reasons_and_backfills_idempotently(v2_workspace):
    client, settings = v2_workspace
    populate(settings, 100)
    with connect(settings) as con:
        result = json.loads(con.execute("SELECT result_json FROM extractions WHERE extraction_id='extraction-v2'").fetchone()[0])
        result["abstentions"] = []
        result["events"] = []
        con.execute("UPDATE extractions SET result_json=? WHERE extraction_id='extraction-v2'", (dumps(result),))
        assert con.execute("SELECT COUNT(*) FROM withheld_event_reasons WHERE extraction_id='extraction-v2'").fetchone()[0] == 0
        before = con.execute("SELECT created_at FROM extractions WHERE extraction_id='extraction-v2'").fetchone()[0]
        con.execute("DELETE FROM extraction_read_index WHERE extraction_id='extraction-v2'")
    initialize(settings)
    initialize(settings)
    assert client.get("/api/v1/events?engine=heuristic-v2").json()["total"] == 1
    with connect(settings) as con:
        assert con.execute("SELECT created_at FROM extractions WHERE extraction_id='extraction-v2'").fetchone()[0] == before
        assert con.execute("SELECT COUNT(*) FROM extraction_read_index WHERE extraction_id='extraction-v2'").fetchone()[0] == 1
        con.execute("UPDATE extractions SET result_json='{broken-json' WHERE extraction_id='extraction-v2'")
    assert client.get("/api/v1/events?engine=heuristic-v2").json()["total"] == 0


def test_source_proof_is_invalidated_by_every_text_write_and_never_repairs_claimed_hash(v2_workspace):
    client, settings = v2_workspace
    with connect(settings) as con:
        source = con.execute("SELECT source_id,text_content FROM sources WHERE paper_id=1").fetchone()
        assert con.execute("SELECT text_digest FROM source_read_index WHERE source_id=?", (source["source_id"],)).fetchone()[0] == digest(source["text_content"])
        con.execute("UPDATE sources SET text_content='Corrupted stored text' WHERE paper_id=1")
        assert con.execute("SELECT text_digest FROM source_read_index WHERE source_id=?", (source["source_id"],)).fetchone()[0] is None
    assert not next(p for p in client.get("/api/v1/papers?engine=heuristic-v2").json()["items"] if p["paper_id"] == 1)["summary_current"]
    initialize(settings)
    assert client.get("/api/v1/events?engine=heuristic-v2").json()["total"] == 0
    assert client.post("/api/v1/reviews", headers=api_fixtures.login(client), json={
        "assertion_id": "assertion-v2", "decision": "approved",
    }).status_code == 409


def test_matching_dictionary_profile_keeps_approval_and_saved_reads_consistent(v2_workspace):
    client, settings = v2_workspace
    with connect(settings) as con:
        con.execute("INSERT INTO engine_profiles(engine_version,rules_version,dictionary_version,enabled,updated_at) VALUES(?,?,?,?,?)", (
            "heuristic-v2", engine_rules_version("heuristic-v2"), "synthetic-dictionary-v2", 1, now(),
        ))
    assert client.post("/api/v1/reviews", headers=api_fixtures.login(client), json={
        "assertion_id": "assertion-v2", "decision": "approved",
    }).status_code == 200
    assert client.get("/api/v1/evidence-map?engine=heuristic-v2&status=approved").json()["total"] == 1
    assert client.get("/api/v1/events?engine=heuristic-v2&status=approved").json()["total"] == 1
