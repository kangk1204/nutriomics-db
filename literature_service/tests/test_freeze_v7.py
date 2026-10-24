import json
import pytest
from nutri_literature.contract_v7 import sources
from nutri_literature.freeze_v7 import freeze,reserve_attempt


def test_failed_training_gate_cannot_open_any_final_labels(tmp_path):
    head=tmp_path/'head';head.mkdir()
    (head/'manifest.json').write_text(json.dumps({'outer_metrics':{'tp':1,'precision':.2,'recall':.1}}))
    missing=tmp_path/'DO_NOT_OPEN.json'
    with pytest.raises(ValueError,match='Nested'):
        freeze(head,missing,missing,missing,missing,tmp_path/'freeze.json')
    assert not (tmp_path/'freeze.json').exists()


def test_cohort_attempt_can_resume_but_cannot_change_artifact(tmp_path):
    spec=tmp_path/'spec.json'
    spec.write_text(json.dumps({'split':'test','corpus':'bc8','cohort_id':'protected-test'}))
    frozen=tmp_path/'freeze.json'
    frozen.write_text(json.dumps({'schema':'v7-frozen-relation-1','sources':sources()[0]}))
    reserve_attempt(tmp_path,spec,frozen)
    reserve_attempt(tmp_path,spec,frozen)
    frozen.write_text(json.dumps({'schema':'v7-frozen-relation-1','sources':sources()[0],'different_model':True}))
    with pytest.raises(ValueError,match='Immutable'):
        reserve_attempt(tmp_path,spec,frozen)
