import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from nutrigenomics import source_replicas as replicas
from nutrigenomics.explorer_performance import CACHE, read_connection


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.delenv(replicas.ENV, raising=False)
    monkeypatch.delenv(replicas.SCOPE_ENV, raising=False)
    monkeypatch.setattr(replicas, '_ACTIVE', None)
    CACHE.clear()
    yield
    CACHE.clear()


def prepared(tmp_path, monkeypatch, *, source=None, scope='internal', role='internal_core', companions=()):
    source = source or tmp_path / 'source.sqlite3'
    if not source.exists():
        with closing(sqlite3.connect(source)) as db:
            db.execute('CREATE TABLE value(id TEXT, effect REAL, p REAL, n INTEGER, rights TEXT)')
            db.executemany('INSERT INTO value VALUES(?,?,?,?,?)',
                [('effect:01', None, 0.0, 0, '{"export_allowed":false}'),
                 ('effect:02', -1.25, None, None, '{"expert_review_status":"pending"}')])
            db.commit()
    plan = {'releases': {scope: 'fixture-v1'}, 'files': [
        {'source': str(source), 'scope': scope, 'role': role, 'name': 'replica.sqlite3',
         'sha256': replicas.file_sha(source), 'companions': [str(p) for p in companions]}]}
    monkeypatch.setattr(replicas, '_tmpfs_directory', lambda root: None)
    monkeypatch.setattr(replicas, 'resource_check', lambda *args, **kwargs: {'test_resource_override': True})
    result = replicas.copy_plan(plan, tmp_path / 'ram', tmp_path / 'receipts')
    monkeypatch.setenv(replicas.ENV, result['maps'][scope]['path'])
    monkeypatch.setenv(replicas.SCOPE_ENV, scope)
    return source, result


def test_whole_file_types_null_zero_stats_permissions_and_logical_fingerprint(tmp_path, monkeypatch):
    source, result = prepared(tmp_path, monkeypatch)
    original = source.read_bytes()
    before = replicas.closed_fingerprint(source)
    initialized = replicas.initialize('internal', 'fixture-v1')
    physical = replicas.physical_read_path(source)
    assert initialized['whole_file_replicas'] == 1
    assert physical != source and physical.read_bytes() == original
    assert replicas.closed_fingerprint(source) == before
    assert initialized['source_freshness_checked_before_response_cache'] is True
    with closing(sqlite3.connect(source)) as baseline, read_connection(source) as actual:
        assert [tuple(row) for row in actual.execute('SELECT * FROM value')] == baseline.execute('SELECT * FROM value').fetchall()
        assert str(physical) in [row[2] for row in actual.execute('PRAGMA database_list')]
        with pytest.raises(sqlite3.OperationalError):
            actual.execute("UPDATE value SET effect=0")
    entry = result['entries'][0]
    assert len({entry[k] for k in ('sha256', 'source_sha256_before', 'source_sha256_after', 'replica_sha256', 'copy_stream_sha256')}) == 1
    assert result['scientific_transformations'] == 0


@pytest.mark.parametrize('change', ['source_replacement', 'source_removed', 'source_wal_one_byte',
                                    'replica_replacement', 'replica_removed', 'replica_wal', 'map_replaced', 'companion_replaced'])
def test_changes_never_serve_old_replica_or_cached_success(tmp_path, monkeypatch, change):
    companion = tmp_path / 'index.json'; companion.write_text('{"sha":"one"}')
    source, result = prepared(tmp_path, monkeypatch, companions=[companion])
    replicas.initialize('internal', 'fixture-v1')
    physical = replicas.physical_read_path(source)
    # Windows' read-only attribute must be cleared before a replacement attempt;
    # the subsequent fingerprint guard must still reject that local mutation.
    os.chmod(physical, 0o600)
    # Reinitialize the expected fingerprint for the test's genuine replacement.
    row = replicas._ACTIVE['entries'][str(source.resolve())]
    row['replica_fingerprint'] = replicas.closed_fingerprint(physical)
    mapping = Path(result['maps']['internal']['path'])
    if change == 'source_replacement':
        new = source.with_suffix('.new');new.write_bytes(source.read_bytes());os.replace(new, source)
    elif change == 'source_removed': source.unlink()
    elif change == 'source_wal_one_byte': Path(str(source)+'-wal').write_bytes(b'x')
    elif change == 'replica_replacement':
        new = physical.with_suffix('.new');new.write_bytes(physical.read_bytes());os.replace(new, physical)
    elif change == 'replica_removed': physical.unlink()
    elif change == 'replica_wal': Path(str(physical)+'-wal').write_bytes(b'x')
    elif change == 'map_replaced':
        new = mapping.with_suffix('.new');new.write_bytes(mapping.read_bytes());os.replace(new, mapping)
    elif change == 'companion_replaced': companion.write_text('{"sha":"changed"}')
    with pytest.raises(replicas.ReplicaUnavailable):replicas.guard()
    with pytest.raises(replicas.ReplicaUnavailable):replicas.physical_read_path(source)


