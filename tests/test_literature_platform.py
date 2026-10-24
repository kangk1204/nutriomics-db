import json
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from nutrigenomics.literature_snapshot import build_snapshot, sha, redistribution_allowed
from nutrigenomics.literature_api import attach_literature


@pytest.fixture
def source(tmp_path):
    p = tmp_path / "source.sqlite3";c = sqlite3.connect(p)
    c.executescript('''
    CREATE TABLE papers(paper_id INTEGER PRIMARY KEY,pmid TEXT,title TEXT,pub_date TEXT,doi TEXT,pmcid TEXT,
    oa_status TEXT,retracted INTEGER,abstract TEXT,organism TEXT,license TEXT,publication_types TEXT,
    current_source_id TEXT,updated_at TEXT);
    CREATE TABLE sources(source_id TEXT,paper_id INTEGER,text_content TEXT,text_hash TEXT,license TEXT,kind TEXT,retrieved_at TEXT);
    CREATE TABLE topics(topic_id TEXT,start_date TEXT,query_version TEXT,enabled INTEGER);
    CREATE TABLE topic_state(topic_id TEXT,query_version TEXT,watermark TEXT,updated_at TEXT);
    CREATE TABLE paper_topics(paper_id INTEGER,topic_id TEXT);
    CREATE TABLE extractions(extraction_id TEXT,paper_id INTEGER,source_id TEXT,engine_version TEXT,
    dictionary_version TEXT,result_json TEXT,active INTEGER,created_at TEXT);
    CREATE TABLE assertions(assertion_id TEXT,extraction_id TEXT,fingerprint TEXT,predicate TEXT,subject TEXT,object TEXT,
    status TEXT,quote TEXT,start INTEGER,end INTEGER,payload TEXT,rule_id TEXT);
    CREATE TABLE reviews(review_id TEXT,assertion_id TEXT,decision TEXT,corrected_payload TEXT,fingerprint TEXT,created_at TEXT);CREATE TABLE reports(report_id TEXT);
    CREATE TABLE queue(stage TEXT,status TEXT);
    CREATE TABLE accounts(username TEXT,password_hash TEXT);
    INSERT INTO topics VALUES('HTN','2010-01-01','q1',1);
    INSERT INTO topic_state VALUES('HTN','q1','2026-10-23','2026-10-23');
    INSERT INTO accounts VALUES('private-user','do-not-export-this-secret');
    ''')
    text = 'β 😀 Vitamin D reduced ACE expression.'
    for i, license_value in [(1, None), (2, 'CC BY 4.0')]:
        c.execute('INSERT INTO papers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (i,str(i),'Vitamin D study '+str(i),'2015-01-01',None,None,'available',0,text,'human',license_value,'[]','s'+str(i),'2026-10-23'))
        c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?)',('s'+str(i),i,text,sha(text),license_value,'fulltext','2026-10-23'))
        c.execute('INSERT INTO paper_topics VALUES(?,?)',(i,'HTN'))
        for engine in ['heuristic-v1','heuristic-v5']:
            eid=str(i)+engine
            c.execute('INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?)',(eid,i,'s'+str(i),engine,'fixed-dict',json.dumps({'summary':{'results':[text]}}),1,'2026-10-23'))
            c.execute('INSERT INTO assertions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (eid,eid,eid,'decreases','D1','G1','candidate',text,0,len(text),json.dumps({'model_path':'/home/private/model','text':text}),'rule-1'))
    c.commit();c.close();return p


def test_parent_documents_are_version_links_and_do_not_inflate_collection(source,tmp_path):
    parent=tmp_path/'parent.sqlite3';c=sqlite3.connect(parent)
    c.execute('CREATE TABLE candidate_document(pmid TEXT,source_sha256 TEXT,extraction_version TEXT)')
    c.executemany('INSERT INTO candidate_document VALUES(?,?,?)',[('1','old','v1'),('not-collected','old2','v1')]);c.commit();c.close();before=parent.read_bytes()
    result=build_snapshot(source,tmp_path/'linked',release_id='linked',parent_database=parent)
    assert parent.read_bytes()==before and result['counts']['unique_papers']==2
    assert result['counts']['parent_documents_matched_by_PMID']==1 and result['counts']['parent_documents_missing']==1
    with sqlite3.connect(tmp_path/'linked/literature.sqlite3') as c:assert c.execute('SELECT count(*) FROM parent_document_links').fetchone()[0]==2


def test_correction_is_versioned_and_applied_without_rewriting_the_quote(source,tmp_path):
    eid='2heuristic-v1'
    with sqlite3.connect(source) as c:
        c.execute("UPDATE assertions SET status='approved' WHERE assertion_id=?",(eid,))
        c.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?)',('review1',eid,'corrected',json.dumps({'subject':'D-corrected','predicate':'decreases','object':'G-corrected'}),eid,'2026-10-23'))
    build_snapshot(source,tmp_path/'reviewed',release_id='reviewed')
    item=app_for(tmp_path/'reviewed').get('/api/v1/explorer/literature/events?pmid=2').json()['items'][0]
    assert item['subject']=='D-corrected' and item['payload']['expert_approved']
    assert item['payload']['original_relation']['subject']=='D1' and item['quote'].startswith('β 😀')
    with sqlite3.connect(source) as c:c.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?)',('review2',eid,'approved',None,'wrong-fingerprint','2026-10-24'))
    build_snapshot(source,tmp_path/'stale',release_id='stale')
    stale=app_for(tmp_path/'stale').get('/api/v1/explorer/literature/events?pmid=2').json()['items'][0]
    assert not stale['payload']['expert_approved'] and stale['status']=='needs_review'
    assert item['event_id']!=stale['event_id']


