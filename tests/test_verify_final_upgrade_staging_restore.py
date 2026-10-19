import pytest

from scripts.verify_final_upgrade_staging_restore import probe_wrapper, process_identity


WRAPPER = '''#!/usr/bin/env bash
set -euo pipefail
source="/permanent/source"
case "$mode" in
 internal) release="/permanent/internal"; port=8040 ;;
 public) release="/permanent/public"; port=8044 ;;
esac
export NUTRIGENOMICS_EXPLORER_PROFILE="$extension/validation/production.jsonl"
exec python -m scripts.serve_verified_release --release "$release" --application "$mode" --receipt "$extension/validation/production-startup.json" --port "$port"
'''


def test_probe_wrapper_preserves_verified_code_release_and_only_changes_scoped_outputs():
    result = probe_wrapper(WRAPPER, 49000, "/operator/test/profile.jsonl", "/operator/test/startup.json")
    assert "port=49000" in result
    assert "port=8040" not in result and "port=8044" not in result
    assert 'source="/permanent/source"' in result
    assert 'release="/permanent/public"' in result
    assert "-m scripts.serve_verified_release" in result
    assert "--receipt /operator/test/startup.json" in result
    assert "NUTRIGENOMICS_EXPLORER_PROFILE=/operator/test/profile.jsonl" in result
    assert "production-startup.json" in WRAPPER


@pytest.mark.parametrize("port", [8040, 8044, 8058, 8059, 0, 1023, 65536])
def test_refuses_every_protected_or_privileged_listener(port):
    with pytest.raises(ValueError):
        probe_wrapper(WRAPPER, port, "/operator/profile", "/operator/receipt")


def test_refuses_unrecognized_wrapper_without_modifying_the_input():
    with pytest.raises(ValueError):
        probe_wrapper("exec python -m unrelated_server --port 8000", 49000, "/operator/p", "/operator/r")


def test_missing_pid_is_explicitly_unavailable():
    assert process_identity(999999999) == {"pid": 999999999, "unavailable": True}
