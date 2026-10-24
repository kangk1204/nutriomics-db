"""Synthetic transport fixtures; these tests do not measure scientific accuracy."""

from datetime import date
import json
from pathlib import Path
import pytest
from nutri_literature.settings import Settings
from nutri_literature.db import connect, digest, dumps, initialize, now
from nutri_literature.collection import ApiClient, collect, parse_pubmed, search_all, save_article, parse_jats
import requests

XML = b"""<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article><ArticleTitle>Synthetic DHA study</ArticleTitle><Abstract><AbstractText NlmCategory="METHODS">We enrolled 30 adults.</AbstractText><AbstractText NlmCategory="RESULTS">DHA reduced blood pressure.</AbstractText></Abstract><Journal><JournalIssue><PubDate><Year>2025</Year><Month>Jan</Month></PubDate></JournalIssue></Journal><PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType></PublicationTypeList></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.fake/123</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"""


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "topics.json"
    config.write_text(
        dumps({"topics": [{"id": "t", "name": "test", "query": "DHA", "start_date": "2025-01-01"}]}),
        encoding="utf-8",
    )
    s = Settings(tmp_path / "runtime", config)
    initialize(s)
    return s


class Fake:
    def __init__(self, broken=False):
        self.broken = broken
        self.searches = []

    def pubmed(self, utility, params, post=False):
        if utility == "esearch.fcgi":
            self.searches.append(params)
            if params["retmax"] == 0:
                return dumps({"esearchresult": {"count": "1", "webenv": "x", "querykey": "1"}}).encode()
            return b'{"esearchresult":{"count":"1","idlist":["123"]}}'
        return b"<PubmedArticleSet/>" if self.broken else XML


class NonmatchingRefresh:
    """A complete current search omits an existing synthetic library paper."""

    def __init__(self, xml=XML, partial_search=False):
        self.xml = xml
        self.partial_search = partial_search
        self.fetched = []

    def pubmed(self, utility, params, post=False):
        if utility == "esearch.fcgi":
            result = {"count": "1" if self.partial_search else "0", "idlist": []}
            if self.partial_search and params["retmax"]:
                result["idlist"] = ["456"]
            return dumps({"esearchresult": result}).encode()
        self.fetched.append(params["id"])
        return self.xml


def test_parser_exact_offsets():
    row = parse_pubmed(XML)[0]
    assert row["pub_date"] == "2025-01"
    assert row["doi"] == "10.fake/123"
    for section in row["sections"]:
        assert row["text"][section["start"] : section["end"]]
    assert row["sections"][2]["label"] == "RESULTS"


def test_complete_checkpoint_dedup(settings):
    collect(settings, client=Fake())
    collect(settings, client=Fake())
    with connect(settings) as con:
        assert con.execute("SELECT count(*) FROM papers").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
        assert con.execute("SELECT watermark FROM topic_state").fetchone()[0]
        assert con.execute("SELECT count(*) FROM queue WHERE stage='extract'").fetchone()[0] == 1


def test_partial_fetch_never_advances(settings):
    with pytest.raises(RuntimeError, match="Incomplete"):
        collect(settings, client=Fake(True))
    with connect(settings) as con:
        assert con.execute("SELECT count(*) FROM topic_state").fetchone()[0] == 0
        assert con.execute("SELECT status FROM runs").fetchone()[0] == "failed"


def test_long_outage_uses_watermark(settings):
    with connect(settings) as con:
        version = con.execute("SELECT query_version FROM topics").fetchone()[0]
        con.execute("INSERT INTO topic_state VALUES(?,?,?,?)", ("t", version, "2025-05-20", "x"))
    fake = Fake()
    collect(settings, client=fake)
    assert "2025-05-13" in fake.searches[0]["term"] and "[MDAT]" in fake.searches[0]["term"]


def test_query_change_backfills(settings):
    collect(settings, client=Fake())
    config = json.loads(settings.config.read_text())
    config["topics"][0]["query"] = "expanded DHA"
    settings.config.write_text(dumps(config), encoding="utf-8")
    initialize(settings)
    fake = Fake()
    collect(settings, client=fake)
    assert "[PDAT]" in fake.searches[0]["term"] and "[MDAT]" not in fake.searches[0]["term"]


