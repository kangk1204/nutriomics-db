import sqlite3
import hashlib
import json
import pytest

from scripts.check_evidence_v8_progress import current_extraction_counts, frozen_search_counts


def test_intermediate_fits_and_latest_checkpoint_are_separate_from_fold_completion(tmp_path):
    import os
    from scripts.check_evidence_v8_progress import training_artifact_progress
    files={'fits/id/linear/model.json':{},'fits/id/neural/complete.json':{'task':'id','config':{'smoke_only':False}},
           'fits/id/smoke/complete.json':{'task':'id','config':{'smoke_only':True}},
           'fits/id/smoke/progress.json':{'history':[{'epoch':1,'optimizer_updated':True}]},
           'ner/models/upstream/progress.json':{'history':[{'epoch':3,'optimizer_updated':True}]},
           'fits/id/neural/progress.json':{'history':[{'epoch':1,'optimizer_updated':True}]}}
    for name,value in files.items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
        os.utime(path,(100,100))
    os.utime(tmp_path/'fits/id/neural/progress.json',(200,200))
    os.utime(tmp_path/'fits/id/smoke/progress.json',(300,300))
    before={name:(tmp_path/name).read_bytes() for name in files}
    result=training_artifact_progress(tmp_path)
    assert result['completed_id_linear_fits']==1 and result['completed_id_neural_fits']==1
    assert result['completed_relation_neural_fits']==0
    assert result['smoke_neural_fits_excluded']==1
    assert result['latest_training_checkpoint']['task']=='id'
    assert result['latest_training_checkpoint']['epoch']==1
    assert not any(k in result for k in ['metrics','precision','completed_id_outer_folds'])
    assert before=={name:(tmp_path/name).read_bytes() for name in files}


def test_invalid_checkpoint_progress_cannot_be_reported_as_training_success(tmp_path):
    from scripts.check_evidence_v8_progress import training_artifact_progress
    path=tmp_path/'fits/id/neural/progress.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'history':[{'epoch':0,'optimizer_updated':True}]}))
    with pytest.raises(ValueError,match='cannot be verified'):training_artifact_progress(tmp_path)


def test_slow_extraction_read_is_cancelled_without_inventing_zero_or_leaving_a_handler():
    from scripts.check_evidence_v8_progress import bounded_extraction_counts
    con=sqlite3.connect(':memory:')
    con.executescript('''
        CREATE TABLE papers(paper_id INTEGER,current_source_id TEXT,abstract TEXT);
        CREATE TABLE sources(source_id TEXT,text_hash TEXT);
        CREATE TABLE extractions(paper_id INTEGER,engine_version TEXT,source_id TEXT,source_hash TEXT,active INTEGER);
        CREATE TABLE queue(stage TEXT,status TEXT);
        WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000)
        INSERT INTO papers SELECT x,'s'||x,'abstract' FROM n;
        INSERT INTO sources SELECT current_source_id,'h' FROM papers;
        INSERT INTO extractions SELECT paper_id,'v1',current_source_id,'h',1 FROM papers;
    ''')
    result=bounded_extraction_counts(con,seconds=0)
    assert result['extraction_read_status']=='deadline_exceeded'
    assert 'extraction_success_papers' not in result
    assert con.execute('SELECT count(*) FROM papers').fetchone()[0]==1000
    assert bounded_extraction_counts(con)['extraction_success_papers']==1000


def test_spare_gpu_observer_has_no_system_python_dependency_or_writes(monkeypatch):
    from scripts import check_evidence_v8_progress as observer
    calls = []
    def fake_remote(host, code, **kwargs):
        calls.append((host, code))
        return b'{"host":"100.68.188.102","pipeline":{"status":"running"}}'
    monkeypatch.setattr(observer, 'remote', fake_remote)
    result = observer.spare_gpu_progress()
    assert result['host'] == '100.68.188.102'
    assert calls[0][0] == '100.68.188.102'
    assert 'psutil' not in calls[0][1]
    assert 'write_text' not in calls[0][1]


def test_current_source_unique_papers_not_attempts_or_old_extractions():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE papers(paper_id INTEGER, current_source_id TEXT, abstract TEXT);
        CREATE TABLE sources(source_id TEXT,text_hash TEXT);
        CREATE TABLE extractions(paper_id INTEGER,engine_version TEXT,source_id TEXT,source_hash TEXT,active INTEGER);
        CREATE TABLE queue(stage TEXT,status TEXT);
        INSERT INTO papers VALUES(1,'new','has abstract'),(2,'current',''),(3,'third','  ');
        INSERT INTO sources VALUES('new','hash'),('current','second'),('third','third');
        INSERT INTO extractions VALUES
          (1,'v1','new','hash',1),(1,'v1','new','hash',1),(1,'v2','new','hash',1),
          (2,'v1','old','second',1),(2,'v1','current','wrong hash',1),
          (3,'v1','third','third',0);
        INSERT INTO queue VALUES('extract','failed'),('extract','failed'),('oa','pending');
    """)
    counts = current_extraction_counts(con)
    assert counts['extraction_success_papers'] == 1
    assert counts['live_current_extraction_papers_by_engine'] == {'v1': 1, 'v2': 1}
    assert counts['live_abstract_papers'] == 1
    assert counts['live_queue_counts'] == [
        {'stage': 'extract', 'status': 'failed', 'queue_items': 2},
        {'stage': 'oa', 'status': 'pending', 'queue_items': 1}]


def test_frozen_inventory_survives_live_state_restart_and_preserves_membership(tmp_path):
    con = sqlite3.connect(':memory:')
    con.executescript("CREATE TABLE papers(pmid TEXT);INSERT INTO papers VALUES('1'),('2'),('unrelated');")
    ids = ['1', '2', '3']
    value = {'pmids': ids, 'expected_papers': 3, 'query_version': 'version', 'status': 'search_complete',
             'pmids_sha256': hashlib.sha256(json.dumps(ids, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    path = tmp_path/'search-manifest.json'
    assert frozen_search_counts(con, path) is None
    path.write_text(json.dumps(value), encoding='utf8')
    counts = frozen_search_counts(con, path)
    assert counts['expected_papers'] == counts['selected_unique'] == 3
    assert counts['stored_selected'] == 2 and counts['missing_count'] == 1
    assert counts['manifest_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    for changed in ({**value, 'pmids':['1','1','3']}, {**value, 'pmids_sha256':'wrong'},
                    {**value, 'expected_papers':4}, {**value,'pmids':['1','2','unknown']}):
        path.write_text(json.dumps(changed), encoding='utf8')
        with pytest.raises(ValueError, match='Frozen search'):
            frozen_search_counts(con, path)
