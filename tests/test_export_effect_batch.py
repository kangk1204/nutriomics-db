"""Export batching preserves the native scalar preview and complete snapshot."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager

import pytest
from test_explorer_api import PRIVATE, RELEASE
from test_explorer_api import (
    explorer as explorer,  # noqa: PLC0414 -- shared real SQLite/API fixture
)

from nutrigenomics import effect_batch
from nutrigenomics import explorer as api
from nutrigenomics.explorer_performance import CURRENT, Budget, QueryUnavailable


def native(explorer):
    with sqlite3.connect(explorer["core"]) as db:
        db.execute("CREATE INDEX ix_study_effect_feature_id ON study_effect(feature_id)")


def node(identifier="NCBIGene:41", label="ENTPD1", aliases=None):
    return {"id": identifier, "kind": "gene", "label": label,
            "metadata": {"verified_feature_ids": aliases or ["ENSG_FIXTURE"]}}


def populate(explorer, people=12):
    native(explorer)
    identifiers = []
    with sqlite3.connect(explorer["db"]) as projection, sqlite3.connect(explorer["core"]) as core:
        for i in range(people):
            identifier, label = f"NCBIGene:{1000+i}", f"GENE{i:02}"
            identifiers.append(identifier)
            projection.execute("INSERT INTO node VALUES(?,?,?,?,?,?,?,?)", (
                identifier, "gene", label, label, "[]", json.dumps({"source_ids": ["open"], "verified_feature_ids": ["SHARED_ALIAS"]}), "[]", 0))
            for j in range(64):
                core.execute("INSERT INTO study_effect(series_accession,feature_type,feature_id,feature_label,contrast,analysis_version,effect_estimate,standard_error,p_value,q_value,n_people,n_samples,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    "GSE2" if j % 2 else "GSE1", ["gene", "expression", "gene_expression", "transcript"][j % 4],
                    label, label, f"C{j % 3}", "v1", None if j % 2 else 0.0,
                    None if j % 2 else 0.0, None if j % 2 else 0.0, None if j % 2 else 0.0,
                    0 if j % 2 else 9, 0 if j % 2 else 72,
                    json.dumps({"export_allowed": j % 3 == 0, "source_file": PRIVATE, "contrast_assay_count": 0})))
        for feature_type in ["cpg", "pathway", "methylation"]:
            core.execute("INSERT INTO study_effect(series_accession,feature_type,feature_id,contrast,analysis_version,metadata_json) VALUES('GSE0',?,'SHARED_ALIAS','C0','v0','{}')", (feature_type,))
        # A shared alias legitimately appears in multiple node previews. Keep
        # both previews and native duplicate records; do not merge namespaces.
        for _ in range(2):
            core.execute("INSERT INTO study_effect(series_accession,feature_type,feature_id,contrast,analysis_version,effect_estimate,metadata_json) VALUES('GSE1','gene','SHARED_ALIAS','C0','v1',0,'{\"export_allowed\":true}')")
    return identifiers


def full_nodes(explorer, identifiers):
    with sqlite3.connect(explorer["db"]) as db:
        db.row_factory = sqlite3.Row
        return [api._node(db.execute("SELECT * FROM node WHERE id=?", (identifier,)).fetchone()) for identifier in identifiers]


def test_native_batch_exact_scalar_order_type_alias_limit_null_zero_rights(explorer):
    identifiers = populate(explorer)
    nodes = full_nodes(explorer, identifiers)
    expected = {n["id"]: api._effects(n) for n in nodes}
    actual = api._batch_export_effects(nodes + [nodes[0]])
    assert actual == expected
    assert all(len(values) == 50 for values in actual.values())
    assert actual[nodes[0]["id"]][:2] == expected[nodes[0]["id"]][:2]
    assert all(row["feature_type"] in api._GENE_EFFECT_TYPES for values in actual.values() for row in values)
    assert {r["effect_estimate"] for r in actual[nodes[0]["id"]]} == {None, 0.0}
    assert {r["p_value"] for r in actual[nodes[0]["id"]]} == {None, 0.0}
    assert {r["export_allowed"] for r in actual[nodes[0]["id"]]} == {True, False}


def test_complete_http_export_json_and_effect_hash_unchanged_with_fewer_connections(explorer, monkeypatch, record_property):
    identifiers = populate(explorer)
    payload = {"release_id": RELEASE, "format": "json", "node_ids": ["food:rice", *identifiers], "edge_ids": []}
    native_batch = api._batch_export_effects
    original_read = api.read_connection
    calls = []

    @contextmanager
    def counted_read(path):
        if str(path) == str(explorer["core"]):
            calls.append(str(path))
        with original_read(path) as db:
            yield db

    monkeypatch.setattr(api, "read_connection", counted_read)
    monkeypatch.setattr(api, "_batch_export_effects", lambda nodes, limit=50: None)
    before = explorer["client"].post("/api/v1/explorer/export", json=payload)
    scalar_connections = len(calls)
    calls.clear()
    monkeypatch.setattr(api, "_batch_export_effects", native_batch)
    after = explorer["client"].post("/api/v1/explorer/export", json=payload)
    assert before.status_code == after.status_code == 200
    assert before.json() == after.json()
    assert hashlib.sha256(before.content).hexdigest() == hashlib.sha256(after.content).hexdigest()
    assert scalar_connections == len(identifiers)
    assert len(calls) == 1
    record_property("scope", "Real SQLite/API fixture, not operating-server latency")
    record_property("scalar_core_connections", scalar_connections)
    record_property("batch_core_connections", len(calls))
    record_property("scalar_snapshot_sha256", hashlib.sha256(before.content).hexdigest())
    record_property("batch_snapshot_sha256", hashlib.sha256(after.content).hexdigest())
    left = [d["effects"] for d in before.json()["node_details"].values()]
    right = [d["effects"] for d in after.json()["node_details"].values()]
    assert hashlib.sha256(json.dumps(left, sort_keys=True).encode()).digest() == hashlib.sha256(json.dumps(right, sort_keys=True).encode()).digest()
    record_property("scalar_effects_sha256", hashlib.sha256(json.dumps(left, sort_keys=True).encode()).hexdigest())
    record_property("batch_effects_sha256", hashlib.sha256(json.dumps(right, sort_keys=True).encode()).hexdigest())
    for identifier in identifiers:
        assert before.json()["node_details"][identifier]["withheld_effect_previews"] == after.json()["node_details"][identifier]["withheld_effect_previews"]


def test_export_does_not_change_ordinary_node_detail_route(explorer, monkeypatch):
    native(explorer)
    monkeypatch.setattr(api, "_batch_export_effects", lambda *a, **k: pytest.fail("ordinary node detail must remain scalar"))
    assert explorer["client"].get("/api/v1/explorer/nodes/NCBIGene:41").status_code == 200


def test_reference_lookup_uses_covering_index_without_effect_payload_reads(explorer):
    native(explorer)
    with sqlite3.connect(explorer["core"]) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT id,feature_id FROM study_effect INDEXED BY ix_study_effect_feature_id WHERE feature_id IN (?,?) ORDER BY feature_id,id", ("ENTPD1", "ENSG_FIXTURE")).fetchall()
    assert any("COVERING INDEX ix_study_effect_feature_id" in row[3] for row in plan)


def test_view_and_unsupported_index_return_only_explicit_scalar_fallback(explorer):
    assert api._batch_export_effects([node()]) is None
    native(explorer)
    with sqlite3.connect(explorer["core"]) as db:
        db.execute("ALTER TABLE study_effect RENAME TO source_effect")
        db.execute("CREATE VIEW study_effect AS SELECT * FROM source_effect")
    assert api._batch_export_effects([node()]) is None


def test_missing_source_is_not_an_empty_preview(explorer, monkeypatch):
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", str(explorer["core"].with_name("missing.sqlite")))
    with pytest.raises(QueryUnavailable, match="source_unavailable"):
        api._batch_export_effects([node()])


def test_no_gene_selection_does_not_require_core(explorer, monkeypatch):
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", str(explorer["core"].with_name("missing.sqlite")))
    assert api._batch_export_effects([{"id": "food", "kind": "food"}]) == {}


@pytest.mark.parametrize("failure", ["missing", "wrong_feature", "wrong_analysis_key"])
def test_missing_or_wrong_hydrated_reference_fails_without_scalar_fallback(explorer, monkeypatch, failure):
    native(explorer)
    original = effect_batch.read_rows_by_ids

    def changed(source, ids, chunk_size=400):
        rows = original(source, ids, chunk_size)
        key = next(iter(rows))
        if failure == "missing":
            rows.pop(key)
        else:
            value = dict(rows[key])
            value["feature_id" if failure == "wrong_feature" else "analysis_version"] = "OTHER" if failure == "wrong_feature" else None
            rows[key] = value
        return rows

    monkeypatch.setattr(effect_batch, "read_rows_by_ids", changed)
    with pytest.raises(QueryUnavailable, match="analysis_key_mismatch"):
        api._batch_export_effects([node()])


def test_source_fingerprint_change_is_not_cached_or_empty(explorer, monkeypatch):
    native(explorer)
    actual = api.source_fingerprint(explorer["core"])
    count = 0

    def changed(path):
        nonlocal count
        count += 1
        return actual if count == 1 else (*actual, "changed")

    monkeypatch.setattr(api, "source_fingerprint", changed)
    with pytest.raises(QueryUnavailable, match="source_changed"):
        api._batch_export_effects([node()])


def test_cancelled_request_keeps_timeout_and_next_request_succeeds(explorer):
    native(explorer)
    token = CURRENT.set(Budget(0))
    try:
        with pytest.raises(QueryUnavailable, match="query_timeout"):
            api._batch_export_effects([node()])
    finally:
        CURRENT.reset(token)
    assert api._batch_export_effects([node()])[node()["id"]] == api._effects(node())


def test_read_only_source_file_hash_does_not_change(explorer):
    native(explorer)
    before = hashlib.sha256(explorer["core"].read_bytes()).hexdigest()
    api._batch_export_effects([node()])
    assert hashlib.sha256(explorer["core"].read_bytes()).hexdigest() == before
