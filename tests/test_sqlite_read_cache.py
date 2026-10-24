import hashlib,sqlite3,os
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nutrigenomics import sqlite_read_cache as cache,source_replicas
from nutrigenomics.explorer_performance import read_connection

@pytest.fixture
def ready(tmp_path,monkeypatch):
    cache.close();path=tmp_path/'source.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE fact(id INTEGER PRIMARY KEY,value REAL)')
        db.executemany('INSERT INTO fact VALUES(?,?)',[(1,None),(2,0.0),(3,-.2)])
    db.close()
    monkeypatch.delenv(source_replicas.ENV,raising=False)
    monkeypatch.setattr(source_replicas,'memory_available',lambda:64*1024**3)
    report=cache.initialize({path:8},{str(path):source_replicas.closed_fingerprint(path)})
    yield path,report
    cache.close()

def test_native_fields_null_zero_identity_and_retained_cache_limit(ready):
    path,report=ready;before=hashlib.sha256(path.read_bytes()).hexdigest()
    for _ in range(3):
        with read_connection(path) as db:
            assert [tuple(r) for r in db.execute('SELECT * FROM fact')]==[(1,None),(2,0.),(3,-.2)]
            assert db.execute('PRAGMA cache_size').fetchone()[0]==-8192
            assert db.execute('PRAGMA query_only').fetchone()[0]==1
            assert Path(db.execute('PRAGMA database_list').fetchone()[2]).resolve()==path
            with pytest.raises(sqlite3.OperationalError):db.execute('DELETE FROM fact')
    assert report['total_limit_mib']==8 and not report['whole_file_copy']
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before

@pytest.mark.parametrize('change',['replace','remove','wal'])
def test_source_change_precedes_cached_response(ready,change):
    if change=='remove' and os.name=='nt':
        pytest.skip('Windows denies deleting an open immutable SQLite file; actual removal is checked on Linux')
    path,_=ready;app=FastAPI();app.add_middleware(source_replicas.ReplicaGuardMiddleware);seen=[]
    @app.get('/api/v1/cached')
    def get():seen.append(1);return {'cached':'previous_success'}
    client=TestClient(app);assert client.get('/api/v1/cached').status_code==200
    if change=='replace':path.write_bytes(path.read_bytes()+b'changed')
    elif change=='remove':path.unlink()
    else:Path(str(path)+'-wal').write_bytes(b'x')
    response=client.get('/api/v1/cached')
    assert response.status_code==503 and response.json()['detail']['code']=='source_changed' and seen==[1]

def test_unverified_input_and_resource_limits_are_rejected(tmp_path,monkeypatch):
    cache.close();path=tmp_path/'input.db'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE fact(x)')
    db.close();monkeypatch.setattr(source_replicas,'memory_available',lambda:64*1024**3)
    with pytest.raises(ValueError,match='same verified'):cache.initialize({path:8},{})
    with pytest.raises(ValueError,match='2048'):cache.initialize({path:2049},{})
    monkeypatch.setattr(source_replicas,'memory_available',lambda:24*1024**3)
    with pytest.raises(ValueError,match='reserve'):cache.initialize({path:8},{})

def test_inactive_cache_preserves_default_reader(tmp_path):
    cache.close();assert cache.connection_settings(tmp_path/'a.db',tmp_path/'a.db') is None
