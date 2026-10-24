"""Real query equivalence, WAL invalidation, bounded caches and result failures."""
import concurrent.futures
import json
import sqlite3
import time

import pytest
from test_explorer_api import (
    explorer as explorer,  # noqa: PLC0414 -- Explicit re-export of shared pytest fixture.
)
from test_explorer_scientific_api import _science

from nutrigenomics.explorer_performance import CACHE, ByteCache, QueryUnavailable, fingerprint
from nutrigenomics.explorer_performance_build import build_performance_release


@pytest.mark.parametrize("setting,normal", [(None, 10), ("15", 15), ("30", 30)])
def test_configured_read_budget_keeps_export_budget_and_profiles_status(monkeypatch, tmp_path, setting, normal):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from nutrigenomics.explorer_performance import CURRENT, ExplorerReadMiddleware
    if setting is None:
        monkeypatch.delenv("NUTRIGENOMICS_READ_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("NUTRIGENOMICS_READ_TIMEOUT_SECONDS", setting)
    profile = tmp_path / "requests.jsonl"
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_PROFILE", str(profile))
    app = FastAPI()

    @app.post("/api/v1/explorer/budget-probe")
    @app.post("/api/v1/explorer/budget-probe/export")
    def probe():
        return {"remaining": CURRENT.get().deadline - time.monotonic()}

    @app.post("/api/v1/explorer/budget-probe/failure")
    def failure():
        raise HTTPException(503, {"code": "source_unavailable"})

    app.add_middleware(ExplorerReadMiddleware)
    with TestClient(app) as client:
        response = client.post("/api/v1/explorer/budget-probe")
        assert response.status_code == 200 and normal - 1 < response.json()["remaining"] <= normal
        response = client.post("/api/v1/explorer/budget-probe/export")
        assert response.status_code == 200 and 44 < response.json()["remaining"] <= 45
        assert client.post("/api/v1/explorer/budget-probe/failure").status_code == 503
    rows = [json.loads(line) for line in profile.read_text().splitlines()]
    assert [r["status"] for r in rows] == [200, 200, 503]
    assert [r["deadline_seconds"] for r in rows] == [normal, 45, normal]
    assert all("parameters" not in r and "body" not in r for r in rows)


@pytest.mark.parametrize("setting", ["", "0", "9", "31", "60", "nan", "30.5"])
def test_unbounded_or_invalid_read_budget_is_rejected(monkeypatch, setting):
    from nutrigenomics.explorer_performance import ExplorerReadMiddleware
    monkeypatch.setenv("NUTRIGENOMICS_READ_TIMEOUT_SECONDS", setting)
    with pytest.raises(ValueError, match="integer from 10 to 30"):
        ExplorerReadMiddleware(None)


def upgrade(explorer, monkeypatch):
    parent = explorer["db"].parent
    original = parent / "explorer.sqlite"
    assert original == explorer["db"]
    output = parent / "performance"
    manifest = build_performance_release(parent, explorer["core"], output, "fixture-perf-v1")
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DATABASE", str(output / "explorer.sqlite"))
    return output, manifest


@pytest.mark.parametrize("seed", ["food:rice", "food:fruit", "MESH:C001", "NCBIGene:41", "MESH:D006973"])
@pytest.mark.parametrize("opts", [{}, {"max_nodes": 250, "max_edges": 750}, {"tier": "curated"}, {"domain": "blood_pressure"}, {"include_extracted": True}])
def test_graph_identical_with_index(explorer, monkeypatch, seed, opts):
    client = explorer["client"]
    before = client.get("/api/v1/explorer/graph", params={"seed_id": seed, **opts})
    upgrade(explorer, monkeypatch)
    after = client.get("/api/v1/explorer/graph", params={"seed_id": seed, **opts})
    assert before.status_code == after.status_code
    left, right = before.json(), after.json()
    left.pop("release_id", None)
    right.pop("release_id", None)
    assert left == right


@pytest.mark.parametrize("term", ["rice", "ri", "C001", "Gene:41", "%", "_", "rice\"", "MESH:", "a\\b", "고혈압", "ENTPD1", "RICE"])
def test_substring_search_equivalence(explorer, monkeypatch, term):
    params = {"kind": "food", "q": term}
    before = explorer["client"].get("/api/v1/explorer/entities", params=params).json()
    upgrade(explorer, monkeypatch)
    after = explorer["client"].get("/api/v1/explorer/entities", params=params).json()
    before.pop("release_id"); after.pop("release_id")
    assert before == after


def test_matches_and_deep_page_exact(explorer, monkeypatch):
    _science(explorer)
    with sqlite3.connect(explorer["core"]) as db:
        for i in range(51):
            db.execute("INSERT INTO study_effect(series_accession,feature_type,feature_id,contrast,analysis_version,effect_estimate,q_value,metadata_json) VALUES('GSE_FIXTURE','gene_expression',?,'6h minus baseline','v1',?,0.8,'{}')", (f"ZZ{i:03}", -i / 100))
    payload = {"seed_id": "food:rice", "intake_g": 150, "reference_context": {"tissue": "PBMC", "response_time_h": 6}}
    params = {"series": "GSE_FIXTURE", "feature_type": "gene_expression", "contrast": "6h minus baseline", "analysis_version": "v1", "page_size": 10, "page": 5}
    before_match = explorer["client"].post("/api/v1/explorer/match", json=payload).json()
    before_page = explorer["client"].get("/api/v1/explorer/omics/results", params=params).json()
    _, manifest = upgrade(explorer, monkeypatch)
    assert manifest["performance"]["effect_values_copied"] == 0
    after_match = explorer["client"].post("/api/v1/explorer/match", json=payload).json()
    after_page = explorer["client"].get("/api/v1/explorer/omics/results", params=params).json()
    before_match.pop("release_id"); after_match.pop("release_id")
    assert before_match == after_match
    for key in ("items", "total", "feature_type", "available_feature_types"):
        assert before_page[key] == after_page[key]
    cursor = after_page["next_cursor"]
    assert cursor
    following = explorer["client"].get("/api/v1/explorer/omics/results", params={**params, "cursor": cursor}).json()
    expected = explorer["client"].get("/api/v1/explorer/omics/results", params={**params, "page": 6}).json()
    assert following["items"] == expected["items"]
    changed = explorer["client"].get("/api/v1/explorer/omics/results", params={**params, "cursor": cursor, "feature_id": "ZZ001"})
    assert changed.status_code == 409
    malformed = explorer["client"].get("/api/v1/explorer/omics/results", params={**params, "cursor": "%%%"})
    assert malformed.status_code == 422


def test_missing_source_is_not_zero_or_cached(explorer, monkeypatch):
    _science(explorer)
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", str(explorer["core"].parent / "missing.sqlite"))
    for _ in range(2):
        response = explorer["client"].post("/api/v1/explorer/match", json={"seed_id": "food:rice"})
        contrast = response.json()["observed_evidence"][0]["contrasts"][0]
        assert contrast["effect_total"] is None
        assert contrast["effects_status"] == "source_unavailable"
        assert response.headers["x-explorer-cache"] == "miss"


def test_wal_commit_invalidates_cache_and_index(explorer, monkeypatch):
    _science(explorer)
    writer = sqlite3.connect(explorer["core"])
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    try:
        upgrade(explorer, monkeypatch)
        payload = {"seed_id": "food:rice"}
        first = explorer["client"].post("/api/v1/explorer/match", json=payload)
        prior = fingerprint(explorer["core"])
        main_stat = explorer["core"].stat()
        writer.execute("UPDATE study_effect SET effect_estimate=0.543 WHERE analysis_version='v1'")
        writer.commit()
        assert explorer["core"].stat().st_mtime_ns == main_stat.st_mtime_ns
        assert fingerprint(explorer["core"]) != prior
        second = explorer["client"].post("/api/v1/explorer/match", json=payload)
        assert first.json()["observed_evidence"][0]["contrasts"][0]["effects"][0]["effect_estimate"] == -0.25
        assert second.json()["observed_evidence"][0]["contrasts"][0]["effects"][0]["effect_estimate"] == 0.543
        assert second.headers["x-explorer-cache"] == "miss"
    finally:
        writer.close()


def test_ordered_public_index_and_receipt_replacement_invalidates_http_cache(explorer, monkeypatch, tmp_path):
    """A valid cached response cannot hide replacement/removal of its sidecar."""
    from nutrigenomics.explorer_performance import source_paths

    index = tmp_path / "ordered.sqlite3"
    receipt = index.with_suffix(".json")
    index.write_bytes(b"source-bound-reference-index")
    receipt.write_text('{"index_sha256":"first"}', encoding="utf-8")
    monkeypatch.setenv("NUTRIGENOMICS_PUBLIC_OMICS_INDEX", str(index))
    assert index in source_paths() and receipt in source_paths()
    client = explorer["client"]
    url = "/api/v1/explorer/entities?kind=food&q=rice"
    first = client.get(url)
    assert first.status_code == 200 and first.headers["x-explorer-cache"] == "miss"
    assert client.get(url).headers["x-explorer-cache"] == "hit"
    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(index.read_bytes())
    replacement.replace(index)
    second = client.get(url)
    assert second.status_code == 200 and second.headers["x-explorer-cache"] == "miss"
    assert second.json() == first.json()
    assert client.get(url).headers["x-explorer-cache"] == "hit"
    receipt.write_text('{"index_sha256":"second"}', encoding="utf-8")
    assert client.get(url).headers["x-explorer-cache"] == "miss"
    assert client.get(url).headers["x-explorer-cache"] == "hit"
    index.unlink()
    assert client.get(url).headers["x-explorer-cache"] == "miss"


def test_byte_lru_and_singleflight():
    cache = ByteCache(10)
    cache.put("one", b"123456")
    cache.put("two", b"abcdef")
    assert cache.get("one") is None and cache.bytes == 6
    first, owner = cache.claim("job")
    follower, other = cache.claim("job")
    assert owner and not other and first is follower
    cache.finish("job", b"done")
    assert follower.result() == b"done" and not cache.flights


def test_concurrent_cache_miss_computes_once(explorer, monkeypatch):
    from nutrigenomics import explorer as module
    original = module._health_anchors
    calls = []
    def slow(*args, **kwargs):
        calls.append(1)
        time.sleep(0.1)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "_health_anchors", slow)
    CACHE.clear()
    def request(_):
        return explorer["client"].get("/api/v1/explorer/graph", params={"seed_id": "food:rice"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(request, range(4)))
    assert all(r.status_code == 200 for r in responses)
    assert len(calls) == 1
    assert len({r.content for r in responses}) == 1


def test_read_error_status_not_empty_result(explorer, monkeypatch):
    _science(explorer)
    from nutrigenomics import explorer_scientific as module
    def fail(*args, **kwargs):
        raise QueryUnavailable("query_timeout")
    monkeypatch.setattr(module, "read_connection", fail)
    response = explorer["client"].post("/api/v1/explorer/match", json={"seed_id": "food:rice"})
    contrast = response.json()["observed_evidence"][0]["contrasts"][0]
    assert contrast["effects_status"] == "query_timeout" and contrast["effect_total"] is None
    assert response.headers["x-explorer-cache"] == "miss"


def test_sql_deadline_releases_connection(explorer):
    from nutrigenomics.explorer_performance import CURRENT, Budget, read_connection
    budget = Budget(0.02)
    token = CURRENT.set(budget)
    started = time.monotonic()
    try:
        with pytest.raises(QueryUnavailable, match="query_timeout"), read_connection(explorer["core"]) as db:
            db.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n").fetchone()
        assert not budget.connections
        assert time.monotonic()-started < 1
    finally:
        CURRENT.reset(token)


def test_derived_rebuild_is_supported(explorer, monkeypatch):
    first, _ = upgrade(explorer, monkeypatch)
    second = first.parent / "performance-two"
    result = build_performance_release(first, explorer["core"], second, "fixture-perf-v2")
    assert result["release_id"] == "fixture-perf-v2"


def test_inherited_index_is_not_used_by_another_release(explorer, monkeypatch):
    _science(explorer)
    first, _ = upgrade(explorer, monkeypatch)
    with sqlite3.connect(first/"explorer.sqlite") as db:
        db.execute("UPDATE meta SET value_json='\"new-unindexed-release\"' WHERE key='release_id'")
        db.execute("DELETE FROM perf_exposure_match")
    response = explorer["client"].post("/api/v1/explorer/match", json={"seed_id":"food:rice"})
    assert response.status_code == 200
    assert response.json()["observed_evidence"]


def test_mid_request_source_change_refuses_mixed_result(explorer, monkeypatch):
    from nutrigenomics import explorer as module
    original = module._health_anchors
    def mutate(*args, **kwargs):
        result=original(*args, **kwargs)
        with sqlite3.connect(explorer["core"]) as db:
            db.execute("UPDATE study_effect SET effect_estimate=0.314 WHERE analysis_version='v1'")
        return result
    monkeypatch.setattr(module,"_health_anchors",mutate)
    response=explorer["client"].get("/api/v1/explorer/graph",params={"seed_id":"food:rice"})
    assert response.status_code == 503 and response.json()["detail"]["code"]=="source_changed"


def test_removed_analysis_is_not_reported_as_zero(explorer, monkeypatch):
    _science(explorer)
    upgrade(explorer,monkeypatch)
    with sqlite3.connect(explorer["core"]) as db:
        db.execute("DELETE FROM study_effect WHERE analysis_version='v1'")
    r=explorer["client"].post("/api/v1/explorer/match",json={"seed_id":"food:rice"})
    c=r.json()["observed_evidence"][0]["contrasts"][0]
    assert c["effects_status"]=="analysis_key_mismatch" and c["effect_total"] is None


def test_export_rights_change_is_immediately_observed(tmp_path,monkeypatch):
    from nutrigenomics.explorer import _export_policies
    path=tmp_path/'rights.json'
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_RIGHTS',str(path))
    path.write_text(json.dumps({'policies':[{'source':'fixture','export_allowed':True}]}))
    assert _export_policies()[0]['export_allowed'] is True
    path.write_text(json.dumps({'policies':[{'source':'fixture','export_allowed':False}]}))
    assert _export_policies()[0]['export_allowed'] is False
