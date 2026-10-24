"""Synthetic transport/provenance tests; not scientific model evaluations."""
from datetime import date
import json
import re

import pytest

from nutri_literature.collection_v8 import PartitionedSearch, expand, inspect, load_contract
from nutri_literature.db import connect, dumps, initialize
from nutri_literature.locking import lock
from nutri_literature.settings import Settings


class Transport:
    def __init__(self, records, missing=(), duplicate=False):
        self.records, self.missing, self.duplicate = records, set(missing), duplicate
        self.calls = []

    def pubmed(self, utility, params, post=False):
        self.calls.append((utility, params))
        if utility == "esearch.fcgi":
            lo, hi = re.findall(r'"(\d{4}-\d{2}-\d{2})"\[PDAT\]', params["term"])
            ids = sorted((p for p, d in self.records.items() if lo <= d <= hi), key=int)
            uid_range = re.search(r"\((\d+):(\d+)\[UID\]\)", params["term"])
            if uid_range:
                a, b = map(int, uid_range.groups())
                ids = [p for p in ids if a <= int(p) <= b]
            start, size = params.get("retstart", 0), params["retmax"]
            page = ids[start:start + size]
            if self.duplicate and len(page) > 1:
                page[-1] = page[0]
            return dumps({"esearchresult": {"count": str(len(ids)), "idlist": page}}).encode()
        assert post and utility == "efetch.fcgi"
        articles = []
        for pmid in params["id"].split(","):
            if pmid in self.missing:
                continue
            articles.append(f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>Study {pmid}</ArticleTitle>"
                            "<Abstract><AbstractText>Nutrition result.</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle>")
        return ("<PubmedArticleSet>" + "".join(articles) + "</PubmedArticleSet>").encode()


@pytest.fixture
def setup(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(dumps({"topics": [{"id": "old", "name": "legacy", "query": "old", "start_date": "2025-01-01"}]}), encoding="utf-8")
    settings = Settings(tmp_path / "runtime", legacy)
    initialize(settings)
    config = tmp_path / "v8.json"
    config.write_text(dumps({"corpus_id": "synthetic", "end_date": "2026-10-23", "partition_limit": 8000,
                            "topics": [{"id": "new", "name": "new", "query": "diet", "start_date": "1800-01-01"}]}), encoding="utf-8")
    return settings, config


def test_adaptive_year_month_day_partition_and_boundary(setup):
    settings, _ = setup
    records = {"1": "2024-12-31", "2": "2025-01-01", "3": "2025-02-01", "4": "2025-02-28", "5": "2025-03-01"}
    fake = Transport(records)
    result, artifacts = PartitionedSearch(settings, fake, settings.home / "parts", 2).search("diet", date(2024, 1, 1), date(2025, 12, 31))
    assert result == list(records)
    assert artifacts
    # No request attempts to fetch a page from an oversized unbounded result set.
    assert all(p["retmax"] <= 2 for _, p in fake.calls if p["retmax"])
    cached = Transport({})
    assert PartitionedSearch(settings, cached, settings.home / "parts", 2).search("diet", date(2024, 1, 1), date(2025, 12, 31))[0] == result
    assert cached.calls == []


def test_same_day_uid_fallback_no_pubmed_limit_truncation(setup):
    settings, _ = setup
    fake = Transport({"1": "2025-01-01", "2": "2025-01-01", "3": "2025-01-01"})
    result, _ = PartitionedSearch(settings, fake, settings.home / "uid-parts", 2).search("diet", date(2025, 1, 1), date(2025, 1, 1))
    assert result == ["1", "2", "3"]
    assert any("[UID]" in p["term"] for _, p in fake.calls)
    assert max(p["retstart"] for _, p in fake.calls) == 0


def test_duplicate_page_fails_closed(setup):
    settings, _ = setup
    with pytest.raises(RuntimeError, match="boundary/count"):
        PartitionedSearch(settings, Transport({"1": "2025-01-01", "2": "2025-01-01"}, duplicate=True), settings.home / "bad").search("diet", date(2025, 1, 1), date(2025, 1, 1))


def test_resume_one_batch_preserves_denominator_and_queues(setup):
    settings, config = setup
    fake = Transport({"1": "2025-01-01", "2": "2025-01-01", "3": "2025-01-01"})
    result = expand(settings, config, fake, batch_size=1)
    assert result["selected_unique"] == 3 and result["metadata_status"] == {"completed": 1, "pending": 2}
    assert not result["metadata_complete"]
    second = Transport(fake.records)
    result = expand(settings, config, second, batch_size=1, max_batches=None)
    assert result["metadata_complete"]
    assert all(u == "efetch.fcgi" for u, _ in second.calls)
    assert [p["id"] for _, p in second.calls] == ["2", "3"]
    state = json.loads((settings.home / "v8-expansion/state.json").read_text())
    assert state["processed"] == 3 and "missing_pmids" not in state
    manifest = json.loads(__import__("pathlib").Path(state["manifest"]).read_text())
    assert manifest["expected_papers"] == 3 and manifest["pmids"] == ["1", "2", "3"]
    with connect(settings) as c:
        assert c.execute("SELECT count(*) FROM queue").fetchone()[0] == 6
        assert c.execute("SELECT enabled FROM topics WHERE topic_id='old'").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM topics WHERE topic_id LIKE 'v8-%' AND enabled=0").fetchone()[0] == 1


def test_partial_fetch_retained_and_only_failures_retried(setup):
    settings, config = setup
    records = {"1": "2025-01-01", "2": "2025-01-01"}
    result = expand(settings, config, Transport(records, missing=["2"]), max_batches=None)
    assert result["metadata_status"] == {"completed": 1, "failed": 1}
    assert result["missing_pmids"] == ["2"] and not result["metadata_complete"]
    retry = Transport(records)
    result = expand(settings, config, retry, max_batches=None)
    assert result["metadata_complete"]
    assert [(u, p["id"]) for u, p in retry.calls] == [("efetch.fcgi", "2")]


def test_duplicate_topics_union_metadata_once_and_additive_query_change(setup):
    settings, config = setup
    payload = json.loads(config.read_text())
    payload["topics"].append({**payload["topics"][0], "id": "second"})
    config.write_text(dumps(payload), encoding="utf-8")
    fake = Transport({"1": "2025-01-01"})
    first = expand(settings, config, fake, max_batches=None)
    assert first["selected_unique"] == 1 and first["metadata_complete"]
    assert len([u for u, _ in fake.calls if u == "efetch.fcgi"]) == 1
    old_fingerprint = first["contract_sha256"]
    payload["topics"][0]["query"] = "expanded diet"
    config.write_text(dumps(payload), encoding="utf-8")
    second = expand(settings, config, Transport({"1": "2025-01-01"}), max_batches=None)
    assert second["contract_sha256"] != old_fingerprint
    with connect(settings) as c:
        assert c.execute("SELECT count(*) FROM papers").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM paper_topics").fetchone()[0] == 3


def test_read_only_audit_does_not_start_collection_or_modify_db(setup):
    settings, config = setup
    assert not inspect(settings, config)["initialized"]
    result = expand(settings, config, Transport({"1": "2025-01-01"}), max_batches=None)
    before = settings.database.read_bytes()
    assert inspect(settings, config)["metadata_complete"]
    assert settings.database.read_bytes() == before
    assert result["reference_counts_are_collected"] is False


def test_global_collection_lock_does_not_take_stage_locks(setup):
    settings, config = setup
    with lock(settings.home / "locks/collection.lock"):
        with pytest.raises(OSError):
            expand(settings, config, Transport({}))
    # Active OA/extract owner is compatible: expansion never steals its lock.
    with lock(settings.home / "locks/worker-oa.lock"), lock(settings.home / "locks/worker-extract.lock"):
        assert expand(settings, config, Transport({"1": "2025-01-01"}))["metadata_complete"]


def test_archived_raw_tampering_detected_on_resume(setup):
    settings, config = setup
    result = expand(settings, config, Transport({"1": "2025-01-01"}), max_batches=None)
    raw = next((settings.raw / "search-v8").glob("*.json"))
    raw.write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="integrity"):
        inspect(settings, config)


