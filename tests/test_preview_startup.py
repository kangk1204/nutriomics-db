"""Readiness preparation has its own bounded budget; failures never become ready."""
import json
import os
from contextlib import contextmanager

import pytest
from test_explorer_api import RELEASE
from test_explorer_api import explorer as explorer  # noqa: PLC0414 -- shared pytest fixture
from test_scientific_effect_batch import setup_batch

from nutrigenomics import explorer_scientific as scientific
from nutrigenomics import preview_startup
from nutrigenomics.explorer_build import sha256
from nutrigenomics.explorer_performance import CACHE, CURRENT, Budget, QueryUnavailable, fingerprint


def prepare_fixture(fixture):
    contrasts = setup_batch(fixture)
    root=fixture['db'].parent
    (root/'manifest.json').write_text(json.dumps({'release_id':RELEASE,'database_sha256':sha256(fixture['db'])}))
    CACHE.clear()
    return root,contrasts,root/'previews.json'


def test_preparation_preserves_full_normal_json_stats_rights_zero_and_null(explorer, monkeypatch):
    root,contrasts,receipt=prepare_fixture(explorer)
    payload={'seed_id':'food:rice','intake_g':150}
    baseline=explorer['client'].post('/api/v1/explorer/match',json=payload).json();CACHE.clear()
    core_reads=[];original=scientific.read_connection
    @contextmanager
    def trace(path):
        core_reads.append(str(path))
        with original(path) as db:yield db
    monkeypatch.setattr(scientific,'read_connection',trace)
    report=preview_startup.prepare_effect_preview_cache(root,'internal',receipt)
    assert report['ready'] and report['selectors']==len(contrasts) and report['serialized_bytes']>0
    assert report['preparation_seconds']>=0 and report['http_slo_evidence'] is False
    assert report['exact_source_totals'][scientific.selector_key(next(c for c in contrasts if c['contrast']=='zero'))]==0
    assert len(core_reads)==1
    core_reads.clear()
    current=explorer['client'].post('/api/v1/explorer/match',json=payload).json()
    assert current==baseline and core_reads==[]
    cached=[json.loads(value) for key,value in CACHE.items.items() if isinstance(key,tuple) and key[0]=='effects']
    effects=[row for rows,_,status in cached for row in rows]
    assert any(row['effect_estimate'] is None and row['q_value'] is None and row['export_allowed'] is False for row in effects)
    assert any(row['effect_estimate']==0 and row['export_allowed'] is True for row in effects)
    assert CURRENT.get() is None


@pytest.mark.parametrize('kwargs', [{'max_selectors':1},{'max_rows':1},{'max_bytes':1}])
def test_bound_failure_writes_not_ready_and_publishes_no_entries(explorer,kwargs):
    root,_,receipt=prepare_fixture(explorer)
    with pytest.raises(ValueError):preview_startup.prepare_effect_preview_cache(root,'internal',receipt,**kwargs)
    assert json.loads(receipt.read_text())['ready'] is False
    assert not CACHE.items and CURRENT.get() is None


@pytest.mark.parametrize('seconds',[0,-1,601])
def test_startup_budget_cannot_disable_or_exceed_guarded_ceiling(explorer,seconds):
    root,_,receipt=prepare_fixture(explorer)
    with pytest.raises(ValueError):preview_startup.prepare_effect_preview_cache(root,'internal',receipt,seconds=seconds)
    assert not CACHE.items


def test_cancelled_preparation_is_not_ready_and_does_not_use_http_timeout(explorer,monkeypatch):
    root,_,receipt=prepare_fixture(explorer)
    cancelled=Budget(120);cancelled.cancel()
    monkeypatch.setattr(preview_startup,'Budget',lambda seconds:cancelled)
    with pytest.raises(QueryUnavailable):preview_startup.prepare_effect_preview_cache(root,'internal',receipt)
    report=json.loads(receipt.read_text())
    assert not report['ready'] and report['error']=='query_timeout' and not CACHE.items


def test_missing_source_is_not_a_zero_result_or_ready(explorer,monkeypatch):
    root,_,receipt=prepare_fixture(explorer)
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_CORE_DATABASE',str(root/'absent-source.sqlite'))
    with pytest.raises(QueryUnavailable):preview_startup.prepare_effect_preview_cache(root,'internal',receipt)
    report=json.loads(receipt.read_text());assert not report['ready'] and report['error']=='source_unavailable'
    assert not CACHE.items


def test_changed_source_during_preparation_cannot_publish_cache(explorer,monkeypatch):
    root,_,receipt=prepare_fixture(explorer)
    original=preview_startup._batch_contrast_effects
    def change(*args,**kwargs):
        result=original(*args,**kwargs)
        stat=explorer['core'].stat();os.utime(explorer['core'],ns=(stat.st_atime_ns,stat.st_mtime_ns+100000))
        return result
    monkeypatch.setattr(preview_startup,'_batch_contrast_effects',change)
    with pytest.raises(QueryUnavailable):preview_startup.prepare_effect_preview_cache(root,'internal',receipt)
    assert json.loads(receipt.read_text())['error']=='source_changed' and not CACHE.items


def test_source_change_after_startup_never_reuses_old_bound_entries(explorer,monkeypatch):
    root,contrasts,receipt=prepare_fixture(explorer)
    preview_startup.prepare_effect_preview_cache(root,'internal',receipt)
    before=fingerprint(explorer['core']);stat=explorer['core'].stat()
    os.utime(explorer['core'],ns=(stat.st_atime_ns,stat.st_mtime_ns+100000))
    reads=[];original=scientific.read_connection
    @contextmanager
    def trace(path):
        reads.append(str(path))
        with original(path) as db:yield db
    monkeypatch.setattr(scientific,'read_connection',trace)
    with scientific.read_connection(explorer['db']) as db:
        scientific._contrast_effects(explorer['core'],contrasts[0],projection=db)
    assert str(explorer['core']) in reads and fingerprint(explorer['core'])!=before
