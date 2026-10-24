"""Synthetic end-to-end source/queue/extraction persistence."""

import pytest
from types import SimpleNamespace
from nutri_literature.db import connect, dumps, initialize
from nutri_literature.settings import Settings
from nutri_literature.collection import save_article
from nutri_literature.pipeline import process, store_extraction


@pytest.fixture
def setting(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "nutri_literature.pipeline.psutil.virtual_memory", lambda: SimpleNamespace(available=16 * 1024**3)
    )
    config = tmp_path / "topics.json"
    config.write_text(
        dumps({"topics": [{"id": "t", "name": "T", "query": "DHA", "start_date": "2025-01-01"}]})
    )
    s = Settings(tmp_path / "runtime", config)
    initialize(s)
    return s


def article():
    text = "We enrolled 30 adults. DHA reduced blood pressure."
    return {
        "pmid": "123",
        "doi": None,
        "pmcid": None,
        "title": "Synthetic test",
        "pub_date": "2025",
        "abstract": text,
        "text": text,
        "sections": [
            {"label": "METHODS", "start": 0, "end": 22},
            {"label": "RESULTS", "start": 23, "end": len(text)},
        ],
        "publication_types": ["Randomized Controlled Trial"],
        "mesh": ["Humans"],
        "authors": [],
        "retracted": False,
        "corrections": [],
        "element_xml": b"<synthetic>one</synthetic>",
    }


def test_queue_extract_persists_anchored_mentions(setting):
    with connect(setting) as con:
        version = con.execute("SELECT query_version FROM topics").fetchone()[0]
    save_article(setting, article(), "t", version)
    result = process(setting, "extract")
    assert result["processed"] == 1 and result["failed"] == 0
    with connect(setting) as con:
        assert con.execute("SELECT count(*) FROM extractions WHERE active=1").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM mentions").fetchone()[0] >= 2
        assert con.execute("SELECT count(*) FROM assertions").fetchone()[0] >= 1
        source = con.execute("SELECT text_content FROM sources").fetchone()[0]
        for mention in con.execute("SELECT * FROM mentions"):
            assert source[mention["start"] : mention["end"]] == mention["text"]


def test_metadata_only_preserves_evidence_identity(setting):
    with connect(setting) as con:
        version = con.execute("SELECT query_version FROM topics").fetchone()[0]
    data = article()
    save_article(setting, data, "t", version)
    process(setting, "extract")
    with connect(setting) as con:
        before = con.execute("SELECT current_source_id FROM papers").fetchone()[0]
    data["element_xml"] = b"<synthetic>new metadata, same evidence</synthetic>"
    save_article(setting, data, "t", version)
    with connect(setting) as con:
        assert con.execute("SELECT current_source_id FROM papers").fetchone()[0] == before
        assert con.execute("SELECT count(*) FROM sources").fetchone()[0] == 2


def test_reject_unanchored_assertion(setting):
    with connect(setting) as con:
        version = con.execute("SELECT query_version FROM topics").fetchone()[0]
    paper_id = save_article(setting, article(), "t", version)
    with connect(setting) as con:
        source = dict(con.execute("SELECT * FROM sources").fetchone())
    bad = {
        "engine_version": "heuristic-v1",
        "dictionary_version": "synthetic",
        "mentions": [],
        "assertions": [
            {
                "start": 0,
                "end": 5,
                "quote": "WRONG",
                "subject": "x",
                "object": "y",
                "predicate": "affects",
                "rule_id": "fake",
            }
        ],
    }
    with pytest.raises(ValueError, match="offsets"):
        store_extraction(setting, paper_id, source, bad)
