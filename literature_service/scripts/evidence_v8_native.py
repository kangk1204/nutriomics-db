"""Separate native supervised experiments; no core engine or API changes."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[key] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "audit", "encode", "train-drugprot", "train-cg", "freeze",
                                          "predict", "evaluate", "status", "resume", "smoke"])
    parser.add_argument("--home", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--protection", type=Path)
    args = parser.parse_args()
    if args.home:
        os.environ["EVIDENCE_V8_NATIVE_HOME"] = str(args.home.resolve())
    from nutri_literature.evidence_v8_native.common import Busy, atomic, home, owner, read
    if args.stage == "status":
        result = {"state": read(home()/"state.json") if (home()/"state.json").exists() else None,
                  "ledger": read(home()/"gpu-ledger.json") if (home()/"gpu-ledger.json").exists() else None,
                  "trained": {task: (home()/"selected"/(task+".json")).exists() for task in ("drugprot", "cg")},
                  "automatic_entities": "not_measured", "core_model_transfer": False}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    try:
        with owner():
            try:
                os.nice(10)
            except (AttributeError, PermissionError):
                pass
            stage = args.stage
            if stage == "resume":
                if not (home()/"audit.json").exists():
                    stage = "prepare"
                elif not (home()/"features/manifest.json").exists():
                    stage = "encode"
                elif not (home()/"selected/drugprot.json").exists():
                    stage = "train-drugprot"
                elif not (home()/"selected/cg.json").exists():
                    stage = "train-cg"
                elif not (home()/"freeze.json").exists():
                    stage = "freeze"
                else:
                    print(json.dumps({"status": "training_frozen", "evaluation_inputs": "separate official source-only inputs required"}))
                    return 0
            atomic(home()/"state.json", {"stage": stage, "status": "running", "started": time.time()})
            if stage == "prepare":
                if not args.source:
                    raise FileNotFoundError("prepare --source 공식 auxiliary/train.json 필요")
                from nutri_literature.evidence_v8_native.data import prepare
                result = prepare(args.source, args.protection)
            elif stage == "audit":
                from nutri_literature.evidence_v8_native.data import load
                value = load()
                result = {"passed": True, "pairs": len(value["pairs"]), "labels": value["labels"]}
            elif stage == "encode":
                from nutri_literature.evidence_v8_native.runner import encode
                result = encode()
            elif stage in ("train-drugprot", "train-cg"):
                from nutri_literature.evidence_v8_native.runner import train
                result = train(stage.removeprefix("train-"))
            elif stage == "smoke":
                from nutri_literature.evidence_v8_native.data import load
                from nutri_literature.evidence_v8_native.learning import fit_neural, labels_for, neural_score
                value = load()
                rows = [r for r in value["pairs"] if r["task"] == "drugprot"][:8]
                labels = labels_for(value, "drugprot")
                path = fit_neural(rows[:6], rows[6:], "drugprot", labels,
                                  {"learning_rate": 1e-5, "epochs": 1, "smoke_only": True}, home()/"smoke/drugprot")
                predictions = neural_score(rows[6:], labels, path)
                result = {"smoke_only": True, "trained_pairs": 6, "scored_pairs": len(predictions), "performance_evaluation": False}
            else:
                from nutri_literature.evidence_v8_native import runner
                result = getattr(runner, stage)()
            atomic(home()/"state.json", {"stage": stage, "status": "completed", "finished": time.time()})
            print(json.dumps(result, ensure_ascii=False))
            return 0
    except Busy as error:
        print(json.dumps({"status": "waiting", "error": str(error), "existing_job_preserved": True}, ensure_ascii=False))
        return 75
    except (FileNotFoundError, RuntimeError, InterruptedError) as error:
        atomic(home()/"state.json", {"stage": args.stage, "status": "waiting", "error": str(error), "completed": False})
        print(json.dumps({"status": "waiting", "error": str(error)}, ensure_ascii=False))
        return 75
    except Exception as error:
        atomic(home()/"state.json", {"stage": args.stage, "status": "failed", "error": type(error).__name__+": "+str(error),
                                     "completed": False})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