def app_for(path, public=True):
    a=FastAPI();attach_literature(a,public=public,database_path=path);return TestClient(a)


def test_public_and_internal_builds_share_one_pinned_transaction(source,tmp_path):
    with sqlite3.connect(source) as writer:writer.execute('PRAGMA journal_mode=WAL')
    pinned=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True);pinned.execute('BEGIN');pinned.execute('SELECT count(*) FROM papers').fetchone()
    try:
        a=build_snapshot(source,tmp_path/'paired-public',release_id='paired',_connection=pinned)
        with sqlite3.connect(source) as writer:writer.execute("UPDATE papers SET title='new title',updated_at='2027-01-01' WHERE pmid='1'")
        b=build_snapshot(source,tmp_path/'paired-internal',release_id='paired',public=False,_connection=pinned)
        assert a['source_transaction_sha256']==b['source_transaction_sha256']
        assert app_for(tmp_path/'paired-internal',False).get('/api/v1/explorer/literature/papers/1').json()['title']!='new title'
    finally:pinned.close()
    c=build_snapshot(source,tmp_path/'later',release_id='later')
    assert c['source_transaction_sha256']!=a['source_transaction_sha256']


def test_public_snapshot_withholds_unconfirmed_text_and_never_exports_accounts(source,tmp_path):
    output=tmp_path/'public';before=source.read_bytes()
    m=build_snapshot(source,output,release_id='pub',public=True)
    assert source.read_bytes()==before and m['counts']['unique_papers']==2
    assert m['counts']['withheld_events']==2 and m['counts']['visible_events']==2
    c=app_for(output)
    hidden=c.get('/api/v1/explorer/literature/papers/1').json()
    assert hidden['abstract'] is None and hidden['source_text'] is None and hidden['summary'] is None
    visible=c.get('/api/v1/explorer/literature/papers/2').json()
    assert visible['source_text'].startswith('β 😀')
    body=c.get('/api/v1/explorer/literature/events').text
    assert '/home/private' not in body and 'do-not-export' not in body and 'private-user' not in body
    assert c.post('/api/v1/literature-admin/jobs',json={'kind':'collect'}).status_code==404


def test_internal_snapshot_retains_quote_and_offsets(source,tmp_path):
    output=tmp_path/'internal';build_snapshot(source,output,release_id='int',public=False)
    c=app_for(output,False);row=c.get('/api/v1/explorer/literature/events?pmid=1').json()['items'][0]
    text=c.get('/api/v1/explorer/literature/papers/1').json()['source_text']
    assert text[row['start']:row['end']]==row['quote'] and row['source_sha256']==sha(text)


def test_public_app_refuses_internal_snapshot_and_corrupt_release(source,tmp_path):
    output=tmp_path/'internal';build_snapshot(source,output,release_id='int',public=False)
    with pytest.raises(ValueError,match='interchanged'):app_for(output)
    (output/'literature.sqlite3').write_bytes(b'broken')
    with pytest.raises(ValueError,match='hash mismatch'):app_for(output,False)


def test_snapshot_preserves_engine_versions_and_queries_are_bounded(source,tmp_path):
    output=tmp_path/'public';build_snapshot(source,output,release_id='pub')
    c=app_for(output)
    rows=c.get('/api/v1/explorer/literature/events?engine=heuristic-v5').json()
    assert rows['total']==1 and rows['items'][0]['engine']=='heuristic-v5'
    assert c.get('/api/v1/explorer/literature/papers?q=Vitamin&topic=HTN&year_from=2010').json()['total']==2
    assert c.get('/api/v1/explorer/literature/papers?page_size=201').status_code==422
    assert c.get('/api/v1/explorer/literature/papers?year_from=2025&year_to=2010').status_code==422
    assert c.get('/api/v1/explorer/literature/papers/999').status_code==404
    assert c.get('/api/v1/explorer/literature/papers?q=%22%20OR%201%3D1').status_code==200


