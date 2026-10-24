"""Independent v8 batch CLI. No production endpoints or generation API calls."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[name] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["audit", "prepare", "train-ner", "train-id", "train-relations", "clinical", "clinical-role", "dapt",
                                          "freeze", "predict", "evaluate", "evaluate-saved", "verify-export", "resume", "status", "probe", "smoke-id", "smoke-ner"])
    parser.add_argument("--home", type=Path)
    parser.add_argument("--corpus", type=Path)
    args = parser.parse_args()
    if args.home:
        os.environ["EVIDENCE_V8_HOME"] = str(args.home.resolve())
    from nutri_literature.evidence_v8.common import POLICY, OwnerBusy, atomic, gpu_inventory, home, owner, read
    if args.stage == "status":
        result = {"engine": POLICY["engine"], "stage": read(home()/"state.json") if (home()/"state.json").exists() else None,
                  "owner": read(home()/"owner.json") if (home()/"owner.json").exists() else None,
                  "gpu_ledger": read(home()/"gpu-ledger.json") if (home()/"gpu-ledger.json").exists() else None,
                  "predictions_ready": (home()/"predictions/manifest.json").exists(),
                  "evaluated": (home()/"evaluation.json").exists(), "default_engine": "heuristic-v1"}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    try:
        with owner():
            try:
                os.nice(10)
            except (AttributeError, PermissionError):
                pass
            state = {"stage": args.stage, "started": time.time(), "status": "running"}
            atomic(home()/"state.json", state)
            if args.stage == "probe":
                from nutri_literature.evidence_v8.common import gpu_setup
                _, gpu = gpu_setup()
                result = {"gpu": gpu, "policy": POLICY}
            elif args.stage == "smoke-id":
                from nutri_literature.evidence_v8.runner import fit, score, example_rows
                if (home()/"examples/id-normalization.json").exists():
                    rows = example_rows("normalization")[:12]
                    profile = "v8-crossfit"
                else:
                    from nutri_literature.evidence_v8.data import id_rows
                    rows = id_rows("normalization")[:12]
                    profile = "imported_r2_for_smoke_only"
                config = {"family": "crossencoder", "learning_rate": 1e-5, "epochs": 1,
                          "patience": 2, "accumulation": 4, "smoke_only": True}
                path = fit("id", rows, [], config)
                outputs = score("id", rows[:3], path)
                result = {"smoke_only": True, "training_mentions": len(rows), "scored_mentions": len(outputs),
                          "path": str(path), "profile": profile, "qualifies_as_model_evaluation": False}
            elif args.stage == "smoke-ner":
                from nutri_literature.evidence_v8.data import documents
                from nutri_literature.evidence_v8.ner import training_rows, predict_sources
                from nutri_literature.evidence_v8.learning import fit_neural
                from nutri_literature.evidence_v8.common import digest
                ds = documents("identity-train")[:3]
                path = home()/"smoke/ner"
                config = {"family": "two-bio-heads", "learning_rate": 2e-5, "epochs": 1, "patience": 2, "accumulation": 2, "smoke_only": True}
                fit_neural("ner", training_rows(ds[:2]), [], path, config, 20261023)
                atomic(path/"training-provenance.json", {"pmids": [str(d["pmid"]) for d in ds[:2]],
                       "text_hashes": [digest(d["text"]) for d in ds[:2]]}, True)
                output = predict_sources(ds[2:3], path)
                result = {"smoke_only": True, "training_papers": 2, "predicted_papers": len(output["predictions"]),
                          "path": str(path), "qualifies_as_model_evaluation": False}
            elif args.stage == "dapt":
                from nutri_literature.evidence_v8.auxiliary import dapt
                result = dapt(args.corpus)
            else:
                from nutri_literature.evidence_v8.runner import execute
                result = execute(args.stage)
            atomic(home()/"state.json", {**state, "status": "completed", "finished": time.time()})
            print(json.dumps(result, ensure_ascii=False))
            return 0
    except OwnerBusy as error:
        print(json.dumps({"status": "waiting", "error": str(error), "active_job_unchanged": True}, ensure_ascii=False))
        return 75
    except (FileNotFoundError, RuntimeError, InterruptedError) as error:
        atomic(home()/"state.json", {"stage": args.stage, "status": "waiting", "error": type(error).__name__+": "+str(error),
                                     "finished": time.time(), "completed": False})
        print(json.dumps({"status": "waiting", "error": str(error)}, ensure_ascii=False))
        return 75
    except Exception as error:
        atomic(home()/"state.json", {"stage": args.stage, "status": "failed", "error": type(error).__name__+": "+str(error),
                                     "finished": time.time(), "completed": False})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
