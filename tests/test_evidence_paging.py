import hashlib
import json
import sqlite3

import pytest

from nutrigenomics.evidence_paging import build, page_rows, verify_index
from nutrigenomics.explorer_performance import QueryUnavailable


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    source = tmp_path / 'source.sqlite'
    index = tmp_path / 'pages.sqlite'
    with sqlite3.connect(source) as db:
        db.executescript('CREATE TABLE meta(key TEXT,value_json TEXT); CREATE TABLE workbench_evidence(id TEXT PRIMARY KEY,layer TEXT,study_id TEXT,tissue TEXT,metadata_json TEXT,subject_id TEXT,object_id TEXT);')
        db.execute('INSERT INTO meta VALUES(?,?)', ('release_id', json.dumps('release-a')))
        db.executemany('INSERT INTO workbench_evidence VALUES(?,?,?,?,?,?,?)', [
            ('b', 'predicted', None, 'K562', '{"value":null}', 'A','B'),
            ('a', 'predicted', None, 'GM12878', '{"value":0}', 'A','A'),
            ('d', 'predicted', 's1', None, '{"value":-2}', 'C','A'),
            ('c', 'literature', None, None, '{"candidate":true}', 'C','D'),
            ('e', 'predicted', 's1', None, '{"value":3}', 'X','Y')])
    receipt = build(source, index, hashlib.sha256(source.read_bytes()).hexdigest())
    index.with_suffix('.json').write_text(json.dumps(receipt))
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


@pytest.mark.parametrize('seed,layer', [('A','all'),('A','predicted'),('C','all'),('missing','all'),('C','absent')])
def test_entity_pages_preserve_order_values_and_self_loop_deduplication(indexed, seed, layer):
    source, _, db, _ = indexed
    where = '(subject_id=? OR object_id=?)' + (' AND layer=?' if layer!='all' else '')
    args=[seed,seed,*([layer] if layer!='all' else [])]
    expected=[dict(r) for r in db.execute('SELECT * FROM workbench_evidence WHERE '+where+' ORDER BY layer,study_id,tissue,id',args)]
    actual=[]
    for page in range(1,5):
        total,rows=page_rows(db,source,'release-a',layer,page,2,seed)
        assert total==len(expected)
        actual.extend(dict(r) for r in rows)
    assert actual==expected


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


def test_startup_verifies_auxiliary_bytes_and_binding(indexed):
    source, index, _, receipt = indexed
    index.with_suffix('.json').write_text(json.dumps(receipt))
    assert verify_index(index, source, 'release-a', receipt['source_sha256'])['references'] == 5
    with pytest.raises(ValueError, match='binding mismatch'):
        verify_index(index, source, 'wrong-release', receipt['source_sha256'])
    with sqlite3.connect(index) as db:
        db.execute("UPDATE page_ref SET id='different' WHERE ordinal=1")
    with pytest.raises(ValueError, match='binding mismatch'):
        verify_index(index, source, 'release-a', receipt['source_sha256'])


def test_index_change_after_startup_cannot_silently_reorder_real_records(indexed):
    source, index, db, receipt = indexed
    verify_index(index, source, 'release-a', receipt['source_sha256'])
    with sqlite3.connect(index) as writer:
        writer.execute("UPDATE page_ref SET id='temporary' WHERE id='a'")
        writer.execute("UPDATE page_ref SET id='a' WHERE id='b'")
        writer.execute("UPDATE page_ref SET id='b' WHERE id='temporary'")
    with pytest.raises(QueryUnavailable, match='source_mismatch'):
        page_rows(db, source, 'release-a', 'predicted', 1, 2)