def test_source_position_or_source_hash_mismatch_fails_build(source,tmp_path):
    c=sqlite3.connect(source);c.execute('UPDATE assertions SET quote=?',('wrong quote',));c.commit();c.close()
    with pytest.raises(ValueError,match='quote does not match'):build_snapshot(source,tmp_path/'bad',release_id='bad')


def test_snapshot_is_immutable_and_reports_use_snapshot_counts(source,tmp_path):
    output=tmp_path/'public';build_snapshot(source,output,release_id='pub')
    with pytest.raises(FileExistsError):build_snapshot(source,output,release_id='pub')
    c=app_for(output)
    for fmt in ['html','md','csv','json']:
        response=c.get('/api/v1/explorer/literature/reports/current?format='+fmt)
        assert response.status_code==200 and '/home/' not in response.text
    assert c.get('/api/v1/explorer/literature/reports/current?format=json').json()['counts']['unique_papers']==2


@pytest.mark.parametrize('license_value,expected',[('CC BY 4.0',True),('CC0',True),('https://creativecommons.org/licenses/by/4.0/',True),('CC BY-NC 4.0',False),('',False),('publisher copyright',False)])
def test_only_explicit_redistributable_license_is_admitted(license_value,expected):
    assert redistribution_allowed(license_value)==expected


def test_expansion_counts_are_db_intersection_not_search_or_manifest_claim(source,tmp_path):
    expansion={'corpus_id':'v8-all','query_version':'v8-q1','expected_papers':133253,
               'pmids':['1','2','999'],'status':'completed','stored_papers':133253,
               'raw_path':'/home/private/secret','observed_at':'2026-10-23T13:00:00+09:00'}
    output=tmp_path/'v8';m=build_snapshot(source,output,release_id='v8',expansion_manifest=expansion)
    assert m['counts']['unique_papers']==2
    assert m['expansion']['expected_papers']==133253 and m['expansion']['enumerated_papers']==3
    assert m['expansion']['stored_papers']==2 and m['expansion']['missing_papers']==133251
    assert not m['expansion']['collection_complete'] and m['expansion']['status']=='waiting'
    c=app_for(output);stats=c.get('/api/v1/explorer/literature/stats').json()
    assert stats['engine_availability']['nutriomics-evidence-v8']=={'status':'unavailable','evaluation_status':'not_completed'}
    assert c.get('/api/v1/explorer/literature/papers?corpus=v8-all&query_version=v8-q1').json()['total']==2
    assert c.get('/api/v1/explorer/literature/papers?corpus=v8-all&query_version=wrong').json()['total']==0
    # Rights remain applied after the new corpus filter; collection counts retain bibliography.
    rows=c.get('/api/v1/explorer/literature/papers?corpus=v8-all').json()['items']
    assert len(rows)==2 and next(r for r in rows if r['pmid']=='1')['summary'] is None
    assert c.get('/api/v1/explorer/literature/events?corpus=v8-all').json()['total']==1
    assert c.get('/api/v1/explorer/literature/papers?corpus=%27%20OR%201%3D1').json()['total']==0
    for fmt in ['json','md','html','csv']:
        body=c.get('/api/v1/explorer/literature/reports/current?format='+fmt+'&table=inventory').text
        assert '/home/private' not in body and '133253' in body.replace(',','')
    assert c.post('/api/v1/literature-admin/jobs',json={'kind':'collect'}).status_code==404


def test_v8_status_filters_use_current_extractions_and_real_queue_not_training_claims(source,tmp_path):
    with sqlite3.connect(source) as c:
        c.execute('CREATE TABLE engine_queue(task_id INTEGER,paper_id INTEGER,engine_version TEXT,source_hash TEXT,status TEXT)')
        text=c.execute("SELECT text_content FROM sources WHERE source_id='s1'").fetchone()[0]
        c.executemany('INSERT INTO engine_queue VALUES(?,?,?,?,?)',[
            (1,1,'nutriomics-evidence-v8',sha(text),'running'),
            (2,2,'nutriomics-evidence-v8','old-source-hash','completed')])
    out=tmp_path/'states';build_snapshot(source,out,release_id='states')
    c=app_for(out);url='/api/v1/explorer/literature/papers?engine=nutriomics-evidence-v8&processing_status='
    assert c.get(url+'running').json()['total']==1
    assert c.get(url+'not_processed').json()['total']==1
    assert c.get(url+'completed').json()['total']==0
    assert c.get(url+'faked').status_code==422
    assert c.get('/api/v1/explorer/literature/papers?processing_status=completed').json()['total']==2
    stat=c.get('/api/v1/explorer/literature/stats').json()['processing_by_engine']['nutriomics-evidence-v8']
    assert stat['running']==1 and stat['not_processed']==1 and sum(stat.values())==2
    assert c.get('/api/v1/explorer/literature/papers/1?engine=nutriomics-evidence-v8').json()['processing_status']=='running'