def test_complete_reconcile_removes_absent_membership_but_preserves_library_history(settings):
    collect(settings, client=Fake())
    with connect(settings) as con:
        paper_id = con.execute("SELECT paper_id FROM papers").fetchone()[0]
        con.execute("INSERT INTO paper_topics VALUES(?,?,?)", (paper_id, "t", "historical-query-version"))
    transport = NonmatchingRefresh()
    result = collect(settings, reconcile=True, client=transport)
    assert result["topics"]["t"]["removed_memberships"] == 1
    assert result["known_library_refresh"]["complete"]
    assert result["known_library_refresh"]["selected"] == result["known_library_refresh"]["returned"] == 1
    assert transport.fetched == ["123"]
    with connect(settings) as con:
        version = con.execute("SELECT query_version FROM topics").fetchone()[0]
        assert (
            con.execute("SELECT COUNT(*) FROM paper_topics WHERE query_version=?", (version,)).fetchone()[0]
            == 0
        )
        assert (
            con.execute("SELECT query_version FROM paper_topics").fetchone()[0] == "historical-query-version"
        )
        assert con.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        audit = con.execute("SELECT * FROM audit WHERE kind='topic_membership_removed'").fetchone()
        details = json.loads(audit["detail"])
        assert audit["paper_id"] == paper_id and details["pmid"] == "123"
        assert details["run_id"] == result["run_id"] and details["query_version"] == version
        assert json.loads(Path(details["manifest"]).read_text())["complete"]


def test_reconcile_refreshes_nonmatching_retraction_and_invalidates_confirmed_assertion(settings):
    collect(settings, client=Fake())
    with connect(settings) as con:
        paper = con.execute("SELECT * FROM papers").fetchone()
        source = con.execute("SELECT * FROM sources").fetchone()
        con.execute(
            "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "synthetic-extraction",
                paper["paper_id"],
                source["source_id"],
                digest(source["text_content"]),
                "heuristic-v1",
                "synthetic",
                "{}",
                now(),
                1,
            ),
        )
        con.execute(
            "INSERT INTO assertions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "synthetic-assertion",
                "synthetic-extraction",
                paper["paper_id"],
                "synthetic-fingerprint",
                "DHA",
                "decreases",
                "blood pressure",
                source["text_content"],
                0,
                len(source["text_content"]),
                "synthetic-rule",
                "approved",
                dumps({"explicit_relation": True}),
            ),
        )
        con.execute("INSERT INTO accounts VALUES('synthetic-reviewer','unused',1,?)", (now(),))
        con.execute(
            "INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)",
            (
                "synthetic-review",
                "synthetic-assertion",
                "synthetic-reviewer",
                "approved",
                "Synthetic fixture only",
                None,
                "synthetic-fingerprint",
                now(),
            ),
        )
    retracted = XML.replace(
        b"</PublicationTypeList>",
        b"<PublicationType>Retracted Publication</PublicationType></PublicationTypeList>",
    )
    retracted = retracted.replace(
        b"</MedlineCitation>",
        b'<CommentsCorrectionsList><CommentsCorrections RefType="RetractionIn"><PMID>999</PMID></CommentsCorrections></CommentsCorrectionsList></MedlineCitation>',
    )
    transport = NonmatchingRefresh(retracted)
    result = collect(settings, reconcile=True, client=transport)
    assert result["known_library_refresh"]["returned"] == 1
    with connect(settings) as con:
        assert con.execute("SELECT retracted FROM papers").fetchone()[0] == 1
        assert con.execute("SELECT status FROM assertions").fetchone()[0] == "retracted"
        assert con.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM paper_topics").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM audit WHERE kind='retraction_change'").fetchone()[0] == 1


def test_partial_search_does_not_prune_membership_or_run_nonmatching_refresh(settings):
    collect(settings, client=Fake())
    transport = NonmatchingRefresh(b"<PubmedArticleSet/>", partial_search=True)
    with pytest.raises(RuntimeError, match="Incomplete"):
        collect(settings, reconcile=True, client=transport)
    assert transport.fetched == ["456"]
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM paper_topics").fetchone()[0] == 1
        assert (
            con.execute("SELECT COUNT(*) FROM audit WHERE kind='topic_membership_removed'").fetchone()[0] == 0
        )
        run = con.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        assert run["status"] == "failed"
        assert (
            json.loads(run["detail"])["known_library_refresh"]["skipped_reason"] == "incomplete_topic_search"
        )


@pytest.mark.parametrize(
    "xml", [b"<PubmedArticleSet/>", XML.replace(b"<PMID>123</PMID>", b"<PMID>456</PMID>")]
)
def test_incomplete_nonmatching_metadata_refresh_fails_reconcile(settings, xml):
    collect(settings, client=Fake())
    with pytest.raises(RuntimeError, match="known_library_refresh"):
        collect(settings, reconcile=True, client=NonmatchingRefresh(xml))
    with connect(settings) as con:
        run = con.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        refresh = json.loads(run["detail"])["known_library_refresh"]
        assert run["status"] == "failed" and not refresh["complete"]
        assert refresh["selected"] == 1 and refresh["returned"] == 0
        assert con.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1


def test_incremental_empty_result_retains_membership(settings):
    collect(settings, client=Fake())
    result = collect(settings, client=NonmatchingRefresh())
    assert result["topics"]["t"]["removed_memberships"] == 0
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM paper_topics").fetchone()[0] == 1


