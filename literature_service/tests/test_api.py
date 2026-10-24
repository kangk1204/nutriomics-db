"""Synthetic source-linked API fixtures, unrelated to real scientific findings."""

import json

from fastapi.testclient import TestClient
import pytest

from nutri_literature import auth
from nutri_literature.api import create_app
from nutri_literature.db import connect, digest, dumps, now, save_source
from nutri_literature.extraction import RULES_VERSION
from nutri_literature.settings import Settings


@pytest.fixture
def workspace(tmp_path):
    config = tmp_path / "topics.json"
    config.write_text(
        json.dumps(
            {
                "topics": [
                    {
                        "id": "synthetic",
                        "name": "합성 시험 주제",
                        "query": "synthetic",
                        "start_date": "2025-01-01",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(tmp_path / "runtime", config)
    with TestClient(create_app(settings)) as client:
        auth.create_account(settings, "reviewer_a", "Synthetic password 123")
        text = "Synthetic only: omega-3 decreased NOS3 expression in participants."
        with connect(settings) as con:
            version = con.execute("SELECT query_version FROM topics").fetchone()[0]
            for paper_id in (1, 2, 3):
                con.execute(
                    "INSERT INTO papers(paper_id,pmid,title,pub_date,abstract,retracted,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        paper_id,
                        f"synthetic-{paper_id}",
                        f"Synthetic trial {paper_id} <script>alert(1)</script>",
                        "2026-01-01",
                        text,
                        int(paper_id == 2),
                        now(),
                        now(),
                    ),
                )
                con.execute(
                    "INSERT INTO paper_topics VALUES(?,?,?)",
                    (paper_id, "synthetic", "outdated-query-version" if paper_id == 3 else version),
                )
                source_id = save_source(
                    con,
                    paper_id,
                    "pubmed",
                    settings.raw / f"synthetic-{paper_id}.xml",
                    b"<article>SYNTHETIC FIXTURE</article>",
                    text,
                    [],
                    "https://pubmed.ncbi.nlm.nih.gov/synthetic/",
                )
                con.execute("UPDATE papers SET current_source_id=? WHERE paper_id=?", (source_id, paper_id))
                con.execute(
                    "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        f"extraction-{paper_id}",
                        paper_id,
                        source_id,
                        digest(text),
                        "heuristic-v1",
                        "synthetic-dictionary",
                        dumps(
                            {
                                "rules_version": RULES_VERSION,
                                "summary": {"results": [{"text": text, "start": 0, "end": len(text)}]},
                            }
                        ),
                        now(),
                        1,
                    ),
                )
                con.execute(
                    "INSERT INTO assertions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"assertion-{paper_id}",
                        f"extraction-{paper_id}",
                        paper_id,
                        f"synthetic-fingerprint-{paper_id}",
                        "omega-3",
                        "decreases",
                        "HGNC:7876",
                        text,
                        0,
                        len(text),
                        "synthetic-explicit-rule",
                        "candidate",
                        dumps({"explicit_relation": True}),
                    ),
                )
                start = text.index("NOS3")
                con.execute(
                    "INSERT INTO mentions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"mention-{paper_id}",
                        f"extraction-{paper_id}",
                        paper_id,
                        "gene",
                        "NOS3",
                        "HGNC:7876",
                        start,
                        start + 4,
                        "candidate",
                        dumps({"symbol": "NOS3"}),
                    ),
                )
        yield client, settings


def login(client):
    response = client.post(
        "/api/v1/auth/login", json={"username": "reviewer_a", "password": "Synthetic password 123"}
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_readonly_search_pagination_provenance_and_limits(workspace):
    client, _settings = workspace
    assert client.get("/healthz").json()["status"] == "ok"
    assert len(client.get("/api/v1/topics").json()["items"]) == 1
    response = client.get(
        "/api/v1/papers", params={"q": "NOS3", "topic": "synthetic", "limit": 1, "offset": 1}
    )
    assert response.status_code == 200
    assert response.json()["total"] == 2  # old query membership excluded
    assert len(response.json()["items"]) == 1
    assert client.get("/api/v1/papers?limit=1000").status_code == 422
    assert client.get("/api/v1/papers?offset=-1").status_code == 422
    assert client.get("/api/v1/papers", params={"q": '" OR 1=1 --'}).json()["total"] == 0
    detail = client.get("/api/v1/papers/1").json()
    assert detail["sources"][0]["raw_hash"]
    assert detail["sources"][0]["text_hash"]
    assert "raw_path" not in detail["sources"][0]
    assert detail["official_links"]["pubmed"].startswith("https://pubmed.ncbi.nlm.nih.gov/")
    source = detail["sources"][0]["text_content"]
    edge = detail["assertions"][0]
    assert source[edge["start"] : edge["end"]] == edge["quote"]
    assert client.get("/api/v1/papers/999").status_code == 404


def test_evidence_map_excludes_retractions_and_old_query_memberships(workspace):
    client, _settings = workspace
    mapping = client.get("/api/v1/evidence-map", params={"topic": "synthetic", "gene": "NOS3"}).json()
    assert mapping["total"] == 1
    assert client.get("/api/v1/evidence-map").json()["total"] == 1
    assert mapping["edges"][0]["assertion_id"] == "assertion-1"
    assert not mapping["edges"][0]["is_reviewed"]
    assert client.get("/api/v1/evidence-map?status=approved").json()["total"] == 0
    assert client.get("/api/v1/evidence-map?gene=unrelated").json()["total"] == 0


def test_review_authentication_audit_and_logout(workspace):
    client, settings = workspace
    body = {"assertion_id": "assertion-1", "decision": "approved", "comment": "Synthetic fixture verified."}
    assert client.post("/api/v1/reviews", json=body).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "reviewer_a", "password": "incorrect"}
        ).status_code
        == 401
    )
    headers = login(client)
    response = client.post("/api/v1/reviews", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["reviewer"] == "reviewer_a"
    mapping = client.get("/api/v1/evidence-map?status=approved&topic=synthetic").json()
    assert mapping["total"] == 1 and mapping["edges"][0]["is_reviewed"]
    with connect(settings) as con:
        review = con.execute("SELECT * FROM reviews").fetchone()
        assert review["fingerprint"] == "synthetic-fingerprint-1"
        assert con.execute("SELECT COUNT(*) FROM audit WHERE kind='review'").fetchone()[0] == 1
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.post("/api/v1/reviews", json=body, headers=headers).status_code == 401


@pytest.mark.parametrize(
    "change", ["inactive", "hash_changed", "current_changed", "quote_changed", "rules_changed", "retracted"]
)
def test_stale_or_retracted_evidence_cannot_be_approved(workspace, change):
    client, settings = workspace
    headers = login(client)
    with connect(settings) as con:
        if change == "inactive":
            con.execute("UPDATE extractions SET active=0 WHERE paper_id=1")
        elif change == "hash_changed":
            con.execute("UPDATE sources SET text_content='Modified synthetic text' WHERE paper_id=1")
        elif change == "current_changed":
            con.execute("UPDATE papers SET current_source_id=NULL WHERE paper_id=1")
        elif change == "quote_changed":
            con.execute("UPDATE assertions SET quote='Unanchored synthetic claim' WHERE paper_id=1")
        elif change == "rules_changed":
            con.execute(
                "UPDATE extractions SET result_json=? WHERE paper_id=1", (dumps({"rules_version": "old"}),)
            )
        else:
            con.execute("UPDATE papers SET retracted=1 WHERE paper_id=1")
    response = client.post(
        "/api/v1/reviews", headers=headers, json={"assertion_id": "assertion-1", "decision": "approved"}
    )
    assert response.status_code == 409
    assert client.get("/api/v1/evidence-map?topic=synthetic").json()["total"] == 0


@pytest.mark.parametrize("change", ["rules_changed", "source_changed", "text_integrity", "inactive"])
def test_stale_summaries_and_historical_approvals_are_not_current(workspace, change):
    client, settings = workspace
    headers = login(client)
    assert (
        client.post(
            "/api/v1/reviews", headers=headers, json={"assertion_id": "assertion-1", "decision": "approved"}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/papers/1").json()["summary_current"]
    assert client.get("/api/v1/runs").json()["counts"]["summarized_papers"] == 3
    with connect(settings) as con:
        if change == "rules_changed":
            result = json.loads(
                con.execute("SELECT result_json FROM extractions WHERE paper_id=1").fetchone()[0]
            )
            result["rules_version"] = "retired-synthetic-rules"
            con.execute("UPDATE extractions SET result_json=? WHERE paper_id=1", (dumps(result),))
        elif change == "source_changed":
            con.execute("UPDATE papers SET current_source_id=NULL WHERE paper_id=1")
        elif change == "text_integrity":
            con.execute("UPDATE sources SET text_content='modified synthetic source' WHERE paper_id=1")
        else:
            con.execute("UPDATE extractions SET active=0 WHERE paper_id=1")
    papers = client.get("/api/v1/papers").json()["items"]
    stale = next(paper for paper in papers if paper["paper_id"] == 1)
    assert stale["summary"] is None and stale["summary_status"] == "stale"
    assert not stale["summary_current"]
    detail = client.get("/api/v1/papers/1").json()
    assert not detail["summary_current"] and detail["summary_status"] == "stale"
    assert not detail["extractions"][0]["current_valid"]
    assert change in detail["extractions"][0]["stale_reasons"]
    assert detail["mentions"] == []
    # Preserve the human decision as historical data while changing its display status.
    historical = detail["assertions"][0]
    assert historical["status"] == "approved" and historical["review"]["decision"] == "approved"
    assert historical["display_status"] == "needs_review" and not historical["current_valid"]
    assert client.get("/api/v1/runs").json()["counts"]["summarized_papers"] == 2


def test_corrections_preserve_original_quote_and_require_source_anchors(workspace):
    client, settings = workspace
    headers = login(client)
    body = {
        "assertion_id": "assertion-1",
        "decision": "corrected",
        "comment": "Synthetic label correction.",
        "corrected_payload": {"subject": "omega-3", "predicate": "associated_with", "object": "NOS3"},
    }
    assert client.post("/api/v1/reviews", json=body, headers=headers).status_code == 200
    edge = client.get("/api/v1/evidence-map?topic=synthetic&status=approved").json()["edges"][0]
    assert edge["object"] == "NOS3"
    assert edge["original_relation"]["object"] == "HGNC:7876"
    with connect(settings) as con:
        original = con.execute(
            "SELECT object,quote FROM assertions WHERE assertion_id='assertion-1'"
        ).fetchone()
        assert original["object"] == "HGNC:7876"
        assert original["quote"].startswith("Synthetic only:")
    body["corrected_payload"]["quote"] = "Silently changed source"
    assert client.post("/api/v1/reviews", json=body, headers=headers).status_code == 422
    del body["corrected_payload"]["quote"]
    body["corrected_payload"]["object"] = "Invented gene"
    assert client.post("/api/v1/reviews", json=body, headers=headers).status_code == 422


def test_status_alone_does_not_create_reviewed_evidence(workspace):
    client, settings = workspace
    with connect(settings) as con:
        con.execute("UPDATE assertions SET status='approved'")
    assert client.get("/api/v1/evidence-map?status=approved").json()["total"] == 0


def test_nonexplicit_relation_cannot_be_approved(workspace):
    client, settings = workspace
    with connect(settings) as con:
        con.execute("UPDATE assertions SET payload='{}' WHERE assertion_id='assertion-1'")
    headers = login(client)
    assert (
        client.post(
            "/api/v1/reviews", headers=headers, json={"assertion_id": "assertion-1", "decision": "approved"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/reviews", headers=headers, json={"assertion_id": "assertion-1", "decision": "rejected"}
        ).status_code
        == 200
    )


def test_pinned_report_download_paths_and_counts(workspace, tmp_path):
    client, settings = workspace
    valid = settings.reports / "synthetic.md"
    valid.write_text("Synthetic report", encoding="utf-8")
    secret = tmp_path / "outside.md"
    secret.write_text("must not be served", encoding="utf-8")
    with connect(settings) as con:
        for name, path in [
            ("good", str(valid)),
            ("outside", str(secret)),
            ("relative_escape", "../../outside.md"),
        ]:
            con.execute(
                "INSERT INTO reports VALUES(?,?,?,?,?,?,?)",
                (name, now(), "2026-01-01", "2026-01-08", "synthetic-hash", "{}", dumps({"markdown": path})),
            )
    listing = client.get("/api/v1/reports").json()
    assert listing["total"] == 3
    assert "paths" not in listing["items"][0]
    assert client.get("/api/v1/reports/good/download/markdown").text == "Synthetic report"
    assert client.get("/api/v1/reports/outside/download/markdown").status_code == 404
    assert client.get("/api/v1/reports/relative_escape/download/markdown").status_code == 404
    assert client.get("/api/v1/reports/good/download/json").status_code == 422
    runs = client.get("/api/v1/runs").json()
    assert runs["counts"]["papers"] == 3
    assert runs["counts"]["summarized_papers"] == 3
    assert runs["counts"]["oa_papers"] == 0
