"""Operational changes must be auditable without changing scientific cache identities."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('finish',Path(__file__).resolve().parents[1]/'scripts/finish_evidence_v8.py')
finish=importlib.util.module_from_spec(spec);spec.loader.exec_module(finish)


@pytest.fixture
def exported(tmp_path):
    d=tmp_path/'runtime-control';d.mkdir()
    def write(name,value):
        p=d/name;p.write_text(json.dumps(value),encoding='utf8')
        return hashlib.sha256(p.read_bytes()).hexdigest()
    files={name:write(name,{'test':'fixed'}) for name in ['v8_resource_layer.py','supervisor.py','desktop-policy.json']}
    sha=write('execution.json',{'files':files})
    expected={'sha256':sha,'files':files,'scientific_source_manifest_sha256':'a'*64}
    (tmp_path/'resource-execution-expected.json').write_text(json.dumps(expected))
    write('manifest.json',{'scope':'resource_admission_only','configuration_sha256':sha,
        'scientific_source_manifest_sha256':'a'*64,'files':{**files,'execution.json':sha}})
    return tmp_path


def test_only_approved_complete_operational_export_is_accepted(exported):
    proof=finish.verify_operational_export(exported)
    assert proof['scope']=='resource_admission_only'
    (exported/'runtime-control/desktop-policy.json').write_text('changed')
    with pytest.raises(ValueError,match='file mismatch'):finish.verify_operational_export(exported)


def test_no_silent_fallback_when_layer_is_missing_or_unapproved(exported):
    expected=exported/'resource-execution-expected.json';blob=expected.read_bytes();expected.unlink()
    with pytest.raises(ValueError,match='Unapproved'):finish.verify_operational_export(exported)
    expected.write_bytes(blob);(exported/'runtime-control/manifest.json').unlink()
    with pytest.raises(ValueError,match='missing'):finish.verify_operational_export(exported)


def test_legacy_run_without_operational_layer_stays_supported(tmp_path):
    assert finish.verify_operational_export(tmp_path) is None
