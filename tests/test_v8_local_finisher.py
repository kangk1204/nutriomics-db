import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "scripts/finish_evidence_v8.py"
spec = importlib.util.spec_from_file_location("v8_finisher", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_export_extraction_is_immutable_and_confined(tmp_path):
    destination = tmp_path / "owned"
    destination.mkdir()
    module.write_recovered_file(destination, "predictions/paper.json", b"{\"relations\":[]}")
    module.write_recovered_file(destination, "predictions/paper.json", b"{\"relations\":[]}")
    with pytest.raises(ValueError, match="Immutable"):
        module.write_recovered_file(destination, "predictions/paper.json", b"new")
    for name in ("../outside.json", "/outside.json"):
        with pytest.raises(ValueError, match="escaped"):
            module.write_recovered_file(destination, name, b"new")
    assert not list(destination.rglob("*.transfer-part"))


def test_id_diagnostics_distinguish_correction_damage_and_unresolved():
    reference = [{"pmid": "1", "entities": [{"kind": "gene", "start": i, "end": i+1,
                   "native_ids": ["correct"]} for i in range(5)]}]
    pairs = [("wrong", "correct"), ("correct", "wrong"), (None, "correct"), ("wrong", "wrong"), (None, None)]
    rows = [{"pmid": "1", "kind": "gene", "start": i, "end": i+1,
             "previous_id": a, "selected_id": b} for i, (a, b) in enumerate(pairs)]
    result = module.id_transition_counts(reference, rows)["gene"]
    assert result["corrected"] == 1
    assert result["damaged"] == 1
    assert result["new_links"] == 1
    assert result["kept"] == 1  # An unchanged wrong ID is not a correction.
    assert result["nil"] == 1


def test_submission_report_requires_exact_twice_scored_proofs(tmp_path):
    output = tmp_path / "v8"
    output.mkdir()
    evaluation = output / "evaluation.json"
    evaluation.write_text('{"papers":100}')
    proof = output / "offline-reproduction.json"
    proof.write_text(json.dumps({"identical": True, "evaluation_sha256": "changed"}))
    with pytest.raises(ValueError, match="twice-rescored"):
        module.verified_report_evaluations(output, tmp_path)
    proof.write_text(json.dumps({"identical": True, "evaluation_sha256": hashlib.sha256(evaluation.read_bytes()).hexdigest()}))
    assert module.verified_report_evaluations(output, tmp_path) == (evaluation, None)
    r2 = tmp_path / "relation-repair-v6-gpu-r2"
    (r2 / "predictions").mkdir(parents=True)
    manifest = r2 / "predictions/manifest.json"
    manifest.write_text('{"papers":100}')
    saved = {"papers": 100, "source": "verified test fixture"}
    (r2 / "evaluation.json").write_text(json.dumps(saved))
    (r2 / "offline-reproduction.json").write_text(json.dumps({"identical": True, "metrics": saved,
        "prediction_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}))
    assert module.verified_report_evaluations(output, tmp_path) == (evaluation, r2 / "evaluation.json")
    manifest.write_text('{"papers":99}')
    with pytest.raises(ValueError, match="unverified r2"):
        module.verified_report_evaluations(output, tmp_path)


def test_publication_failure_preserves_completed_report_and_metrics(tmp_path):
    metrics = {"tp": 65, "fp": 63, "fn": 89}
    state = {"metrics": metrics}
    def publisher(_):
        raise RuntimeError("temporary network interruption")
    module.deliver_scored_result(tmp_path, state, report_builder=lambda _: {"report_sha256": "receipt"},
                                publisher=publisher)
    assert state["evaluation_complete"] is True
    assert state["metrics"] == metrics
    assert state["submission_report"]["report_sha256"] == "receipt"
    assert state["status"] == "development_evaluation_complete_publication_pending"


def test_documentation_requires_complete_scoring_and_preserves_results_when_git_is_dirty():
    calls=[]
    def updater(directory,**kwargs):
        calls.append((directory,kwargs))
        raise ValueError('clean worktree required')
    with pytest.raises(ValueError,match='completed scoring'):
        module.document_scored_result({},'feature/test',updater=updater)
    assert not calls
    state={'evaluation_complete':True,'metrics':{'tp':65,'fp':63,'fn':89},
           'submission_report':{'directory':'verified-report'}}
    module.document_scored_result(state,'feature/test',push=True,updater=updater)
    assert state['documentation_pending'] is True and state['metrics']['tp']==65
    assert calls==[('verified-report',{'branch':'feature/test','push':True})]


def test_prediction_export_retries_network_loss_but_preserves_the_checkpoint(tmp_path):
    state={'running':True};calls=[]
    def exporter(*args):
        calls.append(args)
        if len(calls)==1:raise module.RemoteTransportError('Connection reset by peer')
        return {'archive_sha256':'verified'}
    assert module.recover_with_checkpoint('host',tmp_path,'experiment',state,exporter) is False
    assert state['recovery_pending'] is True and state['running'] is True
    assert 'evaluation_complete' not in state
    assert module.recover_with_checkpoint('host',tmp_path,'experiment',state,exporter) is True
    assert state['transport']=={'archive_sha256':'verified'}
    assert state['recovery_attempts']==2 and state['recovery_pending'] is False


@pytest.mark.parametrize('error',[ValueError('Frozen hash mismatch'),RuntimeError('Remote export verification failed')])
def test_prediction_integrity_or_remote_computation_errors_are_not_retried(tmp_path,error):
    state={}
    def exporter(*args):raise error
    with pytest.raises(type(error),match=str(error)):
        module.recover_with_checkpoint('host',tmp_path,'experiment',state,exporter)
    assert state['recovery_attempts']==1 and not state.get('recovery_pending')


def test_all_native_scorer_dependencies_are_bound_before_evaluation(tmp_path):
    source=tmp_path/'source';output=tmp_path/'experiment';source.mkdir();output.mkdir()
    path=source/'native_scorer.py';path.write_bytes(b'unchanged raw ID and type rules')
    manifest=output/'training-source-manifest.json'
    manifest.write_text(json.dumps({'native_scorer.py':hashlib.sha256(path.read_bytes()).hexdigest()}))
    (output/'training-source-verification.json').write_text(json.dumps({
        'source_manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest()}))
    assert module.verify_local_source_manifest(output,source)['verified_files']==1
    path.write_bytes(b'different evaluator')
    with pytest.raises(ValueError,match='source mismatch'):
        module.verify_local_source_manifest(output,source)
    manifest.write_bytes(b'{}')
    with pytest.raises(ValueError,match='source manifest changed'):
        module.verify_local_source_manifest(output,source)
