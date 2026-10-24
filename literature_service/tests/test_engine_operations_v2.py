"""Clock-controlled operational fixtures, never reported as actual server uptime."""
from datetime import UTC, datetime, timedelta

import pytest

from nutri_literature.db import connect, digest, dumps, initialize
from nutri_literature.operations_v2 import engine_status, provenance, soak_receipt
from nutri_literature.settings import Settings


@pytest.mark.parametrize('engine',['heuristic-v1','heuristic-v2'])
@pytest.mark.parametrize('official',[False,True])
def test_health_dictionary_identity_matches_extraction_without_building_trie(workspace,monkeypatch,engine,official):
    from nutri_literature.dictionaries import load_dictionaries
    if official:
        raw=b'hgnc_id\tsymbol\tname\tstatus\nHGNC:12679\tVDR\tvitamin D receptor\tApproved\n'
        (workspace.dictionaries/'fixture.tsv').write_bytes(raw)
        (workspace.dictionaries/'hgnc-active.json').write_text(dumps({
            'path':'fixture.tsv','sha256':digest(raw),'version':'hgnc-sha256:'+digest(raw),
        }),encoding='utf-8')
    expected=load_dictionaries(workspace,engine).version
    monkeypatch.setattr('nutri_literature.dictionaries.load_dictionaries',lambda *a,**k:pytest.fail('Trie built by health read'))
    assert provenance(workspace,engine)['dictionary_version']==expected


def test_health_rejects_changed_dictionary_bytes(workspace):
    (workspace.dictionaries/'fixture.tsv').write_bytes(b'changed')
    (workspace.dictionaries/'hgnc-active.json').write_text(dumps({
        'path':'fixture.tsv','sha256':digest(b'original'),'version':'pinned',
    }),encoding='utf-8')
    with pytest.raises(ValueError,match='hash mismatch'):
        provenance(workspace,'heuristic-v2')


@pytest.fixture
def workspace(tmp_path):
    config=tmp_path/'topics.json'
    config.write_text(dumps({'topics':[]}),encoding='utf-8')
    settings=Settings(tmp_path/'runtime',config)
    initialize(settings)
    return settings


def sample(settings,at,ok=True,engine='heuristic-v2'):
    identity=provenance(settings,engine)
    detail={'fixture_only':True}
    probe={'at':at.isoformat(),'engine':engine,'ok':ok,'provenance':identity,'detail':detail}
    with connect(settings) as con:
        con.execute('INSERT INTO engine_health VALUES(?,?,?,?,?,?)',
                    (at.isoformat(),engine,int(ok),dumps(identity),dumps(detail),digest(dumps(probe))))


def test_v1_health_is_never_v2_observation(workspace):
    sample(workspace,datetime.now(UTC),engine='heuristic-v1')
    value=engine_status(workspace)
    assert value['status']=='pending' and value['samples']==0


def test_failed_sample_and_gap_restart_contiguous_window(workspace):
    start=datetime(2026,10,3,0,0,tzinfo=UTC)
    sample(workspace,start)
    sample(workspace,start+timedelta(minutes=5),False)
    sample(workspace,start+timedelta(minutes=10))
    sample(workspace,start+timedelta(minutes=15))
    value=engine_status(workspace,clock=start+timedelta(minutes=16))
    assert value['samples']==2 and value['historical_failed_samples']==1
    assert value['elapsed_hours']==round(5/60,2) and value['status']=='observing'
    sample(workspace,start+timedelta(hours=1))
    assert engine_status(workspace,clock=start+timedelta(hours=1,minutes=1))['samples']==1


def test_stale_last_probe_cannot_pass(workspace):
    moment=datetime(2026,10,3,tzinfo=UTC)
    sample(workspace,moment)
    assert engine_status(workspace,clock=moment+timedelta(hours=1))['status']=='failed'


def test_read_and_receipt_use_only_durable_samples(workspace,monkeypatch):
    monkeypatch.setattr('nutri_literature.nlp_v2.CpuParser.parse',lambda *a,**k:pytest.fail('NLP invoked by operational read'))
    moment=datetime.now(UTC)-timedelta(minutes=10)
    sample(workspace,moment)
    sample(workspace,moment+timedelta(minutes=5))
    value=soak_receipt(workspace)
    assert len(value['samples'])==2 and value['started_at']==moment.isoformat()
    assert value['checks']=={}
    assert engine_status(workspace)['samples']==2
