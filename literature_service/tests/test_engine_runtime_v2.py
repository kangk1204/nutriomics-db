"""Synthetic version/isolation failures; these are not scientific gold labels."""
import json
from types import SimpleNamespace

import pytest

from nutri_literature.collection import save_article
from nutri_literature.db import connect, digest, dumps, initialize
from nutri_literature.dictionaries import load_dictionaries
from nutri_literature.engines import default_engine, engine_manifest_validation, engine_rules_version, switch_engine
from nutri_literature.extraction import extract
from nutri_literature.pipeline import process, process_engine, register_engine, store_extraction
from nutri_literature.settings import Settings


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config = tmp_path / 'topics.json'
    config.write_text(dumps({'topics': [{'id': 't', 'name': 'T', 'query': 'DHA', 'start_date': '2025-01-01'}]}),encoding='utf-8')
    setting = Settings(tmp_path / 'runtime', config)
    initialize(setting)
    monkeypatch.setattr('nutri_literature.pipeline.psutil.virtual_memory',lambda: SimpleNamespace(available=16*1024**3))
    monkeypatch.setattr('nutri_literature.operations_v2.register_artifact',lambda s,e: 'synthetic-model-A')
    return setting


def article(text='We enrolled 30 adults. DHA reduced blood pressure.', xml=b'<synthetic />'):
    return {'pmid':'synthetic-001','doi':None,'pmcid':None,'title':'Operational isolation fixture','pub_date':'2025',
            'abstract':text,'text':text,'sections':[],'publication_types':[],'mesh':['Humans'],'authors':[],
            'retracted':False,'corrections':[],'element_xml':xml}


def seed(setting,data=None):
    with connect(setting) as con:
        version=con.execute('SELECT query_version FROM topics').fetchone()[0]
    return save_article(setting,data or article(),'t',version)


def result_v2(setting,text):
    result = extract(text,[],{},load_dictionaries(setting))
    result.update(engine_version='heuristic-v2',rules_version=engine_rules_version('heuristic-v2'),
                  dictionary_version=load_dictionaries(setting,'heuristic-v2').version)
    result['manifest']={'synthetic_runtime_test':True,'parser':{'name':'en_core_sci_sm','version':'0.5.4',
        'spacy_version':'3.7.5','scispacy_version':'0.6.2','model_sha256':'a'*64,'available':True,'mode':'production'}}
    result.update(mentions=[],assertions=[],context={},summary={},events=[])
    return result


def test_per_engine_active_result_and_review_survive(runtime,monkeypatch):
    paper = seed(runtime)
    process(runtime,'extract')
    with connect(runtime) as con:
        v1=con.execute("SELECT extraction_id FROM extractions WHERE engine_version='heuristic-v1'").fetchone()[0]
        assertion=con.execute('SELECT * FROM assertions WHERE extraction_id=?',(v1,)).fetchone()
        con.execute("UPDATE assertions SET status='approved' WHERE assertion_id=?",(assertion['assertion_id'],))
    monkeypatch.setattr('nutri_literature.engines.extract_engine',lambda text,sections,dictionaries,metadata,engine:result_v2(runtime,text))
    register_engine(runtime,'heuristic-v2',queue_all=True)
    assert process_engine(runtime,limit=1)['processed']==1
    with connect(runtime) as con:
        assert con.execute('SELECT count(*) FROM extractions WHERE active=1 AND paper_id=?',(paper,)).fetchone()[0]==2
        assert con.execute('SELECT status FROM assertions WHERE assertion_id=?',(assertion['assertion_id'],)).fetchone()[0]=='approved'
    assert default_engine(runtime)=='heuristic-v1'


def test_artifact_change_creates_independent_durable_task(runtime,monkeypatch):
    seed(runtime)
    register_engine(runtime,'heuristic-v2')
    monkeypatch.setattr('nutri_literature.operations_v2.register_artifact',lambda s,e:'synthetic-model-B')
    register_engine(runtime,'heuristic-v2')
    with connect(runtime) as con:
        rows=con.execute('SELECT artifact_version,status FROM engine_queue ORDER BY task_id').fetchall()
    assert [tuple(r) for r in rows]==[('synthetic-model-A','superseded'),('synthetic-model-B','pending')]


def test_changed_source_cannot_complete_old_task(runtime,monkeypatch):
    seed(runtime)
    register_engine(runtime,'heuristic-v2')
    def changed(text,sections,dictionaries,metadata,engine):
        seed(runtime,article('DHA increased blood pressure.',b'<new-source />'))
        return result_v2(runtime,text)
    monkeypatch.setattr('nutri_literature.engines.extract_engine',changed)
    run=process_engine(runtime,limit=1)
    assert run['failed']==1 and run['processed']==0
    with connect(runtime) as con:
        assert con.execute('SELECT count(*) FROM extractions').fetchone()[0]==0
        assert con.execute("SELECT count(*) FROM engine_queue WHERE status='pending'").fetchone()[0]==2


def test_nested_field_wrong_offset_rejected(runtime):
    paper=seed(runtime)
    with connect(runtime) as con:
        source=dict(con.execute('SELECT * FROM sources').fetchone())
    result=result_v2(runtime,source['text_content'])
    result['events']=[{'quantity':{'text':'not the source','start':0,'end':3}}]
    with pytest.raises(ValueError,match='offsets'):
        store_extraction(runtime,paper,source,result)


def test_ablation_and_changed_model_not_production(runtime):
    result=result_v2(runtime,article()['text'])
    result['manifest']['ablation_stage']='syntax_without_context'
    assert 'development_ablation' in engine_manifest_validation(result,'heuristic-v2')
    del result['manifest']['ablation_stage']
    directory=runtime.home/'engine-artifacts'
    directory.mkdir()
    (directory/'heuristic-v2.json').write_text(dumps({'model_sha256':'b'*64}),encoding='utf-8')
    assert 'model_artifact_changed' in engine_manifest_validation(result,'heuristic-v2',runtime)


def test_unverified_default_promotion_is_blocked(runtime):
    with pytest.raises(ValueError,match='durably verified'):
        switch_engine(runtime,'heuristic-v2',{'passed':True})
    fake={'schema_version':'literature-engine-default-v1','engine':'heuristic-v2',
          'rules_version':engine_rules_version('heuristic-v2'),'promotion':{'passed':True},
          'promotion_sha256':digest(dumps({'passed':True}))}
    (runtime.home/'engine-default.json').write_text(json.dumps(fake),encoding='utf-8')
    assert default_engine(runtime)=='heuristic-v1'
    assert switch_engine(runtime,'heuristic-v1')['engine']=='heuristic-v1'
