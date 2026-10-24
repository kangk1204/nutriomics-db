import hashlib,json,sqlite3

import pytest
from fastapi import HTTPException

from test_pathway_selection import bundle, selection, PARENTS, SUFFIX
from nutrigenomics.pathway_selection import file_sha256
from nutrigenomics.pathway_correction_api import corrected_page


@pytest.fixture
def configured(bundle, monkeypatch):
    overlay, _ = selection(bundle)
    record={'id':2,'method':'synthetic method fixture'}
    digest=hashlib.sha256(json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    overlay['selectors'][0]['parent_record_sha256']=digest
    bundle['output'].write_text(json.dumps(overlay),encoding='utf-8')
    monkeypatch.setenv('NUTRIOMICS_CORRECTION_ACCESS','internal_review')
    monkeypatch.setenv('NUTRIOMICS_PATHWAY_SELECTION',str(bundle['output']))
    monkeypatch.setenv('NUTRIOMICS_PATHWAY_ARTIFACT_ROOT',str(bundle['parquet_root']))
    monkeypatch.setenv('NUTRIOMICS_PATHWAY_SELECTION_SHA256',file_sha256(bundle['output']))
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT)')
    db.execute('INSERT INTO science_record VALUES(?,?,?)',('c','contrast',json.dumps(dict(series_accession='GSE56960',contrast='contrast-0',analysis_version=PARENTS[2][0]))))
    db.execute('INSERT INTO science_record VALUES(?,?,?)',('2','analysis_run',json.dumps(record)))
    yield db,bundle
    db.close()


def call(db,**kwargs):
    return corrected_page(db,release='fixture-release',series='GSE56960',contrast=kwargs.pop('contrast','contrast-0'),analysis_version=kwargs.pop('analysis_version',PARENTS[2][0]),feature_id=kwargs.pop('feature_id',None),page=kwargs.pop('page',1),page_size=kwargs.pop('page_size',2),cursor=kwargs.pop('cursor',None),**kwargs)


def test_explicit_correction_displays_real_keys_nulls_and_pending(configured):
    db,_=configured
    response=call(db)
    assert response['total']==3 and response['status']=='available'
    assert response['method_correction']['selected']['analysis_version']==PARENTS[2][0]+SUFFIX
    assert response['method_correction']['expert_review_status']=='pending'
    assert response['method_correction']['public_allowed'] is False
    assert response['items'][0]['analysis_key'][-1]==PARENTS[2][0]+SUFFIX
    assert response['items'][0]['standard_error'] is None
    second=call(db,cursor=response['next_cursor'])
    assert second['items'][0]['p_value'] is None and second['next_cursor'] is None
    assert second['items'][0]['effect_estimate'] is None
    assert call(db,feature_id='absent')['total']==0  # Confirmed absence only.
    assert call(db,analysis_version=PARENTS[2][0]+SUFFIX)['total']==3


def test_cursor_is_bound_to_correction_and_query(configured):
    db,_=configured
    token=call(db)['next_cursor']
    with pytest.raises(HTTPException) as e:call(db,cursor=token,feature_id='R-HSA-0')
    assert e.value.status_code==409
    with pytest.raises(HTTPException) as e:call(db,cursor='bad!!')
    assert e.value.status_code==422


@pytest.mark.parametrize('mode',['public','publication','', 'unknown'])
def test_unapproved_public_or_unconfigured_access_denied(configured,monkeypatch,mode):
    db,_=configured;monkeypatch.setenv('NUTRIOMICS_CORRECTION_ACCESS',mode)
    with pytest.raises(HTTPException) as e:call(db)
    assert e.value.status_code==409


def test_changed_artifact_is_error_not_zero_or_original_fallback(configured):
    db,b=configured
    p=b['parquet_root']/'analysis-2/pathway_effects.parquet';p.write_bytes(p.read_bytes()+b'changed')
    with pytest.raises(HTTPException) as e:call(db)
    assert e.value.status_code==503


def test_changed_parent_record_requires_rebinding(configured):
    db,_=configured;db.execute("UPDATE science_record SET metadata_json='{}' WHERE kind='analysis_run'")
    with pytest.raises(HTTPException) as e:call(db)
    assert e.value.status_code==503


def test_unregistered_contrast_rejected_and_other_series_unaffected(configured):
    db,_=configured
    with pytest.raises(HTTPException) as e:call(db,contrast='contrast-1')
    assert e.value.status_code==404
    assert corrected_page(db,release='fixture-release',series='GSE00000',contrast='x',analysis_version='v',feature_id=None,page=1,page_size=25,cursor=None) is None
