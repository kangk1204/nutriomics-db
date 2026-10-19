"""Complete API equality and bounded source reads without scientific rewriting."""
import json
import os
import sqlite3
from contextlib import contextmanager

import pytest
from test_explorer_api import RELEASE
from test_explorer_api import explorer as explorer  # noqa: PLC0414 -- shared pytest fixture
from test_explorer_scientific_api import _science

from nutrigenomics import explorer_scientific as scientific
from nutrigenomics.effect_batch import read_rows_by_ids
from nutrigenomics.explorer_performance import (
    CACHE,
    QueryUnavailable,
    canonical,
    fingerprint,
    selector_key,
)


def setup_batch(fixture):
    _science(fixture)
    with sqlite3.connect(fixture['core']) as db:
        for contrast in ('second', 'third'):
            for feature, value, q_value, allowed, kind in (
                ('ENTPD1', None, None, False, 'gene_expression'),
                ('OTHER', 0.0, .8, True, 'gene_expression'),
                ('ENTPD1', .7, .0001, True, 'cpg'),
                ('Z', -.25, .9, True, 'pathway')):
                db.execute('INSERT INTO study_effect SELECT NULL,series_accession,?, ?,feature_label,?,tissue,timepoint,intervention,dose,duration,?,standard_error,p_value,?,effect_unit,status,n_people,n_samples,analysis_version,? FROM study_effect WHERE id=1',
                           (kind, feature, contrast, value, q_value, canonical({'export_allowed': allowed})))
    with sqlite3.connect(fixture['db']) as db:
        original = json.loads(db.execute("SELECT metadata_json FROM science_record WHERE kind='contrast'").fetchone()[0])
        for name in ('second', 'third', 'zero'):
            item = {**original, 'id': name, 'contrast': name}
            db.execute('INSERT INTO science_record VALUES(?,?,?)', (name, 'contrast', canonical(item)))
        db.executescript('CREATE TABLE perf_effect_selector(selector TEXT PRIMARY KEY,total INTEGER);'
                         'CREATE TABLE perf_effect_ref(selector TEXT,effect_id INTEGER,feature_type TEXT,feature_id TEXT,ordinal_all INTEGER);'
                         'CREATE UNIQUE INDEX perf_effect_all ON perf_effect_ref(selector,ordinal_all);'
                         'CREATE INDEX perf_effect_feature ON perf_effect_ref(selector,feature_type,feature_id);')
        records = [json.loads(row[0]) for row in db.execute("SELECT metadata_json FROM science_record WHERE kind='contrast' ORDER BY id")]
        with sqlite3.connect(fixture['core']) as core:
            for item in records:
                rows = list(core.execute('SELECT id,feature_type,feature_id FROM study_effect WHERE series_accession=? AND contrast=? AND analysis_version=? ORDER BY feature_type,feature_id',
                                        [item[key] for key in ('series_accession','contrast','analysis_version')]))
                selector = selector_key(item)
                db.execute('INSERT INTO perf_effect_selector VALUES(?,?)', (selector, len(rows)))
                db.executemany('INSERT INTO perf_effect_ref VALUES(?,?,?,?,?)', [(selector, *row, i) for i, row in enumerate(rows)])
        db.execute('INSERT INTO meta VALUES(?,?)', ('performance', canonical({'indexed_for_release':RELEASE,
                   'core_fingerprint':json.loads(canonical(fingerprint(fixture['core'])))})))
    return records


