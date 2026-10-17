import hashlib
import json
import sqlite3

import pytest

from nutrigenomics.evidence_paging import build, page_rows
from nutrigenomics.explorer_performance import QueryUnavailable


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    source = tmp_path / 'source.sqlite'
    index = tmp_path / 'pages.sqlite'
    with sqlite3.connect(source) as db:
        db.executescript('CREATE TABLE meta(key TEXT,value_json TEXT); CREATE TABLE workbench_evidence(id TEXT PRIMARY KEY,layer TEXT,study_id TEXT,tissue TEXT,metadata_json TEXT);')
        db.execute('INSERT INTO meta VALUES(?,?)', ('release_id', json.dumps('release-a')))
        db.executemany('INSERT INTO workbench_evidence VALUES(?,?,?,?,?)', [
            ('b', 'predicted', None, 'K562', '{"value":null}'),
            ('a', 'predicted', None, 'GM12878', '{"value":0}'),
            ('d', 'predicted', 's1', None, '{"value":-2}'),
            ('c', 'literature', None, None, '{"candidate":true}'),
            ('e', 'predicted', 's1', None, '{"value":3}')])
    receipt = build(source, index, hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX', str(index))
    with sqlite3.connect(source) as db:
        db.row_factory = sqlite3.Row
        yield source, index, db, receipt


@pytest.mark.parametrize('layer', ['all', 'predicted', 'literature', 'absent'])
def test_all_pages_preserve_native_count_order_and_values(indexed, layer):
    source, _, db, receipt = indexed
    assert receipt['scientific_values_copied'] == 0
    where, args = ('', []) if layer == 'all' else (' WHERE layer=?', [layer])
    native = [dict(row) for row in db.execute('SELECT * FROM workbench_evidence' + where + ' ORDER BY layer,study_id,tissue,id', args)]
    actual = []
    for page in range(1, 6):
        total, rows = page_rows(db, source, 'release-a', layer, page, 2)
        assert total == len(native)
        actual.extend(dict(row) for row in rows)
    assert actual == native


def test_changed_source_and_wrong_release_are_not_zero(indexed):
    source, _, db, _ = indexed
    with pytest.raises(QueryUnavailable, match='source_mismatch'):
        page_rows(db, source, 'release-b', 'all', 1, 2)
    with sqlite3.connect(source) as writer:
        writer.execute("UPDATE workbench_evidence SET metadata_json='{}' WHERE id='a'")
    with pytest.raises(QueryUnavailable, match='source_mismatch'):
        page_rows(db, source, 'release-a', 'all', 1, 2)


def test_missing_reference_is_not_reported_as_evidence_absence(indexed):
    source, index, db, _ = indexed
    with sqlite3.connect(index) as writer:
        writer.execute('DELETE FROM page_ref WHERE ordinal=1')
    with pytest.raises(QueryUnavailable, match='source_mismatch'):
        page_rows(db, source, 'release-a', 'all', 1, 2)


def test_source_hash_and_existing_index_guard(indexed, tmp_path):
    source, index, _, _ = indexed
    with pytest.raises(ValueError, match='SHA-256'):
        build(source, tmp_path / 'bad.sqlite', 'bad')
    with pytest.raises(FileExistsError):
        build(source, index, hashlib.sha256(source.read_bytes()).hexdigest())