def test_new_topic_uses_the_existing_pipeline(settings):
    config = json.loads(settings.config.read_text())
    config["topics"].append(
        {"id": "extended", "name": "확장 시험", "query": "DHA", "start_date": "2025-01-01"}
    )
    settings.config.write_text(dumps(config), encoding="utf-8")
    initialize(settings)
    collected = collect(settings, topic_id="extended", client=Fake())
    assert set(collected["topics"]) == {"extended"}
    with connect(settings) as con:
        assert con.execute("SELECT topic_id FROM paper_topics").fetchone()[0] == "extended"
        assert con.execute("SELECT topic_id FROM topic_state").fetchone()[0] == "extended"


def test_single_day_overflow_fails():
    class TooMany:
        def pubmed(self, *args, **kwargs):
            return b'{"esearchresult":{"count":"10001"}}'

    with pytest.raises(RuntimeError, match="Single-day"):
        search_all(TooMany(), "x", date(2025, 1, 1), date(2025, 1, 1), "MDAT", lambda _: None)


def test_retraction_invalidates_assertions(settings):
    article = parse_pubmed(XML)[0]
    with connect(settings) as con:
        version = con.execute("SELECT query_version FROM topics").fetchone()[0]
    paper = save_article(settings, article, "t", version)
    article["retracted"] = True
    article["corrections"] = [{"type": "RetractionIn", "pmid": "999"}]
    save_article(settings, article, "t", version)
    with connect(settings) as con:
        assert con.execute("SELECT retracted FROM papers WHERE paper_id=?", (paper,)).fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM audit WHERE kind='retraction_change'").fetchone()[0] == 1


def test_jats_discards_reference_body():
    text, sections, _ = parse_jats(
        b'<article><front><article-meta><title-group><article-title>Test</article-title></title-group></article-meta></front><body><sec sec-type="results"><title>Results</title><p>DHA reduced BP.</p></sec></body><back><ref-list><ref>Old gene finding.</ref></ref-list></back></article>'
    )
    assert "Old gene" not in text and sections[-1]["label"] == "results"


def test_external_entities_rejected():
    with pytest.raises(Exception):
        parse_pubmed(
            b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><PubmedArticleSet>&a;</PubmedArticleSet>'
        )


@pytest.mark.parametrize("mode", ["complete", "duplicate", "drift"])
def test_page_boundary_count_and_unique_pmids(mode):
    class Pages:
        def pubmed(self, utility, params):
            if params["retmax"] == 0:
                return dumps({"esearchresult": {"count": "1001"}}).encode()
            offset = params["retstart"]
            ids = [str(i + 1) for i in range(offset, min(offset + 1000, 1001))]
            if offset and mode == "duplicate":
                ids = ["1"]
            return dumps(
                {"esearchresult": {"count": "1002" if offset and mode == "drift" else "1001", "idlist": ids}}
            ).encode()

    if mode == "complete":
        ids = search_all(Pages(), "x", date(2025, 1, 1), date(2026, 1, 1), "PDAT", lambda _: None)
        assert len(ids) == 1001 and ids[-1] == "1001"
    else:
        with pytest.raises(RuntimeError, match="count"):
            search_all(Pages(), "x", date(2025, 1, 1), date(2026, 1, 1), "PDAT", lambda _: None)


def test_overflow_splits_disjoint_periods():
    class Split:
        def pubmed(self, utility, params):
            term = params["term"]
            if '"2025-01-01"[PDAT] : "2025-01-02"' in term:
                return b'{"esearchresult":{"count":"10001"}}'
            pmid = "1" if '"2025-01-01"[PDAT]' in term else "2"
            return dumps({"esearchresult": {"count": "1", "idlist": [pmid]}}).encode()

    assert search_all(Split(), "x", date(2025, 1, 1), date(2025, 1, 2), "PDAT", lambda _: None) == ["1", "2"]


def test_429_and_timeout_retry_is_bounded_and_sanitized(settings, monkeypatch):
    monkeypatch.setattr("nutri_literature.collection.time.sleep", lambda _: None)

    class Response:
        status_code = 429
        headers = {"Retry-After": "1"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    client = ApiClient(settings)
    attempts = []

    def rate_limited(*args, **kwargs):
        attempts.append(1)
        return Response()

    monkeypatch.setattr(client.session, "request", rate_limited)
    with pytest.raises(RuntimeError, match="429"):
        client.request("https://example.invalid", {"api_key": "SYNTHETIC_SECRET"})
    assert len(attempts) == 5
    attempts.clear()

    def timed_out(*args, **kwargs):
        attempts.append(1)
        raise requests.Timeout("SYNTHETIC_SECRET")

    monkeypatch.setattr(client.session, "request", timed_out)
    with pytest.raises(RuntimeError) as caught:
        client.request("https://example.invalid", {"api_key": "SYNTHETIC_SECRET"})
    assert len(attempts) == 5 and "SYNTHETIC_SECRET" not in str(caught.value)
