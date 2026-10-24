import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'literature_service'))
from nutri_literature.evidence_v8 import common


def test_streaming_canonical_has_identical_unicode_float_and_nested_bytes(tmp_path,monkeypatch):
    value={'rows':[{'pmid':'1','text':'긴 원문 🧬 αβ'*20000,'candidate_ids':['C1','D2',None],
                    'weights':[0.0,1e-8,-1.5,True,False]}]*4,'source':{'b':2,'a':1}}
    expected=common.canonical(value).encode('utf8')
    assert b''.join(common.canonical_chunks(value))==expected
    assert common.canonical_digest(value)==hashlib.sha256(expected).hexdigest()
    def forbidden(*_):raise AssertionError('full canonical buffer allocated')
    monkeypatch.setattr(common,'canonical',forbidden)
    path=tmp_path/'rows.json'
    common.atomic(path,value,True);common.atomic(path,value,True)
    assert path.read_bytes()==expected
    with pytest.raises(ValueError,match='불변'):
        common.atomic(path,{'changed':True},True)
    assert path.read_bytes()==expected


def test_loaded_rows_share_equal_text_without_merging_versions_or_ids(tmp_path):
    text='A long article for many mentions. '*1000
    value={'rows':[{'pmid':'1','text':text,'menu':[None,'A']},
                   {'pmid':'1','text':text,'menu':[None,'B']},
                   {'pmid':'1','text':text+'revised','menu':[None,'A']}]}
    path=tmp_path/'rows.json';path.write_text(json.dumps(value),encoding='utf8')
    loaded=common.read(path)
    assert loaded==value
    assert loaded['rows'][0]['text'] is loaded['rows'][1]['text']
    assert loaded['rows'][0]['text']!=loaded['rows'][2]['text']
    assert loaded['rows'][0]['menu']!=loaded['rows'][1]['menu']


def test_streaming_failure_leaves_prior_file_and_no_partial(tmp_path):
    path=tmp_path/'state.json';path.write_bytes(b'previous')
    with pytest.raises(ValueError):common.atomic(path,{'bad':float('nan')})
    assert path.read_bytes()==b'previous'
    assert not list(tmp_path.glob('*.tmp-*'))


def test_candidate_ids_share_storage_across_profiles_without_changing_menus(tmp_path):
    identifier='D12345678901234567890'
    value={'rows':[{'menu':[None,identifier,'NIL-like-name'],
        'mention':{'candidate_ids':[identifier,'1234567'],'lexical_ids':[identifier],
                   'sapbert_ids':['1234567',identifier],'previous_id':identifier},
        'gold_ids':['1234567'],'rank_weight':1.0}]}
    a,b=tmp_path/'a.json',tmp_path/'b.json'
    a.write_text(json.dumps(value),encoding='utf8');b.write_bytes(a.read_bytes())
    first,second=common.read(a),common.read(b)
    assert first==second==value
    row=first['rows'][0]
    assert row['menu'][0] is None
    assert row['menu'][1] is row['mention']['candidate_ids'][0]
    assert row['menu'][1] is row['mention']['previous_id']
    assert row['menu'][1] is second['rows'][0]['menu'][1]
    assert common.canonical_digest(first)==common.canonical_digest(value)
