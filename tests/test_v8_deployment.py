"""Deployment contract tests; these do not start SSH or measure model accuracy."""
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from scripts.deploy_evidence_v8 import package_sources, ssh


def test_gpu_hosts_are_explicitly_allowlisted():
    assert ssh("100.84.183.6")[-1] == "keunsoo@100.84.183.6"
    with pytest.raises(ValueError, match="allowlist"):
        ssh("unverified-gpu.example")
    assert "StrictHostKeyChecking=yes" in ssh("100.93.148.94")


def test_verified_ipv6_keeps_the_existing_host_identity_and_other_routes():
    default=ssh('100.84.183.6')
    assert '-6' not in default and 'HostKeyAlias=100.84.183.6' in default
    command=ssh('100.84.183.6',ipv6=True)
    assert '-6' in command and 'HostKeyAlias=100.84.183.6' in command
    assert 'StrictHostKeyChecking=yes' in command and 'BatchMode=yes' in command
    assert ssh('100.93.148.94')[-1]=='keunsoo@100.93.148.94'
    assert not any(v.startswith('HostKeyAlias') for v in ssh('100.93.148.94'))
    with pytest.raises(ValueError):ssh('fd7a:115c:a1e0::134:b707')
    with pytest.raises(ValueError,match='verified IPv6'):ssh('100.93.148.94',ipv6=True)


def test_remote_timeout_does_not_replay_mutations_or_disclose_the_command(monkeypatch):
    from scripts import deploy_evidence_v8 as deployment
    import subprocess
    calls=[]
    def expired(command,**kwargs):
        calls.append((command,kwargs))
        raise subprocess.TimeoutExpired(command,kwargs['timeout'])
    monkeypatch.setattr(deployment.subprocess,'run',expired)
    with pytest.raises(TimeoutError,match='remote completion unconfirmed') as error:
        deployment.remote('100.84.183.6','private_command_body',b'private_payload',timeout=12)
    assert len(calls)==1
    assert 'private_command_body' not in str(error.value) and 'private_payload' not in str(error.value)
    assert calls[0][0][-2]=='keunsoo@100.84.183.6'


def test_remote_host_key_failure_is_not_retried(monkeypatch):
    from scripts import deploy_evidence_v8 as deployment
    from types import SimpleNamespace
    calls=[]
    def rejected(command,**kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=255,stdout=b'',stderr=b'Host key verification failed')
    monkeypatch.setattr(deployment.subprocess,'run',rejected)
    with pytest.raises(RuntimeError,match='Host key verification failed'):
        deployment.remote('100.84.183.6','print(1)')
    assert len(calls)==1


def test_only_failed_tcp_connect_can_use_the_verified_second_address(monkeypatch):
    from scripts import deploy_evidence_v8 as deployment
    from types import SimpleNamespace
    calls=[]
    def transport(command,**kwargs):
        calls.append((command,kwargs))
        if len(calls)==1:
            return SimpleNamespace(returncode=255,stdout=b'',
                stderr=b'ssh: connect to host 100.84.183.6 port 22: Connection timed out\r\n')
        return SimpleNamespace(returncode=0,stdout=b'result',stderr=b'')
    monkeypatch.setattr(deployment.subprocess,'run',transport)
    assert deployment.remote('100.84.183.6','print(1)',b'payload')==b'result'
    assert len(calls)==2 and '-6' in calls[1][0]
    assert calls[0][0][-1]==calls[1][0][-1]
    assert all('HostKeyAlias=100.84.183.6' in c[0] and 'StrictHostKeyChecking=yes' in c[0] for c in calls)
    assert calls[0][1]['input']==calls[1][1]['input']==b'payload'


def test_authenticated_disconnect_does_not_replay_a_remote_command(monkeypatch):
    from scripts import deploy_evidence_v8 as deployment
    from types import SimpleNamespace
    calls=[]
    def disconnected(command,**kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=255,stdout=b'',stderr=b'Connection to 100.84.183.6 closed by remote host.')
    monkeypatch.setattr(deployment.subprocess,'run',disconnected)
    with pytest.raises(RuntimeError):deployment.remote('100.84.183.6','print(1)')
    assert len(calls)==1


def test_new_peer_requires_unchanged_authenticated_host_key_receipt(tmp_path, monkeypatch):
    from scripts import deploy_evidence_v8 as deployment
    keyfile = tmp_path / 'known_hosts'
    keyfile.write_bytes(b'100.68.188.102 ssh-ed25519 fixture\n')
    expected = hashlib.sha256(keyfile.read_bytes()).hexdigest()
    monkeypatch.setitem(deployment.PINNED_HOST_KEYS, '100.68.188.102', (keyfile, expected))
    command = ssh('100.68.188.102')
    assert command[-1] == 'keunsoo@100.68.188.102'
    assert 'UserKnownHostsFile=' + keyfile.as_posix() in command
    assert 'StrictHostKeyChecking=yes' in command
    keyfile.write_bytes(b'changed')
    with pytest.raises(ValueError, match='receipt changed'):
        ssh('100.68.188.102')
    keyfile.unlink()
    with pytest.raises(ValueError, match='receipt changed'):
        ssh('100.68.188.102')


def test_source_bundle_is_hashed_and_excludes_runtime_and_credentials(tmp_path):
    source = tmp_path / "literature_service"
    for name in ("nutri_literature/evidence_v8/model.py", "config/topics-v8.json",
                 "scripts/evidence_v8.py", "runtime/secrets.json", ".env"):
        p = source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}" if p.suffix == ".json" else "# source", encoding="utf8")
    with zipfile.ZipFile(io.BytesIO(package_sources(tmp_path))) as z:
        names = z.namelist()
        assert "scripts/evidence_v8.py" in names
        assert not any("runtime" in n or ".env" in n for n in names)
        manifest = json.loads(z.read("source-manifest.json"))
        assert manifest
        for name, value in manifest.items():
            assert hashlib.sha256(z.read(name)).hexdigest() == value
