"""A resumed public run must neither retry protected predictions nor hide gaps."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from nutri_literature.db import dumps, initialize
from nutri_literature.settings import Settings


def setup_run(tmp_path,monkeypatch):
    spec=importlib.util.spec_from_file_location('public_measurement_runner',Path(__file__).parents[1]/'scripts'/'run_public_v2.py')
    runner=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    settings=Settings(tmp_path/'runtime',tmp_path/'topics.json')
    settings.config.write_text(dumps({'topics':[]}),encoding='utf-8')
    initialize(settings)
    provenance={name:{'engine_version':name} for name in ['heuristic-v1','heuristic-v2']}
    state={'development_complete':True,'provenance':provenance,'steps':{}}
    sources=[('biored','abstract'),('bioredirect','abstract'),('ebmnlp','abstract'),
             ('bioscope','abstract'),('bioscope','fulltext'),('evidenceoutcomes','abstract')]
    for corpus,level in sources:
        identity=corpus+':'+level
        manifest=tmp_path/(identity.replace(':','-')+'.json')
        manifest.write_text(dumps({'source_sha256':'pinned'}),encoding='utf-8')
        prepared={'inputs_path':'unused','gold_path':'unused','manifest_path':str(manifest),'documents':1}
        state['steps']['prepare:development:'+identity]=prepared
        for engine in provenance:
            state['steps']['freeze:'+identity+':'+engine]={'freeze_path':'unused'}
        if corpus=='ebmnlp':
            continue
        state['steps']['prepare:test:'+identity]=prepared
        modes=['end_to_end','gold_entities'] if corpus in {'biored','bioredirect'} else ['end_to_end']
        for mode in modes:
            for engine in provenance:
                key='test:'+identity+':'+mode+':'+engine
                state['steps']['predict:'+key]={'prediction_run':'already_consumed','documents':1}
                state['steps']['evaluate:'+key]={'documents':1}
            state['steps']['compare:test:'+identity+':'+mode]={'documents':1}
    path=settings.home/'public_benchmarks'/'v2-measurement-index.json'
    path.parent.mkdir(parents=True)
    path.write_text(dumps(state),encoding='utf-8')
    diagnostic={'id':'fixture','source_sha256':'pinned','status':'native_test_layout_unsupported_in_frozen_protocol',
                'parsed_test_papers':0,'native_expert_test_papers':191}
    diagnosis=settings.home/'validation'/'ebm-test-diagnostic.json'
    diagnosis.parent.mkdir()
    diagnosis.write_text(dumps(diagnostic),encoding='utf-8')
    monkeypatch.setattr(runner.Settings,'from_env',lambda:settings)
    monkeypatch.setattr(runner,'_provenance',lambda _,engine:(None,provenance[engine]))
    monkeypatch.setattr(runner,'_receipt',lambda *a:None)
    monkeypatch.setattr(runner,'verify_receipt',lambda *a:True)
    monkeypatch.setattr(runner,'predict_public',lambda *a,**k:pytest.fail('Protected prediction retried'))
    monkeypatch.setattr(runner,'prepare_public',lambda *a,**k:pytest.fail('Unprepared source read'))
    monkeypatch.setattr(sys,'argv',['runner','--phase','test','--skip-unavailable-ebm-test'])
    return runner,path,diagnosis,diagnostic


def test_completed_available_runs_remain_partial_and_predictions_are_not_retried(tmp_path,monkeypatch):
    runner,path,_,_=setup_run(tmp_path,monkeypatch)
    runner.main()
    value=json.loads(path.read_bytes())
    assert value['test_execution_complete'] is True and value['test_complete'] is False
    assert len(value['test_unavailable'])==1 and value['test_unavailable'][0]['corpus']=='ebmnlp'
    assert value['steps']['predict:test:biored:abstract:end_to_end:heuristic-v2']['prediction_run']=='already_consumed'


@pytest.mark.parametrize('field,value',[('source_sha256','changed'),('parsed_test_papers',1),('native_expert_test_papers',0)])
def test_unavailability_requires_exact_unparsed_native_diagnostic(tmp_path,monkeypatch,field,value):
    runner,_,diagnosis,diagnostic=setup_run(tmp_path,monkeypatch)
    diagnosis.write_text(dumps({**diagnostic,field:value}),encoding='utf-8')
    with pytest.raises(ValueError,match='exact audited'):
        runner.main()


def test_unaudited_skip_is_rejected(tmp_path,monkeypatch):
    runner,_,_,_=setup_run(tmp_path,monkeypatch)
    monkeypatch.setattr(runner,'verify_receipt',lambda *a:False)
    with pytest.raises(ValueError,match='exact audited'):
        runner.main()
