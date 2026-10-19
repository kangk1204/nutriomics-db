"""Logical scientific identity and truthful physical SQLite paths stay distinct."""
import json
from contextlib import contextmanager

import pytest
from test_explorer_api import RELEASE
from test_explorer_api import explorer as explorer  # noqa: PLC0414 -- shared pytest fixture
from test_preview_startup import prepare_fixture

from nutrigenomics import preview_startup, release_contract
from nutrigenomics import source_replicas as replicas
from nutrigenomics.explorer_performance import (
    CACHE,
    canonical,
    logical_connection_path,
    read_connection,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(replicas,'_ACTIVE',None)
    monkeypatch.delenv(replicas.ENV,raising=False)
    monkeypatch.delenv(replicas.SCOPE_ENV,raising=False)
    CACHE.clear()
    yield
    CACHE.clear()
    release_contract._legacy_contract.cache_clear()


def activate(explorer,tmp_path,monkeypatch,scope='internal'):
    monkeypatch.setattr(replicas,'_tmpfs_directory',lambda root:None)
    monkeypatch.setattr(replicas,'resource_check',lambda *args,**kwargs:{})
    paths=[('release',explorer['db'])]+([('core',explorer['core'])] if scope=='internal' else [])
    files=[{'source':str(path),'scope':scope,'role':scope+'_'+kind,'name':kind+'.sqlite3',
            'sha256':replicas.file_sha(path)} for kind,path in paths]
    receipt=replicas.copy_plan({'releases':{scope:RELEASE},'files':files},tmp_path/'ram',tmp_path/'receipts')
    monkeypatch.setenv(replicas.ENV,receipt['maps'][scope]['path'])
    monkeypatch.setenv(replicas.SCOPE_ENV,scope)
    replicas.initialize(scope,RELEASE)


def test_mapped_preview_startup_and_entire_match_json_preserve_keys_stats_permissions(explorer,tmp_path,monkeypatch):
    root,contrasts,receipt=prepare_fixture(explorer)
    payload={'seed_id':'food:rice','intake_g':150}
    baseline=explorer['client'].post('/api/v1/explorer/match',json=payload)
    assert baseline.status_code==200
    CACHE.clear()
    activate(explorer,tmp_path,monkeypatch)
    prepared=preview_startup.prepare_effect_preview_cache(root,'internal',receipt)
    assert prepared['ready'] and prepared['selectors']==len(contrasts)
    with read_connection(explorer['db']) as db:
        assert logical_connection_path(db)==str(explorer['db'].resolve())
        assert db.execute('PRAGMA database_list').fetchone()[2]==str(replicas.physical_read_path(explorer['db']))
        assert logical_connection_path(db)!=db.execute('PRAGMA database_list').fetchone()[2]
    current=explorer['client'].post('/api/v1/explorer/match',json=payload)
    assert current.status_code==200
    assert canonical(current.json())==canonical(baseline.json())
    keys=[key for key in CACHE.items if key[0]=='effects']
    assert keys and all(key[2][0]==str(explorer['db'].resolve()) for key in keys)
    assert prepared['exact_source_totals']==json.loads(receipt.read_text())['exact_source_totals']


def test_public_identifier_search_does_not_reopen_unlisted_physical_database(explorer,tmp_path,monkeypatch):
    params={'kind':'food','q':'rice'}
    baseline=explorer['client'].get('/api/v1/explorer/entities',params=params)
    assert baseline.status_code==200
    CACHE.clear()
    activate(explorer,tmp_path,monkeypatch,scope='public')
    # The public service normally supplies this same context-scoped core value.
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_CORE_DATABASE',str(explorer['db']))
    current=explorer['client'].get('/api/v1/explorer/entities',params=params)
    assert current.status_code==200 and current.json()==baseline.json()


def test_legacy_contract_fallback_uses_logical_identity_and_mapped_reader(explorer,tmp_path,monkeypatch):
    release_contract._legacy_contract.cache_clear()
    with read_connection(explorer['db']) as db:baseline=release_contract.metadata_with_contract(db)
    release_contract._legacy_contract.cache_clear()
    activate(explorer,tmp_path,monkeypatch,scope='public')
    calls=[];original=release_contract.read_connection
    @contextmanager
    def traced(path):
        with original(path) as db:
            calls.append((str(path),db.execute('PRAGMA database_list').fetchone()[2]))
            yield db
    monkeypatch.setattr(release_contract,'read_connection',traced)
    with read_connection(explorer['db']) as db:current=release_contract.metadata_with_contract(db)
    assert current==baseline
    assert calls==[(str(explorer['db'].resolve()),str(replicas.physical_read_path(explorer['db'])))]