def test_default_contract_retains_six_legacy_topics_and_frozen_cutoff():
    contract, fingerprint = load_contract()
    assert len(fingerprint) == 64
    config = contract["configuration"]
    assert config["end_date"] == "2026-10-23" and config["partition_limit"] == 8000
    assert len(config["topics"]) == 8
    assert all(t["start_date"] == "2010-01-01" for t in config["topics"][2:])


def test_exact_8000_boundary_pages_and_last_page(setup):
    settings, _ = setup
    records = {str(n): "2025-01-01" for n in range(1, 8001)}
    fake = Transport(records)
    ids, _ = PartitionedSearch(settings, fake, settings.home / "boundary").search("diet", date(2025, 1, 1), date(2025, 1, 1))
    assert len(ids) == 8000
    assert [p["retstart"] for _, p in fake.calls if p["retmax"]] == list(range(0, 8000, 1000))
    assert not any("[UID]" in p["term"] for _, p in fake.calls)


def test_count_drift_resume_uses_new_attempt_preserving_failed_raw(setup):
    settings, config = setup

    class Drift(Transport):
        def pubmed(self, utility, params, post=False):
            raw = super().pubmed(utility, params, post)
            if utility == "esearch.fcgi" and params["retmax"]:
                payload = json.loads(raw)
                payload["esearchresult"]["count"] = "3"
                return dumps(payload).encode()
            return raw

    with pytest.raises(RuntimeError, match="count drift"):
        expand(settings, config, Drift({"1": "2025-01-01"}))
    attempts = list((settings.home / "v8-expansion").glob("*/search/new/*/attempt.json"))
    assert len(attempts) == 1
    failed = attempts[0].read_bytes()
    good = Transport({"1": "2025-01-01", "2": "2025-01-01"})
    assert expand(settings, config, good, max_batches=None)["metadata_complete"]
    assert attempts[0].read_bytes() == failed
    assert len(list((settings.home / "v8-expansion").glob("*/search/new/*/attempt.json"))) == 2
    assert any(u == "esearch.fcgi" for u, _ in good.calls)


def test_resource_wait_does_not_discard_or_complete_pending_pmids(setup, monkeypatch):
    settings, config = setup
    import nutri_literature.collection_v8 as module
    monkeypatch.setattr(module, "resource_status", lambda *a: {"ready": False, "available_memory_bytes": 1, "free_disk_bytes": 1})
    result = expand(settings, config, Transport({"1": "2025-01-01"}), max_batches=None)
    assert result["stage"] == "waiting-resources" and not result["metadata_complete"]
    assert result["metadata_status"] == {"pending": 1}


def test_restored_membership_replays_without_losing_frozen_search(setup):
    settings, config = setup
    fake = Transport({"1": "2025-01-01"})
    assert expand(settings, config, fake)["metadata_complete"]
    with connect(settings) as c:
        c.execute("DELETE FROM paper_topics WHERE topic_id LIKE 'v8-%'")
    assert not inspect(settings, config)["metadata_complete"]
    replay = Transport(fake.records)
    assert expand(settings, config, replay)["metadata_complete"]
    assert [u for u, _ in replay.calls] == ["efetch.fcgi"]
