"""Run pinned v8 batch stages durably; gold evaluation stays on the local PC.

The supervisor owns only this experiment. It never promotes an engine, resets a
budget, changes another process, or retries invalid scientific inputs forever.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


STAGES = ("bootstrap", "probe", "audit", "smoke-ner", "smoke-id", "prepare", "train-ner",
          "train-id", "train-relations", "freeze", "predict")
CLINICAL_STAGES = ("clinical", "clinical-role")
NATIVE_STAGES = ("audit", "smoke", "encode", "train-drugprot", "train-cg", "freeze")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part-" + str(os.getpid()))
    with temporary.open("w", encoding="utf8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def verify_source(source):
    manifest = read(source / "source-manifest.json")
    for relative, digest in manifest.items():
        path = (source / relative).resolve()
        if not path.is_relative_to(source) or sha(path) != digest:
            raise ValueError("Pinned source integrity failure: " + relative)
    return sha(source / "source-manifest.json")


def resource_execution(path, source, home, expected_sha256):
    import importlib.util
    path = Path(path).resolve()
    if not path.is_relative_to(home / 'runtime-control'):
        raise ValueError('Resource execution escaped its experiment')
    if sha(path) != expected_sha256:
        raise ValueError('Approved resource configuration changed')
    value = read(path)
    adapter = path.parent / 'v8_resource_layer.py'
    if sha(adapter) != value.get('files', {}).get('v8_resource_layer.py'):
        raise ValueError('Unverified resource adapter')
    spec = importlib.util.spec_from_file_location('verified_v8_resource_layer', adapter)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.configuration(path, source, home, expected_sha256)
    return {'configuration':str(path), 'sha256':sha(path),
            'scientific_source_manifest_sha256':value['scientific_source_manifest_sha256'],
            'files':value['files']}


def command(stage, source, home, assets, python, native=False, execution=None):
    if native:
        if stage not in NATIVE_STAGES:
            raise ValueError("Unsupported native training stage")
        return [python, "-u", "-B", str(source / "scripts/evidence_v8_native.py"), stage,
                "--home", str(home)]
    if stage == "bootstrap":
        return [python, "-u", "-B", str(source / "scripts/bootstrap_evidence_v8.py"),
                "--source-root", str(assets), "--output", str(home), "--resolve-models"]
    if stage not in (*STAGES[1:], *CLINICAL_STAGES):
        raise ValueError("Stage is outside the approved training/prediction pipeline")
    if execution:
        config = Path(execution['configuration'])
        return [python, '-u', '-B', str(config.parent / 'v8_resource_layer.py'),
                '--configuration', str(config), '--configuration-sha256', execution['sha256'],
                '--source', str(source), '--home', str(home), '--stage', stage]
    return [python, "-u", "-B", str(source / "scripts/evidence_v8.py"), stage,
            "--home", str(home)]


def run(args):
    import fcntl
    import psutil
    source, home, assets = (p.resolve() for p in (args.source, args.home, args.assets))
    home.mkdir(parents=True, exist_ok=True)
    logs = home / "pipeline-logs"
    logs.mkdir(exist_ok=True)
    with (home / "pipeline-owner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "other_jobs_unchanged": True}
        source_sha = verify_source(source)
        layer_path = getattr(args, 'resource_execution', None)
        layer_sha = getattr(args, 'resource_execution_sha256', None)
        execution = resource_execution(layer_path, source, home, layer_sha) if layer_path else None
        if execution and (args.native_only or args.clinical_only):
            raise ValueError('This resource continuation is scoped to the main v8 experiment')
        stages = NATIVE_STAGES if args.native_only else CLINICAL_STAGES if args.clinical_only else STAGES
        contract = {"source_directory": str(source), "source_manifest_sha256": source_sha,
                    "assets": str(assets), "home": str(home), "stages": list(stages),
                    "asset_manifest_sha256": sha(assets / "asset-manifest.json")
                    if (assets / "asset-manifest.json").exists() else None,
                    "additional_generative_model_calls": 0, "local_evaluation_only": True,
                    "default_engine": "heuristic-v1"}
        contract["wait_for_existing_r2"] = args.wait_r2
        contract_path = home / "pipeline-contract.json"
        if contract_path.exists() and read(contract_path) != contract:
            raise ValueError("A new experiment directory is required for changed inputs")
        atomic(contract_path, contract)
        env = {**os.environ, "EVIDENCE_V8_HOME": str(home), "PYTHONUNBUFFERED": "1",
               "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
               "NUMEXPR_NUM_THREADS": "1", "TOKENIZERS_PARALLELISM": "false"}
        state_path = home / "pipeline-state.json"
        state = {"pid": os.getpid(), "started_at": time.time(), "completed_stages": [],
                 "status": "running", **contract}
        if execution:
            state['resource_execution'] = execution
        while args.wait_r2:
            r2 = Path("/home/keunsoo/projects/nutriomics-relation-v6-gpu-r2/runtime/relation-repair-v6-gpu-r2")
            job = read(r2 / "job-state.json")
            active = False
            if job.get("pid"):
                try:
                    active = abs(psutil.Process(job["pid"]).create_time() - job["process_created"]) < 1
                except psutil.NoSuchProcess:
                    pass
            if not active and (r2 / "predictions/manifest.json").is_file():
                break
            state.update(status="waiting_for_r2", stage="dependency", updated_at=time.time(),
                         r2_pid=job.get("pid"), r2_stage=job.get("stage"))
            atomic(state_path, state)
            for _ in range(60):
                if (home / "cancel-request.json").exists():
                    atomic(state_path, {**state, "status": "cancelled", "updated_at": time.time()})
                    return {"status": "cancelled", "checkpoint_preserved": True}
                time.sleep(5)
        for stage in stages:
            receipt = logs / (stage + ".complete.json")
            if receipt.exists():
                if read(receipt).get("source_manifest_sha256") != source_sha:
                    raise ValueError("Completed stage has a different source contract")
                if execution and read(receipt).get('resource_execution_sha256') != execution['sha256']:
                    predecessor = read(layer_path).get('predecessor_receipts', {})
                    if predecessor.get(str(receipt.relative_to(home))) != sha(receipt):
                        raise ValueError('Unverified predecessor stage receipt')
                state["completed_stages"].append(stage)
                continue
            attempt = 0
            while True:
                verify_source(source)
                if execution and resource_execution(layer_path, source, home, layer_sha) != execution:
                    raise ValueError('Resource execution changed during the batch')
                if (home / "cancel-request.json").exists():
                    atomic(state_path, {**state, "stage": stage, "status": "cancelled",
                                        "updated_at": time.time()})
                    return {"status": "cancelled", "checkpoint_preserved": True}
                attempt += 1
                stamp = str(time.time_ns())
                output = logs / (stage + "-" + stamp + ".log")
                with output.open("ab", buffering=0) as stream:
                    process = subprocess.Popen(command(stage, source, home, assets, sys.executable, args.native_only, execution),
                                               stdout=stream, stderr=subprocess.STDOUT, env=env)
                    state.update(stage=stage, child_pid=process.pid, attempt=attempt,
                                 status="running", log=str(output), updated_at=time.time())
                    atomic(state_path, state)
                    peak_rss = 0
                    while process.poll() is None:
                        try:
                            rss = psutil.Process(process.pid).memory_info().rss
                        except psutil.NoSuchProcess:
                            break
                        peak_rss = max(peak_rss, rss)
                        state.update(updated_at=time.time(), peak_child_rss_bytes=peak_rss)
                        atomic(state_path, state)
                        if rss > 8 * 1024**3:
                            # Terminate only the child created and still owned by this Popen.
                            process.terminate()
                            process.wait(timeout=30)
                            state.update(status="resource_budget_exceeded", returncode=process.returncode,
                                         error="Owned stage RSS exceeded 8GiB; previous checkpoints retained")
                            atomic(state_path, state)
                            return state
                        time.sleep(5)
                    code = process.wait()
                if code == 0:
                    atomic(receipt, {"stage": stage, "returncode": code,
                                     "source_manifest_sha256": source_sha,
                                     **({'resource_execution_sha256':execution['sha256']} if execution else {}),
                                     "completed_at": time.time(), "log_sha256": sha(output)})
                    state["completed_stages"].append(stage)
                    break
                if code != 75:
                    state.update(status="failed", returncode=code, updated_at=time.time())
                    atomic(state_path, state)
                    return state
                state.update(status="waiting", returncode=code, updated_at=time.time())
                atomic(state_path, state)
                child_state = read(home / "state.json") if (home / "state.json").exists() else {}
                error = child_state.get("error", "")
                temporary_resource = any(word in error for word in
                                         ("가용 RAM", "디스크 여유", "가용 메모리", "다른 작업 실행 중"))
                temporary_resource |= "기존 계산 작업" in error or "학습 대기" in error
                if error and not temporary_resource:
                    state.update(status="waiting_for_input_or_repair", error=error, updated_at=time.time())
                    atomic(state_path, state)
                    return state
                # Wait only for temporary resource availability / owned checkpoints.
                # Invalid code/data returns a non-75 error and stops for investigation.
                for _ in range(60):
                    if (home / "cancel-request.json").exists():
                        atomic(state_path, {**state, "status": "cancelled", "updated_at": time.time()})
                        return {"status": "cancelled", "checkpoint_preserved": True}
                    time.sleep(5)
        state.update(status="native_training_complete" if args.native_only else "clinical_training_complete" if args.clinical_only else "predictions_ready",
                     updated_at=time.time(), child_pid=None, scientific_goal_achieved=False,
                     next="local_native_evaluation" if not args.clinical_only else "native_clinical_evaluation")
        atomic(state_path, state)
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--clinical-only", action="store_true")
    parser.add_argument("--native-only", action="store_true")
    parser.add_argument("--wait-r2", action="store_true")
    parser.add_argument('--resource-execution', type=Path)
    parser.add_argument('--resource-execution-sha256')
    args = parser.parse_args()
    if args.clinical_only and args.native_only:
        raise ValueError("Clinical and native tasks must have separate experiment directories")
    print(json.dumps(run(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
