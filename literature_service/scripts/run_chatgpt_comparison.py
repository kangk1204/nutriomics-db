"""Run both authorized models with resumable100-paper checkpoints."""
import json
import os
from pathlib import Path
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.chatgpt_auth import ChatGPTAuth
    from nutri_literature.chatgpt_client import available_models
    from nutri_literature.chatgpt_prefetch import PrefetchedComparison
    from nutri_literature.chatgpt_evaluation import prepare_experiment, run_experiment, _durable
    from nutri_literature.chatgpt_rescore import score_corrected_experiment
    from nutri_literature.contract_v7 import sha
    home, outputs = ROOT / "runtime/chatgpt-inputs", ROOT / "runtime/chatgpt-comparison"
    outputs.mkdir(parents=True, exist_ok=True)
    auth = ChatGPTAuth()
    account = auth.status()["active_registration_id"]
    catalog = {row["slug"] for row in available_models(auth, account)}
    planned = [("gpt-6.1-sol", "medium"), ("gpt-6-luna", "max")]
    if not {row[0] for row in planned} <= catalog:
        raise RuntimeError("Requested models are unavailable in the connected account")
    state = outputs / "batch-state.json"
    write_lock = threading.Lock()
    states = {}

    def save(value):
        value = {"pid": os.getpid(), "at_unix": time.time(), **value}
        with write_lock:
            if value.get("model"):
                states[value["model"]] = value
            temporary = state.with_suffix(".tmp")
            temporary.write_text(json.dumps({**value, "models_state": states}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(state)
            print(json.dumps(value, ensure_ascii=False), flush=True)

    def run_model(model, effort):
        output = outputs / (model + "-" + effort + "-" + sha(model + ":" + effort)[:8])
        save({"status": "running", "model": model, "effort": effort, "output": str(output)})
        prepare_experiment(home, home / "baseline", output, model,
            dictionary_path=home / "official-id-dictionary.json.gz", reasoning_effort=effort)
        _durable(output / "client-transport.json", {
            "integration": "official-local-personal-sign-in-with-chatgpt",
            "account_profile_sha256": sha(account),
            "source_sha256": {name: sha((ROOT / "nutri_literature" / name).read_bytes()) for name in
                ("chatgpt_client.py", "chatgpt_auth.py")},
            "requirements_sha256": sha((ROOT / "requirements-chatgpt.txt").read_bytes())})
        _durable(output / "network-plan.json", {"network_requests_per_model": 4, "model_workers": 2,
                  "cpu_nlp_training_jobs_started": 0,
                  "source_sha256": sha((ROOT / "nutri_literature/chatgpt_prefetch.py").read_bytes())})
        with PrefetchedComparison(auth, output, registration_id=account, workers=4) as inference:
            result = run_experiment(output, inference)
        if result["completed_papers"] != 100:
            save({"status": "attention_required", "model": model, "effort": effort, "result": result})
            return 2
        report = score_corrected_experiment(output, home)
        save({"status": "model_complete", "model": model, "effort": effort, "metrics": report["metrics"]})
        return 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_model, *pair) for pair in planned]
        codes = [future.result() for future in futures]
    if any(codes):
        save({"status": "attention_required"})
        return 2
    save({"status": "complete", "models": planned, "default_engine": "heuristic-v1"})
    return 0


if __name__ == "__main__":
    from nutri_literature.chatgpt_batch import batch_lock
    with batch_lock(ROOT / "runtime/chatgpt-comparison"):
        raise SystemExit(main())
