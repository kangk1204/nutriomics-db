"""Resource admission regressions; no training or GPU allocation is performed."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import v8_resource_layer as layer
from scripts import run_evidence_v8_pipeline as supervisor


POLICY={'user_id':1000,'maximum_total_desktop_mib':512,'additional_desktops':[
    {'executable':'/usr/bin/nautilus','maximum_mib':64},
    {'executable':'/opt/google/chrome/chrome','maximum_mib':128}]}
PROCESS={'pid':'12','gpu_uuid':'GPU-a','used_mib':'80'}


def identity(executable='/opt/google/chrome/chrome',uid=1000,args=None):
    return {'executable':executable,'real_uid':uid,'effective_uid':uid,
            'arguments':['chrome','--type=gpu-process'] if args is None else args}


@pytest.mark.parametrize('executable,memory,args,allowed',[
    ('/usr/bin/nautilus','38',[],True),
    ('/opt/google/chrome/chrome','80',['chrome','--type=gpu-process'],True),
    ('/opt/google/chrome/chrome','129',['chrome','--type=gpu-process'],False),
    ('/usr/bin/nautilus','65',[],False),
    ('/opt/google/chrome/chrome','80',['chrome'],False),
    ('/usr/bin/python3','1',[],False),
    ('/usr/bin/unknown','1',[],False),
    ('/opt/google/chrome/chrome','[N/A]',['--type=gpu-process'],False),
    ('/opt/google/chrome/chrome','nan',['--type=gpu-process'],False),
])
def test_only_verified_small_desktop_roles_are_admitted(executable,memory,args,allowed):
    proc={**PROCESS,'used_mib':memory}
    assert layer.additional_desktop_allowed(proc,'GPU-a',POLICY,
        lookup=lambda _:identity(executable,args=args),resolve=lambda p:p) is allowed


def test_other_users_devices_disappearing_processes_and_excessive_policy_remain_blocked():
    assert not layer.additional_desktop_allowed(PROCESS,'GPU-a',POLICY,lambda _:identity(uid=2000))
    assert not layer.additional_desktop_allowed(PROCESS,'GPU-b',POLICY,lambda _:identity())
    def missing(_):raise OSError('process exited')
    assert not layer.additional_desktop_allowed(PROCESS,'GPU-a',POLICY,missing)
    oversized={'user_id':1000,'additional_desktops':[{'executable':'/opt/google/chrome/chrome','maximum_mib':512}]}
    assert not layer.additional_desktop_allowed(PROCESS,'GPU-a',oversized,lambda _:identity())


def test_install_changes_only_the_resource_predicate_and_audits_acceptance(tmp_path,monkeypatch):
    marker=object();original=lambda *args:False
    common=SimpleNamespace(gpu_process_allowed=original,model_math=marker)
    monkeypatch.setattr(layer,'process_identity',lambda _:identity())
    monkeypatch.setattr(layer,'nvidia_processes',lambda:[PROCESS])
    audit=tmp_path/'audit.jsonl'
    assert layer.install(common,POLICY,audit,'a'*64) is original
    assert common.model_math is marker
    assert common.gpu_process_allowed(PROCESS,'GPU-a',{})
    record=json.loads(audit.read_text())
    assert record['used_mib']==80 and record['execution_sha256']=='a'*64
    assert 'arguments' not in record and 'user' not in record


def test_cache_policy_sets_library_mode_without_loading_or_replacing_a_model(monkeypatch):
    import os
    monkeypatch.setenv('HF_HUB_OFFLINE','0');monkeypatch.setenv('TRANSFORMERS_OFFLINE','0')
    layer.configure_model_resolution({})
    assert os.environ['HF_HUB_OFFLINE']=='0'
    layer.configure_model_resolution({'cached_models_only':True})
    assert os.environ['HF_HUB_OFFLINE']=='1' and os.environ['TRANSFORMERS_OFFLINE']=='1'


def test_desktop_aggregate_budget_and_foreign_compute_are_checked():
    original=lambda *a:False
    rows=[{**PROCESS,'pid':str(i),'used_mib':'128'} for i in range(1,5)]
    assert layer.desktop_budget_allowed(rows,'GPU-a',POLICY,original,{},999,lambda _:identity())
    assert not layer.desktop_budget_allowed(rows+[{**PROCESS,'pid':'5'}],
        'GPU-a',POLICY,original,{},999,lambda _:identity())
    def lookup(pid):return identity('/usr/bin/python3' if pid==5 else '/opt/google/chrome/chrome')
    assert not layer.desktop_budget_allowed(rows[:1]+[{**PROCESS,'pid':'5','used_mib':'1'}],
        'GPU-a',POLICY,original,{},999,lookup)


@pytest.fixture
def execution(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'source-manifest.json').write_text('{}')
    home=tmp_path/'home';directory=home/'runtime-control/desktop-v2';directory.mkdir(parents=True)
    (directory/'v8_resource_layer.py').write_bytes(Path(layer.__file__).read_bytes())
    (directory/'supervisor.py').write_bytes(Path(supervisor.__file__).read_bytes())
    (directory/'desktop-policy.json').write_text(json.dumps(POLICY))
    config={'schema_version':'v8-resource-admission-v1','scientific_source_manifest_sha256':layer.sha(source/'source-manifest.json'),
            'files':{p.name:layer.sha(p) for p in directory.iterdir()},'predecessor_receipts':{}}
    path=directory/'execution.json';path.write_text(json.dumps(config))
    return path,source,home


def test_execution_hash_is_pinned_before_loading_the_operational_layer(execution):
    path,source,home=execution
    value=supervisor.resource_execution(path,source,home,layer.sha(path))
    command=supervisor.command('train-id',source,home,Path('/assets'),'python',execution=value)
    assert 'v8_resource_layer.py' in command[3] and '--configuration-sha256' in command
    assert '--stage' in command and command[-1]=='train-id' and 'evaluate' not in command
    with pytest.raises(ValueError,match='configuration changed'):
        supervisor.resource_execution(path,source,home,'f'*64)
    (path.parent/'desktop-policy.json').write_text('{}')
    with pytest.raises(ValueError,match='file mismatch'):
        supervisor.resource_execution(path,source,home,layer.sha(path))


def test_adapter_configuration_rejects_source_changes_and_path_escape(execution):
    path,source,home=execution
    layer.configuration(path,source,home,layer.sha(path))
    (source/'source-manifest.json').write_text('{"changed":true}')
    with pytest.raises(ValueError,match='scientific source'):
        layer.configuration(path,source,home,layer.sha(path))
    with pytest.raises(ValueError,match='escaped'):
        layer.configuration(path,source,home/'another-experiment',layer.sha(path))