def test_source_guard_precedes_real_http_cache_hit(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nutrigenomics.explorer_performance import ExplorerReadMiddleware

    source, _ = prepared(tmp_path, monkeypatch)
    replicas.initialize('internal', 'fixture-v1')
    app = FastAPI(); calls = []
    @app.get('/api/v1/explorer/proof')
    def proof():
        calls.append(True)
        return {'effect': None, 'reported_zero': 0, 'expert_review_status': 'pending'}
    app.add_middleware(ExplorerReadMiddleware)
    with TestClient(app) as client:
        one = client.get('/api/v1/explorer/proof')
        hit = client.get('/api/v1/explorer/proof')
        assert one.status_code == hit.status_code == 200
        assert hit.headers['x-explorer-cache'] == 'hit' and len(calls) == 1
        Path(str(source)+'-wal').write_bytes(b'x')
        failure = client.get('/api/v1/explorer/proof')
        assert failure.status_code == 503
        assert failure.json()['detail']['code'] == 'source_changed'
        assert 'x-explorer-cache' not in failure.headers and len(calls) == 1


def test_public_map_cannot_initialize_private_scope_before_reading_private_file(tmp_path, monkeypatch):
    _, result = prepared(tmp_path, monkeypatch)
    monkeypatch.setenv(replicas.SCOPE_ENV, 'public')
    called = []
    monkeypatch.setattr(replicas, 'file_sha', lambda p, *args: called.append(str(p)))
    with pytest.raises(replicas.ReplicaUnavailable, match='scope'):
        replicas.initialize('public', 'fixture-v1')
    assert called == []
    value = json.loads(Path(result['maps']['internal']['path']).read_text())
    value['scope'] = 'public'
    Path(result['maps']['internal']['path']).write_text(json.dumps(value))
    with pytest.raises(replicas.ReplicaUnavailable, match='Cross-scope'):
        replicas.initialize('public', 'fixture-v1')
    assert called == []


def test_readiness_rejects_bad_replica_digest_and_unverified_map(tmp_path, monkeypatch):
    source, result = prepared(tmp_path, monkeypatch)
    with pytest.raises(replicas.ReplicaUnavailable, match='not passed readiness'):
        replicas.physical_read_path(source)
    mapping = Path(result['maps']['internal']['path'])
    value = json.loads(mapping.read_text());value['entries'][0]['sha256'] = '0'*64
    mapping.write_text(json.dumps(value))
    with pytest.raises(replicas.ReplicaUnavailable, match='checksum'):
        replicas.initialize('internal', 'fixture-v1')


@pytest.mark.parametrize('available,cap,pending,error', [
    (25*1024**3, 0, 2*1024**3, '24 GiB'),
    (100*1024**3, 42*1024**3, 1, '42 GiB'),
])
def test_hard_memory_and_project_file_caps(tmp_path, monkeypatch, available, cap, pending, error):
    monkeypatch.setattr(replicas, 'memory_available', lambda: available)
    monkeypatch.setattr(replicas, 'cgroup_budget', dict)
    monkeypatch.setattr(replicas, 'commit_snapshot', lambda: ({}, {'overcommit_memory':'1'}))
    monkeypatch.setattr(replicas, '_directory_bytes', lambda root: cap)
    monkeypatch.setattr(replicas.shutil, 'disk_usage', lambda root: type('Usage', (), {'free': 200*1024**3})())
    with pytest.raises(ValueError, match=error):replicas.resource_check(tmp_path, pending_bytes=pending)


def test_cgroup_reserve_is_enforced_even_with_free_host_ram(tmp_path, monkeypatch):
    monkeypatch.setattr(replicas, 'memory_available', lambda: 100*1024**3)
    monkeypatch.setattr(replicas, 'cgroup_budget', lambda: {'memory.max': str(30*1024**3), 'memory.current': str(8*1024**3)})
    monkeypatch.setattr(replicas, 'commit_snapshot', lambda: ({}, {'overcommit_memory':'1'}))
    with pytest.raises(ValueError, match='24 GiB'):replicas.resource_check(tmp_path)


def test_source_change_during_copy_publishes_no_usable_map(tmp_path, monkeypatch):
    source = tmp_path/'source.sqlite3';source.write_bytes(b'unchanged-original')
    plan = {'releases': {'public':'fixture-v1'}, 'files':[{'source':str(source), 'scope':'public',
        'role':'public_release', 'name':'public.sqlite3', 'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]}
    monkeypatch.setattr(replicas, '_tmpfs_directory', lambda root: None)
    monkeypatch.setattr(replicas, 'resource_check', lambda *a, **kw: {})
    original = replicas.file_sha; count = []
    def changed(path, check=None):
        digest = original(path, check)
        count.append(str(path))
        if len(count) == 1:source.write_bytes(source.read_bytes()+b'changed')
        return digest
    monkeypatch.setattr(replicas, 'file_sha', changed)
    with pytest.raises(replicas.ReplicaUnavailable):replicas.copy_plan(plan,tmp_path/'ram',tmp_path/'receipts')
    assert not (tmp_path/'receipts/public-map.json').exists()
    assert json.loads((tmp_path/'receipts/copy.json').read_text())['complete'] is False


def test_orm_maps_readonly_source_but_preserves_mutable_history(tmp_path, monkeypatch):
    from sqlalchemy import text

    from nutrigenomics.db import get_engine

    source, _ = prepared(tmp_path, monkeypatch)
    replicas.initialize('internal', 'fixture-v1')
    readonly = get_engine('sqlite:///'+str(source))
    mutable = get_engine('sqlite:///'+str(tmp_path/'history.sqlite3'))
    try:
        with readonly.connect() as connection:
            assert connection.execute(text('SELECT effect,p,n FROM value ORDER BY id')).all() == [(None,0.0,0),(-1.25,None,None)]
            assert connection.execute(text('PRAGMA database_list')).first()[2] == str(replicas.physical_read_path(source))
            assert connection.execute(text('PRAGMA query_only')).scalar_one() == 1
        with mutable.begin() as connection:
            connection.execute(text('CREATE TABLE meal(id INTEGER)'))
            connection.execute(text('INSERT INTO meal VALUES(1)'))
            assert connection.execute(text('PRAGMA query_only')).scalar_one() == 0
        Path(str(source)+'-wal').write_bytes(b'x')
        with readonly.connect() as connection, pytest.raises(replicas.ReplicaUnavailable):
            connection.execute(text('SELECT * FROM value'))
    finally:
        readonly.dispose();mutable.dispose()


def test_late_change_before_headers_emits_only503_and_cleans_nested_context(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nutrigenomics.explorer_performance import CURRENT, ExplorerReadMiddleware

    source, _ = prepared(tmp_path, monkeypatch)
    replicas.initialize('internal', 'fixture-v1')
    app = FastAPI()
    @app.get('/api/v1/foods')
    def changed():
        Path(str(source)+'-wal').write_bytes(b'x')
        return {'total':0}  # This response must never be committed.
    app.add_middleware(ExplorerReadMiddleware)
    app.add_middleware(replicas.ReplicaGuardMiddleware)
    with TestClient(app) as client:
        response = client.get('/api/v1/foods')
    assert response.status_code == 503 and 'total' not in response.json()
    assert CURRENT.get() is None and not CACHE.flights


def test_public_readiness_uses_only_public_map_without_private_core(tmp_path, monkeypatch):
    from scripts import serve_verified_release as server

    release = tmp_path/'release';release.mkdir()
    source = release/'nutriomics.sqlite3';source.write_bytes(b'public-only-whole-file')
    source, result = prepared(tmp_path, monkeypatch, source=source, scope='public', role='public_release')
    private = tmp_path/'missing-private-core.sqlite3'
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_CORE_DATABASE',str(private))
    (release/'manifest.json').write_text(json.dumps({'release_id':'fixture-v1',
        'database_sha256':replicas.file_sha(source),
        'source_paths':{'core_db':str(private)},'input_hashes':{'core_db':'0'*64}}))
    observed = []
    original = replicas.closed_fingerprint
    def checked(path):
        observed.append(Path(path))
        assert Path(path) != private
        return original(path)
    monkeypatch.setattr(replicas,'closed_fingerprint',checked)
    value = server.verify(release,'public',tmp_path/'startup.json')
    assert value['source_replicas']['whole_file_replicas'] == 1
    assert 'quantitative_source_verification' not in value
    assert result['maps'].keys() == {'public'}
    assert private not in observed
    with pytest.raises(replicas.ReplicaUnavailable,match='unlisted database'):
        replicas.physical_read_path(private)


def test_activated_mapping_cannot_be_silently_disabled_or_rescoped(tmp_path,monkeypatch):
    source,_=prepared(tmp_path,monkeypatch)
    replicas.initialize('internal','fixture-v1')
    monkeypatch.setenv(replicas.SCOPE_ENV,'public')
    with pytest.raises(replicas.ReplicaUnavailable,match='scope changed'):replicas.guard()
    monkeypatch.setenv(replicas.SCOPE_ENV,'internal')
    monkeypatch.delenv(replicas.ENV)
    with pytest.raises(replicas.ReplicaUnavailable,match='silently disabled'):replicas.physical_read_path(source)


def test_public_ordered_index_keeps_logical_binding_and_all_native_values(tmp_path, monkeypatch):
    from nutrigenomics import public_omics_index as paging

    source = tmp_path/'public.sqlite3'
    with closing(sqlite3.connect(source)) as db:
        db.executescript('''
            CREATE TABLE meta(key TEXT,value_json TEXT);
            INSERT INTO meta VALUES('release_id','"fixture-v1"');
            CREATE TABLE effect(id TEXT,series_accession TEXT,feature_type TEXT,feature_id TEXT,contrast TEXT,analysis_version TEXT,effect_estimate REAL,p_value REAL,q_value REAL,metadata_json TEXT);
            CREATE UNIQUE INDEX native_id ON effect(id);
            CREATE TABLE effect_selector(series_accession TEXT,feature_type TEXT,contrast TEXT,analysis_version TEXT,total INTEGER);
            INSERT INTO effect_selector VALUES('GSE1','gene','after-before','v1',3);
            INSERT INTO effect VALUES('Z','GSE1','gene','APOE','after-before','v1',NULL,0,NULL,'{"export_allowed":false}');
            INSERT INTO effect VALUES('A','GSE1','gene','APOE','after-before','v1',0,NULL,0,'{"export_allowed":true}');
            INSERT INTO effect VALUES('B','GSE1','gene','Z','after-before','v1',-1.5,0.02,0.9,'{"expert_review_status":"pending"}');
            CREATE VIEW study_effect AS SELECT * FROM effect;
        ''')
    sidecar = tmp_path/'ordered.sqlite3'
    paging.build(source,sidecar,paging.file_sha(source),batch_size=2)
    source, result = prepared(tmp_path,monkeypatch,source=source,scope='public',role='public_release')
    map_path = Path(result['maps']['public']['path'])
    value=json.loads(map_path.read_text())
    target=Path(value['ram_root'])/'public/ordered.sqlite3'
    target.write_bytes(sidecar.read_bytes());os.chmod(target,0o400)
    value['entries'].append({'scope':'public','role':'public_omics','source':str(sidecar),
        'replica':str(target),'sha256':replicas.file_sha(sidecar),'bytes':sidecar.stat().st_size,
        'source_fingerprint':replicas.closed_fingerprint(sidecar),
        'replica_fingerprint':replicas.closed_fingerprint(target)})
    map_path.write_text(json.dumps(value))
    replicas.initialize('public','fixture-v1')
    monkeypatch.setenv(paging.ENV,str(sidecar))
    paging.verify_index(sidecar,source,'fixture-v1',paging.file_sha(source))
    binding_before=paging._VERIFIED[str(sidecar)][1]
    with read_connection(source) as db:
        total, rows=paging.page_rows(db,source,'fixture-v1',('GSE1','gene','after-before','v1'),0,10)
    with closing(sqlite3.connect(source)) as baseline:
        baseline.row_factory=sqlite3.Row
        expected=[dict(r) for r in baseline.execute('SELECT * FROM study_effect ORDER BY feature_id,id')]
    assert rows == expected and total == 3
    assert binding_before == replicas.closed_fingerprint(source) or json.loads(json.dumps(binding_before)) == replicas.closed_fingerprint(source)
