"""Recover verified v8 predictions and score locally; never send gold to a server."""
from __future__ import annotations

import argparse
import io
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "literature_service"))
from deploy_evidence_v8 import ALLOWED_HOSTS, PROJECT, RemoteTransportError, remote, run_remote


def write_recovered_file(output, name, value):
    relative = PurePosixPath(name)
    destination = output.joinpath(*relative.parts).resolve()
    if relative.is_absolute() or ".." in relative.parts or not destination.is_relative_to(output.resolve()):
        raise ValueError("Export extraction escaped experiment")
    if destination.exists() and destination.read_bytes() != value:
        raise ValueError("Immutable recovered output changed: " + name)
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".transfer-part")
        with temp.open("wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(destination)


def status(host, experiment="nutriomics-evidence-v8"):
    code = """import json,psutil
from pathlib import Path
b=Path(PROJECT);h=b/'runtime'/EXPERIMENT;p=h/'pipeline-state.json'
v=json.loads(p.read_text()) if p.exists() else {'status':'not_started'}
v['predictions_ready']=(h/'predictions/manifest.json').exists()
v['supervisor_alive']=bool(v.get('pid') and psutil.pid_exists(v['pid']))
v['child_alive']=bool(v.get('child_pid') and psutil.pid_exists(v['child_pid']))
print(json.dumps(v))
"""
    return json.loads(remote(host, "PROJECT=" + repr(PROJECT) + "\nEXPERIMENT=" + repr(experiment) + "\n" + code, timeout=45))


def verify_local_source_manifest(output, source_root=ROOT / "literature_service"):
    """Bind the actual native scorer dependencies as well as the v8 model code."""
    manifest = output / 'training-source-manifest.json'
    expected = json.loads((output / 'training-source-verification.json').read_text(encoding='utf8'))
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != expected['source_manifest_sha256']:
        raise ValueError('Pinned training/scorer source manifest changed')
    source_root = source_root.resolve()
    files = json.loads(manifest.read_text(encoding='utf8'))
    for name, digest in files.items():
        path = (source_root / name).resolve()
        if (not path.is_relative_to(source_root) or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError('Pinned training/scorer source mismatch: ' + name)
    return {'source_manifest_sha256': expected['source_manifest_sha256'], 'verified_files': len(files)}


def recover(host, output, experiment="nutriomics-evidence-v8"):
    from nutri_literature.evidence_v8.common import atomic, filehash, read
    code = """import json,os,sys,subprocess,zipfile,hashlib
from pathlib import Path
b=Path(PROJECT);h=b/'runtime'/EXPERIMENT;s=Path(json.loads((h/'pipeline-contract.json').read_text())['source_directory'])
contract=json.loads((h/'pipeline-contract.json').read_text());source_manifest=(s/'source-manifest.json').read_bytes()
if hashlib.sha256(source_manifest).hexdigest()!=contract['source_manifest_sha256']:raise ValueError('Pinned full source manifest')
for name,digest in json.loads(source_manifest).items():
 p=(s/name).resolve()
 if not p.is_relative_to(s.resolve()) or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('Pinned full source file')
env={**os.environ,'EVIDENCE_V8_HOME':str(h),'PYTHONPATH':str(s),'OMP_NUM_THREADS':'1'}
subprocess.run([str(b/'.venv/bin/python'),'-B',str(s/'scripts/evidence_v8.py'),'verify-export','--home',str(h)],env=env,stdout=subprocess.DEVNULL,check=True)
receipt=json.loads((h/'export-verification.json').read_text())
frozen=json.loads((h/'freeze.json').read_text())
extra={n:frozen['files'][n] for n in ['selected/id.json','selected/relation.json','ner/manifest.json','retrieval/manifest.json','policy.json','models.json']}
for n,digest in extra.items():
 if hashlib.sha256((h/n).read_bytes()).hexdigest()!=digest:raise ValueError('Module diagnostics hash')
with zipfile.ZipFile(sys.stdout.buffer,'w',zipfile.ZIP_DEFLATED) as z:
 for name in [*receipt['export_files'],'export-verification.json',*extra]:
  p=(h/name).resolve()
  if not p.is_relative_to(h.resolve()):raise ValueError('Export path escape')
  z.write(p,name)
 z.writestr('module-diagnostics-verification.json',json.dumps(extra,sort_keys=True))
 z.writestr('training-source-manifest.json',source_manifest)
 z.writestr('training-source-verification.json',json.dumps({'source_manifest_sha256':contract['source_manifest_sha256']}))
"""
    archive = output / "server-results.zip"
    temp = archive.with_suffix(".part")
    with temp.open("wb") as stream:
        result = run_remote(host, "PROJECT=" + repr(PROJECT) + "\nEXPERIMENT=" + repr(experiment) + "\n" + code,
                            stdout=stream, timeout=1800)
    if result.returncode:
        raise RuntimeError("Authenticated export failed: " + result.stderr.decode(errors="replace")[-600:])
    temp.replace(archive)
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            write_recovered_file(output, entry.filename, z.read(entry))
    proof = read(output / "export-verification.json")
    for relative, digest in proof["export_files"].items():
        if filehash(output / relative) != digest:
            raise ValueError("Recovered file hash mismatch")
    frozen = read(output / "freeze.json")
    for relative, digest in read(output / "module-diagnostics-verification.json").items():
        if frozen["files"].get(relative) != digest or filehash(output / relative) != digest:
            raise ValueError("Frozen module diagnostics verification failed")
    source_verification = verify_local_source_manifest(output)
    receipt = {"host": host, "transport": "strict-host-key authenticated SSH",
               "archive_sha256": filehash(archive), "freeze_sha256": proof["freeze_sha256"],
               "verification_sha256": filehash(output / "export-verification.json"),
               "gold_exported": False, "model_weights_downloaded": False,
               "full_scientific_source_verification": source_verification}
    atomic(output / "transport-verification.json", receipt, True)
    return receipt


def recover_with_checkpoint(host, output, experiment, state, recoverer=None):
    """Retry authenticated export after network loss without hiding invalid data."""
    recoverer = recoverer or recover
    state['recovery_attempts'] = state.get('recovery_attempts', 0) + 1
    try:
        state['transport'] = recoverer(host, output, experiment)
    except (RemoteTransportError, TimeoutError, subprocess.TimeoutExpired) as error:
        state.update(recovery_pending=True, recovery_error=type(error).__name__ + ': ' + str(error)[:400])
        return False
    state.update(recovery_pending=False, recovery_error=None)
    return True


def id_transition_counts(reference, decisions):
    from collections import Counter, defaultdict
    expected = defaultdict(set)
    for d in reference:
        for e in d.get("entities", []):
            key = (str(d["pmid"]), e["kind"].replace("component", "chemical"), e["start"], e["end"])
            expected[key].update(map(str, e.get("native_ids", [])))
    changes = defaultdict(Counter)
    for r in decisions:
        key = (str(r["pmid"]), r["kind"], r["start"], r["end"])
        old, new, truth = r.get("previous_id"), r.get("selected_id"), expected.get(key, set())
        counts = changes[r["kind"]]
        counts["mentions"] += 1
        counts["kept"] += int(old == new and old is not None)
        counts["nil"] += int(new is None)
        counts["changed"] += int(old is not None and new is not None and old != new)
        counts["new_links"] += int(old is None and new is not None)
        counts["withdrawn"] += int(old is not None and new is None)
        counts["corrected"] += int(old is not None and old not in truth and new in truth)
        counts["damaged"] += int(old in truth and new not in truth)
    return {k: dict(v) for k, v in changes.items()}


def build_diagnostics(output):
    """Post-scoring aggregates only; these records never enter model selection."""
    from collections import Counter
    from nutri_literature.evidence_v8.common import atomic, read
    from nutri_literature.repair_v3.runner import gold
    reference, _ = gold()  # Called only after all predictions and exact scoring.
    changes = id_transition_counts(reference, read(output / "normalized/development.json")["decisions"])
    selected = {name: read(output / "selected" / (name + ".json")) for name in ("id", "relation")}
    stages = {}
    for name in selected:
        metrics = Counter()
        for fold in selected[name]["outer_folds"]:
            q = fold["quality"]
            q = q.get("strict_span_ID") or q
            metrics.update({k: int(q[k]) for k in ("tp", "fp", "fn")})
        stages["training_oof_" + name] = dict(metrics)
    value = {"status": "completed", "stages": stages,
             "id_changes": changes,
             "label_provenance": {task + "_training_task_document_units": {
                 "documents": selected[task]["ensemble_calibration"]["papers"],
                 "public_gold": selected[task]["ensemble_calibration"]["papers"],
                 "weak_labels": 0} for task in selected},
             "label_provenance_scope": "Per-task document counts; tasks may overlap and must not be summed as unique papers",
             "scope": "Training OOF stage metrics and repeated development ID transitions; not independent final performance",
             "kept_definition": "Unchanged linked ID, whether correct or not",
             "corrected_definition": "Previously linked incorrect ID changed to an annotated correct ID"}
    atomic(output / "diagnostics.json", value, True)
    return value


def publish(output):
    """Publish only to the existing literature staging sidecar, after exact scoring."""
    code = """import sys,json,os,subprocess
from pathlib import Path
b=Path('/home/keunsoo/projects/nutriomics-literature-platform');h=b/'runtime';envelope=json.loads(sys.stdin.buffer.read());v=envelope['evaluation'];blob=json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
if v.get('engine')!='nutriomics-evidence-v8' or v.get('papers')!=100 or v.get('gold_relations')!=154:raise ValueError('cohort/engine')
d=h/'model-evaluation-v8';d.mkdir(exist_ok=True);p=d/'evaluation.json'
if p.exists() and p.read_bytes()!=blob:raise ValueError('Immutable evaluation conflict')
q=p.with_suffix('.part');q.write_bytes(blob);q.chmod(0o600);q.replace(p)
diagnostics=json.dumps(envelope['diagnostics'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode();p=d/'diagnostics.json'
if p.exists() and p.read_bytes()!=diagnostics:raise ValueError('Immutable diagnostics conflict')
q=p.with_suffix('.part');q.write_bytes(diagnostics);q.chmod(0o600);q.replace(p)
env={**os.environ,'LITERATURE_HOME':str(h),'NUTRIOMICS_PUBLIC_RELEASE':'/home/keunsoo/projects/nutrigenomics-db/rebuild/20261022/releases/nutriomics-composed-candidate-20261022-v3','PYTHONPATH':str(b/'web-source')+':'+str(b/'web-source/literature_service')}
subprocess.run([str(b/'.venv/bin/python'),'-B',str(b/'web-source/scripts/refresh_platform_snapshots.py')],env=env,check=True,stdout=subprocess.DEVNULL)
subprocess.run([str(b/'.venv/bin/supervisorctl'),'-c',str(h/'supervisor/supervisord.conf'),'restart','literature-public','literature-internal'],check=True,stdout=subprocess.DEVNULL)
print(json.dumps({'staging_published':True,'default_engine':'heuristic-v1'}))
"""
    known = "C:/Projects/00_NutriGenomics_auto/outputs/publication/2026-10-22/tailscale-known-hosts"
    command = ["ssh", "-J", "keunsoo@100.93.148.94", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=12", "-o", "UserKnownHostsFile=" + known, "keunsoo@100.110.101.17",
               "python3 -c " + shlex.quote(code)]
    envelope = {"evaluation": json.loads((output / "evaluation.json").read_text(encoding="utf8")),
                "diagnostics": json.loads((output / "diagnostics.json").read_text(encoding="utf8"))}
    result = subprocess.run(command, input=json.dumps(envelope, ensure_ascii=False).encode("utf8"), capture_output=True, timeout=900)
    if result.returncode:
        raise RuntimeError("Local evaluation saved; staging publication failed")
    return json.loads(result.stdout)


def verified_report_evaluations(output, runtime):
    """Only twice-rescored local artifacts may enter the submission report."""
    from nutri_literature.evidence_v8.common import filehash, read
    proof = read(output / "offline-reproduction.json")
    evaluation = output / "evaluation.json"
    if proof.get("identical") is not True or proof.get("evaluation_sha256") != filehash(evaluation):
        raise ValueError("Submission report needs an unchanged, twice-rescored v8 evaluation")
    r2_home = runtime / "relation-repair-v6-gpu-r2"
    r2_evaluation = r2_home / "evaluation.json"
    r2_proof = r2_home / "offline-reproduction.json"
    if not r2_evaluation.exists() or not r2_proof.exists():
        return evaluation, None
    saved, reference = read(r2_evaluation), read(r2_proof)
    if (reference.get("identical") is not True or reference.get("metrics") != saved
            or reference.get("prediction_manifest_sha256") != filehash(r2_home / "predictions/manifest.json")):
        raise ValueError("Submission report refused an unverified r2 evaluation")
    return evaluation, r2_evaluation


def submission_report(output):
    from datetime import datetime, timezone
    from build_v8_submission_report import build_payload, write_report
    from nutri_literature.evidence_v8.common import filehash
    runtime = ROOT / "literature_service/runtime"
    v8_evaluation, r2_evaluation = verified_report_evaluations(output, runtime)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + str(time.time_ns())
    inputs = output / "report-inputs"
    inputs.mkdir(exist_ok=True)
    progress = inputs / ("progress-" + stamp + ".json")
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/check_evidence_v8_progress.py"),
                             "--output", str(progress)], cwd=ROOT, capture_output=True, timeout=100)
    if result.returncode:
        raise RuntimeError("Scoring completed, but the report progress receipt could not be created")
    clinical = runtime / "v8-clinical-contextual/summary.json"
    nlp = ROOT / 'docs/status/nlp-validation-2026-10-24.json'
    payload = build_payload(progress, r2_evaluation=r2_evaluation, v8_evaluation=v8_evaluation,
                            clinical_summary=clinical if clinical.exists() else None,
                            nlp_validation_path=nlp if nlp.exists() else None)
    destination = output / "submission-reports" / stamp
    manifest = write_report(payload, destination)
    return {"directory": str(destination), "report_sha256": manifest["files"]["report.json"],
            "manifest_sha256": filehash(destination / "report-manifest.json"),
            "v8_evaluation_sha256": filehash(v8_evaluation),
            "all_system_validation_complete": False}