def test_existing_pmids_do_not_prove_new_metadata_or_membership_complete(source,tmp_path):
    expansion={'corpus_id':'v8','query_version':'q8','pmids':['1','2'],'expected_papers':2,'status':'completed'}
    old=build_snapshot(source,tmp_path/'old-pmids',release_id='old',expansion_manifest=expansion)['expansion']
    assert old['enumeration_complete'] and old['stored_papers']==2
    assert not old['metadata_complete'] and not old['collection_complete'] and old['status']=='waiting'
    expansion.update(metadata_complete=True,missing_memberships=1)
    missing=build_snapshot(source,tmp_path/'missing-memberships',release_id='missing',expansion_manifest=expansion)['expansion']
    assert not missing['collection_complete']
    expansion['missing_memberships']=0
    ready=build_snapshot(source,tmp_path/'metadata-ready',release_id='ready',expansion_manifest=expansion)['expansion']
    assert ready['collection_complete'] and ready['status']=='completed'


def test_explicit_audits_are_aggregate_allowlisted_and_frozen(source,tmp_path):
    audit=tmp_path/'audit.json';audit.write_text(json.dumps({'status':'running',
        'stages':{'entity':{'total':20,'retained':15,'lost':5,'raw_vectors':[1,2],'path':'/home/private'}},
        'id_changes':{'gene':{'kept':5,'corrected':0,'damaged':0}},
        'label_provenance':{'training':{'public_gold':384,'weak_labels':1000,'unlabeled':5000}},
        'password_hash':'private'}))
    out=tmp_path/'audit-snapshot';build_snapshot(source,out,release_id='audit',diagnostics_manifest=audit)
    audit.write_text('{"status":"completed"}')
    response=app_for(out).get('/api/v1/explorer/literature/stats')
    value=response.json()['diagnostics']
    assert value['status']=='running' and value['stages']['entity']=={'total':20,'retained':15,'lost':5}
    assert value['id_changes']['gene']['corrected']==0
    assert value['label_provenance']['training']['public_gold']==384
    assert all(x not in response.text for x in ('raw_vectors','/home/private','password_hash'))


@pytest.mark.parametrize('patch',[{'pmids':['1','1']},{'pmids':['/home/private']},{'expected_papers':1},
                                  {'corpus_id':'/home/private'},{'query_version':'../../secret'}])
def test_invalid_expansion_manifest_fails_closed(source,tmp_path,patch):
    data={'corpus_id':'v8-all','query_version':'v8-q1','expected_papers':3,'pmids':['1','2'],**patch}
    with pytest.raises(ValueError):build_snapshot(source,tmp_path/'bad-expansion',release_id='bad',expansion_manifest=data)


def test_v8_evaluation_requires_same_gold_hash_and_counts(source,tmp_path):
    baseline=tmp_path/'baseline.json';baseline.write_text(json.dumps({'arms':{
        'v5-r1':{'metrics':{'tp':65,'fp':63,'fn':89,'precision':65/128,'recall':65/154,'f1':130/282},'sha256':'old'}},
        'gold_sha256':'gold','targets':{}}))
    evaluation=tmp_path/'evaluation.json';data={'papers':100,'gold_relations':154,'gold_sha256':'wrong',
        'scores':{'nutriomics-evidence-v8':{'metrics':{'tp':66,'fp':10,'fn':88,'precision':66/76,'recall':66/154,'f1':132/230}}}}
    evaluation.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='cohort differs'):
        build_snapshot(source,tmp_path/'wrong-eval',release_id='wrong',baseline_reference=baseline,v8_evaluation_path=evaluation)
    data['gold_sha256']='gold';evaluation.write_text(json.dumps(data))
    out=tmp_path/'valid-eval';build_snapshot(source,out,release_id='valid',baseline_reference=baseline,v8_evaluation_path=evaluation)
    api=app_for(out);benchmark=api.get('/api/v1/explorer/literature/benchmarks').json()
    assert len(benchmark['items'])==2
    assert benchmark['engine_availability']['nutriomics-evidence-v8']['evaluation_status']=='completed_development_only'
    assert not benchmark['independent_final_performance']
