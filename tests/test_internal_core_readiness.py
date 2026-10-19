import hashlib
import json
from pathlib import Path

import pytest

from scripts import serve_verified_release as server


def fixture(tmp_path, monkeypatch):
    source = tmp_path / 'core.sqlite3'
    source.write_bytes(b'full unchanged quantitative source\0' * 1024)
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_CORE_DATABASE', str(source))
    manifest = {'input_hashes': {'core_db': hashlib.sha256(source.read_bytes()).hexdigest()},
                'source_paths': {'core_db': str(source)}}
    return source, manifest


def test_full_core_bytes_manifest_bound_and_time_memory_cache_scope(tmp_path, monkeypatch):
    source, manifest = fixture(tmp_path, monkeypatch)
    result = server.verify_internal_core(manifest)
    assert result['verified'] and result['source_sha256'] == manifest['input_hashes']['core_db']
    assert result['bytes'] == source.stat().st_size
    assert result['source_fingerprint_before'] == result['source_fingerprint_after']
    assert result['http_slo_evidence'] is False and result['cold_query_performance_validated'] is False
    assert 'memory_available_bytes_before' in result and result['checksum_seconds'] >= 0


@pytest.mark.parametrize('failure', ['wrong_sha', 'missing_sha', 'changed_source', 'wal_before', 'wal_during', 'missing_file'])
def test_integrity_failures_never_publish_startup_receipt(tmp_path, monkeypatch, failure):
    source, manifest = fixture(tmp_path, monkeypatch)
    release = tmp_path / 'release';release.mkdir()
    main = release / 'explorer.sqlite';main.write_bytes(b'graph release')
    manifest.update(release_id='internal-v1',database_sha256=server.sha256(main))
    if failure == 'wrong_sha':manifest['input_hashes']['core_db'] = '0' * 64
    elif failure == 'missing_sha':manifest['input_hashes'] = {}
    elif failure == 'wal_before':Path(str(source)+'-wal').write_bytes(b'x')
    elif failure == 'missing_file':source.unlink()
    elif failure in {'changed_source','wal_during'}:
        original = server._core_sha256
        def changed(path, deadline):
            digest = original(path, deadline)
            if path == source:
                if failure == 'changed_source':source.write_bytes(source.read_bytes()+b'change')
                else:Path(str(source)+'-wal').write_bytes(b'x')
            return digest
        monkeypatch.setattr(server,'_core_sha256',changed)
    (release/'manifest.json').write_text(json.dumps(manifest))
    receipt = tmp_path/'startup.json'
    with pytest.raises(ValueError):server.verify(release,'internal',receipt)
    assert not receipt.exists()


def test_public_only_startup_does_not_open_inherited_private_core(tmp_path, monkeypatch):
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_CORE_DATABASE',str(tmp_path/'not-present-private-core'))
    main = tmp_path/'nutriomics.sqlite3';main.write_bytes(b'public dataset')
    (tmp_path/'manifest.json').write_text(json.dumps({'release_id':'public-v1','database_sha256':server.sha256(main),
        'input_hashes':{'core_db':'0'*64},'source_paths':{'core_db':'private-not-available'}}))
    result=server.verify(tmp_path,'public',tmp_path/'startup.json')
    assert 'quantitative_source_verification' not in result


def test_source_budget_failure_denies_ready_and_no_global_tuning(tmp_path, monkeypatch):
    _, manifest = fixture(tmp_path, monkeypatch)
    ticks=iter([1.0,602.0]);monkeypatch.setattr(server.time,'monotonic',lambda:next(ticks))
    with pytest.raises(TimeoutError,match='readiness denied'):server.verify_internal_core(manifest)


def test_undeclared_source_never_claims_integrity_verification():
    result=server.verify_internal_core({})
    assert result['availability']=='not_declared_in_manifest' and result['verified'] is False
    with pytest.raises(ValueError,match='require a manifest-bound'):
        server.verify_internal_core({'quantitative_preservation':{'protected_table_counts_after':{'perf_effect_ref':1}}})