def deliver_scored_result(output, state, report_builder=submission_report, publisher=publish):
    """Publishing failure must not erase completed, verified scientific scoring."""
    state["evaluation_complete"] = True
    state["submission_report"] = report_builder(output)
    try:
        state["publication"] = publisher(output)
        state["status"] = "development_evaluation_complete"
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        state.update(status="development_evaluation_complete_publication_pending",
                     publication_error=type(error).__name__ + ": " + str(error)[:400])
    return state


def document_scored_result(state, branch, push=False, updater=None):
    if state.get('evaluation_complete') is not True or not state.get('submission_report', {}).get('directory'):
        raise ValueError('Documentation requires completed scoring and its report')
    if updater is None:
        from update_v8_result_docs import update
        updater = update
    try:
        state['documentation'] = updater(state['submission_report']['directory'], branch=branch, push=push)
        state['documentation_pending'] = False
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        state.update(documentation_pending=True, documentation_error=type(error).__name__ + ': ' + str(error)[:400])
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=ALLOWED_HOSTS, default="100.84.183.6")
    parser.add_argument("--home", type=Path, default=ROOT / "literature_service/runtime/nutriomics-evidence-v8")
    parser.add_argument("--remote-experiment", default="nutriomics-evidence-v8")
    parser.add_argument("--max-days", type=int, default=84)
    parser.add_argument("--resume-interrupted", action="store_true")
    parser.add_argument('--documentation-branch', help='Explicit branch for committing completed evaluation reports')
    parser.add_argument('--push-documentation', action='store_true')
    args = parser.parse_args()
    if args.push_documentation and not args.documentation_branch:
        raise ValueError('Pushing documentation requires an explicit branch')
    import re
    if not re.fullmatch(r"nutriomics-evidence-v8(?:-[a-z0-9]+)*", args.remote_experiment):
        raise ValueError("Remote experiment must be an owned v8 runtime directory")
    if not 1 <= args.max_days <= 84:
        raise ValueError("Finite observation must be between 1 and 84 days")
    output = args.home.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["EVIDENCE_V8_HOME"] = str(output)
    from nutri_literature.evidence_v8.common import atomic, filehash, read
    from nutri_literature.locking import lock
    directory = output / "local-finisher"
    directory.mkdir(exist_ok=True)
    with lock(directory / "owner.lock"):
        path = directory / "state.json"
        prior = read(path) if path.exists() else {}
        state = {"pid": os.getpid(), "host": args.host, "running": True,
                 "started_at": prior.get("started_at", time.time()), "max_days": args.max_days,
                 "additional_generative_model_calls": 0, "gold_stays_local": True,
                 "documentation_branch":args.documentation_branch,
                 "push_documentation":args.push_documentation}
        atomic(path, state)
        try:
            while time.time() - state["started_at"] < args.max_days * 86400:
                if (directory / "STOP").exists():
                    state.update(status="stopped_by_request")
                    break
                try:
                    observed = status(args.host, args.remote_experiment)
                    state.update(remote=observed, checked_at=time.time(), connection_error=None)
                    atomic(path, state)
                except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
                    state.update(connection_error=type(error).__name__, checked_at=time.time())
                    atomic(path, state)
                    time.sleep(60)
                    continue
                if observed.get("predictions_ready"):
                    if not recover_with_checkpoint(args.host, output, args.remote_experiment, state):
                        atomic(path, state)
                        time.sleep(60)
                        continue
                    from nutri_literature.evidence_v8.runner import evaluate_saved
                    first, second = evaluate_saved(), evaluate_saved()
                    if first != second:
                        raise ValueError("Offline re-scoring differed")
                    atomic(output / "offline-reproduction.json", {"identical": True,
                           "evaluation_sha256": filehash(output / "evaluation.json"), "sealed_labels_opened": False}, True)
                    build_diagnostics(output)
                    state["metrics"] = first["scores"]["nutriomics-evidence-v8"]["metrics"]
                    deliver_scored_result(output, state)
                    if args.documentation_branch:
                        document_scored_result(state, args.documentation_branch, push=args.push_documentation)
                    break
                if args.resume_interrupted:
                    from resume_evidence_v8 import interrupted, resume
                    if interrupted(observed):
                        try:
                            state["resume"] = resume(args.host, args.remote_experiment)
                        except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
                            state.update(connection_error=type(error).__name__, checked_at=time.time())
                            atomic(path, state)
                            time.sleep(60)
                            continue
                        atomic(path, state)
                if observed.get("status") in {"failed", "resource_budget_exceeded", "waiting_for_input_or_repair", "cancelled"}:
                    state.update(status="attention_required", error=observed.get("error", observed["status"]))
                    break
                time.sleep(60)
            else:
                state.update(status="finite_observation_expired", unfinished=True)
        except Exception as error:
            state.update(status="failed", error=type(error).__name__ + ": " + str(error)[:600])
        finally:
            state.update(running=False, finished_at=time.time())
            atomic(path, state)


if __name__ == "__main__":
    main()
