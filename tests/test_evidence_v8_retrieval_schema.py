import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"literature_service"))
from nutri_literature.evidence_v8.retrieval import record_schema_probe, record_stream


def database(rows, modern=False, reverse=False):
    db = sqlite3.connect(":memory:")
    schema = "kind TEXT,native_id TEXT,names TEXT,taxon INTEGER,source TEXT"
    if modern:
        schema += ",description TEXT"
    db.execute("CREATE TABLE records("+schema+")")
    db.execute("CREATE INDEX ids ON records(kind,native_id)")
    for row in reversed(rows) if reverse else rows:
        db.execute("INSERT INTO records VALUES("+",".join("?" for _ in row)+")", row)
    return db


def fixture(reverse=False):
    base = database([
        ("gene", "1", json.dumps(["old symbol", "shared"]), 9606, "legacy-official"),
        ("gene", "2", json.dumps(["ambiguous"]), 10090, "legacy-official"),
        ("chemical", "D1", json.dumps(["compound"]), None, "MeSH"),
    ], reverse=reverse)
    overlay = database([
        ("gene", "1", json.dumps(["HGNC approved symbol", "shared"]), 9606, "HGNC-approved", "Official definition"),
        ("gene", "1", json.dumps(["NCBI alias"]), 9606, "NCBI-gene-info", "Additional description"),
        ("gene", "2", json.dumps(["other taxon"]), 10116, "NCBI-gene-info", "Different species"),
        ("chemical", "D1", json.dumps(["chemical alias"]), None, "MeSH-overlay", None),
    ], modern=True, reverse=reverse)
    return SimpleNamespace(base=base, overlay=overlay)


def test_real_legacy_schema_without_description_is_supported():
    kb = fixture()
    try:
        assert "description" not in {r[1] for r in kb.base.execute("PRAGMA table_info(records)")}
        records = list(record_stream(kb))
        assert len(records) == 3
        gene = next(r for r in records if (r["kind"], r["native_id"]) == ("gene", "1"))
        assert set(gene["names"]) == {"old symbol", "shared", "HGNC approved symbol", "NCBI alias"}
        assert gene["description"] == "Additional description; Official definition"
        assert gene["source_records"] == 3
        assert gene["taxon"] == 9606
    finally:
        kb.base.close()
        kb.overlay.close()


def test_row_and_database_order_cannot_drop_official_aliases_or_descriptions():
    a, b = fixture(), fixture(reverse=True)
    try:
        expected = list(record_stream(a))
        assert list(record_stream(b)) == expected
        assert list(record_stream(SimpleNamespace(base=a.overlay, overlay=a.base))) == expected
    finally:
        for kb in (a, b):
            kb.base.close()
            kb.overlay.close()


def test_taxon_conflict_is_preserved_instead_of_selecting_one_species():
    kb = fixture()
    try:
        record = next(r for r in record_stream(kb) if r["kind"] == "gene" and r["native_id"] == "2")
        assert record["taxa"] == [10090, 10116]
        assert record["taxon"] is None
        assert record["taxon_conflict"]
    finally:
        kb.base.close()
        kb.overlay.close()


def test_probe_is_read_only_and_reports_both_schemas():
    kb = fixture()
    try:
        before = (kb.base.total_changes, kb.overlay.total_changes)
        value = record_schema_probe(kb)
        assert (kb.base.total_changes, kb.overlay.total_changes) == before
        assert value["source_rows"] == {"base": 3, "overlay": 4}
        assert value["merged_identities"] == 3
        assert value["taxon_conflicts"] == 1
        assert not value["gold_labels_read"]
        assert not value["source_databases_modified"]
    finally:
        kb.base.close()
        kb.overlay.close()


def test_missing_required_name_column_is_an_explicit_error():
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    try:
        a.execute("CREATE TABLE records(kind TEXT,native_id TEXT)")
        b.execute("CREATE TABLE records(kind TEXT,native_id TEXT,names TEXT)")
        with pytest.raises(ValueError, match="names"):
            list(record_stream(SimpleNamespace(base=a, overlay=b)))
    finally:
        a.close()
        b.close()


def test_numeric_legacy_identifier_type_still_merges_in_text_order():
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    try:
        a.execute("CREATE TABLE records(kind TEXT,native_id INTEGER,names TEXT)")
        b.execute("CREATE TABLE records(kind TEXT,native_id TEXT,names TEXT,description TEXT)")
        a.executemany("INSERT INTO records VALUES('gene',?,?)", [(2, '["two"]'), (10, '["ten"]')])
        b.executemany("INSERT INTO records VALUES('gene',?,?,?)", [("2", '["two alias"]', "description"), ("10", '["ten alias"]', "description")])
        records = list(record_stream(SimpleNamespace(base=a, overlay=b)))
        assert [r["native_id"] for r in records] == ["10", "2"]
        assert all(len(r["names"]) == 2 for r in records)
    finally:
        a.close()
        b.close()