@pytest.mark.parametrize('gene', [False, True])
@pytest.mark.parametrize('context', [{}, {'tissue':'PBMC','response_time_h':2,'population':'unmatched population'}])
def test_entire_match_json_is_identical_while_core_connections_and_pk_reads_decrease(explorer, monkeypatch, gene, context):
    setup_batch(explorer)
    payload = {'seed_id':'food:rice','intake_g':150,'reference_context':context}
    if gene:payload['gene_node_id']='NCBIGene:41'
    original_batch, original_read = scientific._batch_contrast_effects, scientific.read_connection
    statements, connections = [], []
    @contextmanager
    def trace(path):
        connections.append(str(path))
        with original_read(path) as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(scientific, 'read_connection', trace)
    monkeypatch.setattr(scientific, '_batch_contrast_effects', lambda *args, **kwargs: None)
    CACHE.clear()
    baseline=explorer['client'].post('/api/v1/explorer/match',json=payload)
    assert baseline.status_code==200
    baseline_connections=len(connections)
    baseline_pk=sum('SELECT * FROM study_effect WHERE id IN (' in sql for sql in statements)
    connections.clear();statements.clear();CACHE.clear()
    monkeypatch.setattr(scientific, '_batch_contrast_effects', original_batch)
    current=explorer['client'].post('/api/v1/explorer/match',json=payload)
    assert current.status_code==200 and current.json()==baseline.json()
    assert len(connections)==1 < baseline_connections
    assert sum('SELECT * FROM study_effect WHERE id IN (' in sql for sql in statements)==1 < baseline_pk
    contrasts=current.json()['observed_evidence'][0]['contrasts']
    assert next(row for row in contrasts if row['id']=='zero')['effect_total']==0
    nonzero=next(row for row in contrasts if row['id']=='second')
    assert any(row['effect_estimate'] is None and row['q_value'] is None and row['export_allowed'] is False for row in nonzero['effects'])
    if gene:assert all(row['feature_type']=='gene_expression' for row in nonzero['effects'])
    else:assert any(row['effect_estimate']==0 for row in nonzero['effects'])


def projection(fixture):
    db=sqlite3.connect(fixture['db']);db.row_factory=sqlite3.Row
    return db


def test_mismatched_native_reference_returns_unknown_count_not_zero_and_is_not_cached(explorer):
    contrasts=setup_batch(explorer)
    with projection(explorer) as db:
        db.execute("UPDATE perf_effect_ref SET feature_id='WRONG' WHERE ordinal_all=0")
    CACHE.clear()
    with projection(explorer) as db:
        result=scientific._batch_contrast_effects(explorer['core'],contrasts,None,db)
        for contrast in contrasts:
            rows,total,status=scientific._contrast_effects(explorer['core'],contrast,projection=db,batch_results=result)
            if contrast['contrast']=='zero':assert (rows,total,status)==([],0,'available')
            else:assert rows==[] and total is None and status=='analysis_key_mismatch'


def test_mid_request_source_change_invalidates_whole_batch_without_falling_back(explorer, monkeypatch):
    contrasts=setup_batch(explorer);CACHE.clear()
    original=scientific.read_rows_by_ids
    def mutate(core, ids):
        rows=original(core,ids)
        stat=explorer['core'].stat();os.utime(explorer['core'],ns=(stat.st_atime_ns,stat.st_mtime_ns+100000))
        return rows
    monkeypatch.setattr(scientific,'read_rows_by_ids',mutate)
    with projection(explorer) as db:
        results=scientific._batch_contrast_effects(explorer['core'],contrasts,None,db)
        for contrast in contrasts:
            assert scientific._contrast_effects(explorer['core'],contrast,projection=db,batch_results=results)==([],None,'source_changed')
    assert not CACHE.items


def test_source_error_keeps_null_count_and_failure_out_of_success_cache(explorer, monkeypatch):
    contrasts=setup_batch(explorer);CACHE.clear()
    @contextmanager
    def fail(path):
        raise QueryUnavailable('query_timeout')
        yield
    monkeypatch.setattr(scientific,'read_connection',fail)
    with projection(explorer) as db:
        results=scientific._batch_contrast_effects(explorer['core'],contrasts,None,db)
        for contrast in contrasts:
            assert scientific._contrast_effects(explorer['core'],contrast,projection=db,batch_results=results)==([],None,'query_timeout')
    assert not CACHE.items


def test_sorted_pk_loader_retains_native_rows_and_deduplicates_ids():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE study_effect(id INTEGER PRIMARY KEY,effect REAL,metadata_json TEXT)')
    db.executemany('INSERT INTO study_effect VALUES(?,?,?)', [(1,None,'{}'),(2,0.0,'{"export_allowed":false}'),(3,-.2,'{}')])
    sql=[];db.set_trace_callback(sql.append)
    rows=read_rows_by_ids(db,[3,1,3,2])
    assert list(rows)==[1,2,3] and rows[1]['effect'] is None and rows[2]['effect']==0 and rows[3]['effect']==-.2
    assert len(sql)==1 and 'IN (1,2,3)' in sql[0]
    with pytest.raises(ValueError):read_rows_by_ids(db,[1],401)
    db.close()
