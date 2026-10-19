import json
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nutrigenomics import public_omics_index as index
from nutrigenomics.explorer_performance import QueryUnavailable

KEY = ('GSE_TEST', 'gene', 'after-before', 'v1')


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'public.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE meta(key TEXT PRIMARY KEY,value_json TEXT);
            INSERT INTO meta VALUES('release_id','"public-test-v1"');
            CREATE TABLE effect(id TEXT NOT NULL,series_accession TEXT,feature_type TEXT,feature_id TEXT,contrast TEXT,analysis_version TEXT,effect_estimate REAL,p_value REAL,q_value REAL,standard_error REAL,n_people INTEGER,metadata_json TEXT NOT NULL,rights_policy_id TEXT NOT NULL);
            CREATE UNIQUE INDEX public_effect_id ON effect(id);
            CREATE TABLE effect_selector(series_accession TEXT,feature_type TEXT,contrast TEXT,analysis_version TEXT,total INTEGER,PRIMARY KEY(series_accession,feature_type,contrast,analysis_version));
            CREATE VIEW study_effect AS SELECT id,series_accession,feature_type,feature_id,contrast,analysis_version,effect_estimate,p_value,q_value,standard_error,n_people,rights_policy_id,json_set(metadata_json,'$.export_allowed',json('true'),'$.publication_rights_policy_id',rights_policy_id) AS metadata_json,NULL AS source_file FROM effect;
            CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT);
        ''')
        db.execute('INSERT INTO science_record VALUES(?,?,?)', ('contrast:1','contrast', json.dumps(dict(zip(('series_accession','feature_type','contrast','analysis_version'), KEY)))))
        values = []
        # Native TEXT ids intentionally differ from rowid order; repeated features
        # prove five-part-key multiplicity and stable native id tie ordering.
        for n in reversed(range(87)):
            feature = None if n < 2 else '' if n < 4 else f'F{n // 3:04}'
            values.append((f'effect:{n:04}', KEY[0], KEY[1], feature, KEY[2], KEY[3], None if n == 3 else 0 if n == 4 else -n / 10, None if n == 5 else 0, .8, None if n == 6 else .1, 0, '{"expert_review_status":"pending","export_allowed":false}', 'test-CCBY'))
        db.executemany('INSERT INTO effect VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
        db.execute('INSERT INTO effect_selector VALUES(?,?,?,?,?)', (*KEY, len(values)))
    return path


def prepared(source, monkeypatch):
    output = source.with_name('ordered.sqlite3')
    digest = index.file_sha(source)
    receipt = index.build(source, output, digest, batch_size=9)
    index.verify_index(output, source, 'public-test-v1', digest)
    monkeypatch.setenv(index.ENV, str(output))
    return output, receipt


def test_all_pages_exact_native_view_values_types_rights_and_ids(source, monkeypatch):
    output, receipt = prepared(source, monkeypatch)
    assert receipt['total'] == 87 and receipt['scientific_values_copied'] == 0
    assert receipt['duplicate_five_part_keys'] > 0
    assert receipt['source_sha256'] == receipt['source_sha256_after'] == index.file_sha(source)
    with closing(sqlite3.connect(source)) as db:
        db.row_factory = sqlite3.Row
        for feature in (None, 'F0008', 'not present'):
            for offset in (0, 3, 40, 80, 100):
                total, rows = index.page_rows(db, source, 'public-test-v1', KEY, offset, 7, feature)
                clause, args = (' AND feature_id=?', [feature]) if feature else ('', [])
                expected = [dict(r) for r in db.execute('SELECT * FROM study_effect WHERE series_accession=? AND feature_type=? AND contrast=? AND analysis_version=?' + clause + ' ORDER BY feature_id,id LIMIT 7 OFFSET ?', [*KEY, *args, offset])]
                assert rows == expected
                assert total == (3 if feature == 'F0008' else 0 if feature else 87)
                assert all('_native_row_reference' not in r and type(r['id']) is str for r in rows)
                assert all(json.loads(r['metadata_json'])['export_allowed'] is True for r in rows)
    with sqlite3.connect(output) as db:
        columns = {r[1] for r in db.execute('PRAGMA table_info(raw_ref)')}
        assert columns == {'source_rowid','effect_id','selector_id','feature_id'}


@pytest.mark.parametrize('stop', [1, 12, 15])
def test_checkpoint_resume_exact_no_source_modification(source, monkeypatch, stop):
    output = source.with_name('resume.sqlite3')
    digest = index.file_sha(source)
    stopped = index.build(source, output, digest, batch_size=9, stop_after_batches=stop)
    assert not stopped['complete'] and not output.exists()
    with pytest.raises(ValueError, match='explicit resume'):
        index.build(source, output, digest)
    receipt = index.build(source, output, digest, resume=True, batch_size=11)
    assert receipt['total'] == 87 and index.file_sha(source) == digest
    index.verify_index(output, source, 'public-test-v1', digest)
    monkeypatch.setenv(index.ENV, str(output))
    with closing(sqlite3.connect(source)) as db:
        db.row_factory = sqlite3.Row
        assert len(index.page_rows(db, source, 'public-test-v1', KEY, 80, 20)[1]) == 7


def test_readiness_verification_required_missing_and_changes_fail_not_zero(source, monkeypatch):
    output, _ = prepared(source, monkeypatch)
    with closing(sqlite3.connect(source)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(QueryUnavailable):
            index.page_rows(db, source, 'wrong-release', KEY, 0, 10)
        with pytest.raises(QueryUnavailable):
            index.page_rows(db, source, 'public-test-v1', ('wrong',*KEY[1:]), 0, 10)
        index._VERIFIED.pop(str(output.resolve()))
        with pytest.raises(QueryUnavailable, match='source_unavailable'):
            index.page_rows(db, source, 'public-test-v1', KEY, 0, 10)
    index.verify_index(output, source, 'public-test-v1', index.file_sha(source))
    # Even a short nonempty WAL must be rejected, including <32 byte files that
    # the older fingerprint helper historically treated as empty.
    wal = source.with_name(source.name + '-wal')
    wal.write_bytes(b'changed')
    with closing(sqlite3.connect(':memory:')) as db, pytest.raises(QueryUnavailable):
        index.page_rows(db, source, 'public-test-v1', KEY, 0, 10)
    wal.unlink()
    with sqlite3.connect(source) as db:
        db.execute('UPDATE effect SET effect_estimate=10 WHERE rowid=1')
    with closing(sqlite3.connect(source)) as db, pytest.raises(QueryUnavailable, match='source_changed'):
        index.page_rows(db, source, 'public-test-v1', KEY, 0, 10)


def test_sidecar_tamper_and_native_key_tamper_fail(source, monkeypatch):
    output, _ = prepared(source, monkeypatch)
    with sqlite3.connect(output) as db:
        db.execute("UPDATE raw_ref SET effect_id='wrong' WHERE source_rowid=1")
    with pytest.raises(ValueError, match='binding mismatch'):
        index.verify_index(output, source, 'public-test-v1', index.file_sha(source))


def test_readiness_source_changed_while_hashing_sidecar_never_publishes_ready(source, monkeypatch):
    output, _ = prepared(source, monkeypatch)
    digest = index.file_sha(source)
    index._VERIFIED.pop(str(output.resolve()))
    original = index.file_sha
    def changed(path):
        result = original(path)
        if path == output:
            with sqlite3.connect(source) as db:
                db.execute('UPDATE effect SET effect_estimate=123 WHERE rowid=1')
        return result
    monkeypatch.setattr(index, 'file_sha', changed)
    with pytest.raises(ValueError, match='binding mismatch'):
        index.verify_index(output, source, 'public-test-v1', digest)
    assert str(output.resolve()) not in index._VERIFIED


def test_build_rejects_selector_mismatch_nonempty_wal_and_overwrite(source):
    output = source.with_name('invalid.sqlite3')
    with sqlite3.connect(source) as db:
        db.execute('UPDATE effect_selector SET total=1000')
    with pytest.raises(ValueError, match='totals disagree'):
        index.build(source, output, index.file_sha(source))
    output.write_text('protected')
    with pytest.raises(FileExistsError):
        index.build(source, output, index.file_sha(source))
    assert output.read_text() == 'protected'


def test_range_plan_and_vm_work_bounded_on_deep_pages(source, monkeypatch):
    with sqlite3.connect(source) as db:
        template = db.execute('SELECT * FROM effect WHERE rowid=1').fetchone()
        db.executemany('INSERT INTO effect VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', [(f'large:{n:06}', *template[1:3], f'Z{n:06}', *template[4:]) for n in range(12000)])
        db.execute('UPDATE effect_selector SET total=total+12000')
    output, _ = prepared(source, monkeypatch)
    with sqlite3.connect(output) as db:
        plan = str(db.execute('EXPLAIN QUERY PLAN SELECT r.source_rowid,r.effect_id,r.feature_id FROM page_ref p JOIN raw_ref r ON r.source_rowid=p.source_rowid WHERE p.selector_id=? AND p.ordinal>? AND p.ordinal<=? ORDER BY p.ordinal', (1,11000,11025)).fetchall())
        assert 'PRIMARY KEY' in plan and 'TEMP B-TREE' not in plan
        work = []
        for offset in (0,11000):
            ticks = [0]
            db.set_progress_handler(lambda ticks=ticks: ticks.__setitem__(0,ticks[0]+1) or 0, 1)
            assert len(db.execute('SELECT r.source_rowid,r.effect_id,r.feature_id FROM page_ref p JOIN raw_ref r ON r.source_rowid=p.source_rowid WHERE p.selector_id=? AND p.ordinal>? AND p.ordinal<=? ORDER BY p.ordinal', (1,offset,offset+25)).fetchall()) == 25
            work.append(ticks[0])
            db.set_progress_handler(None,0)
        assert max(work) < 600 and abs(work[0]-work[1]) < 20


def test_public_api_page_cursor_alias_and_full_response_unchanged(source, monkeypatch):
    from nutrigenomics.explorer_workbench import router
    monkeypatch.delenv(index.ENV, raising=False)
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_DATABASE', str(source))
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_CORE_DATABASE', str(source))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    params = {'series':KEY[0],'feature_type':'gene_expression','contrast':KEY[2],'analysis_version':KEY[3],'page_size':7,'page':11}
    original = client.get('/api/v1/explorer/omics/results', params=params)
    assert original.status_code == 200
    prepared(source, monkeypatch)
    improved = client.get('/api/v1/explorer/omics/results', params=params)
    assert improved.json() == original.json()
    following = client.get('/api/v1/explorer/omics/results', params={**params,'cursor':improved.json()['next_cursor']})
    page12 = client.get('/api/v1/explorer/omics/results', params={**params,'page':12})
    assert following.json()['items'] == page12.json()['items']
    mismatch = client.get('/api/v1/explorer/omics/results', params={**params,'cursor':improved.json()['next_cursor'],'feature_id':'changed'})
    assert mismatch.status_code == 409
