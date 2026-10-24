"""Fit a newly protected clinical artifact, then run development evaluations only."""

from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.extraction_v6 import source_manifest

    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Only the isolated Linux v6 checkout may run this pipeline")
    runtime = ROOT / "runtime"
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, default=runtime / "v6/evidence-preparation.json")
    parser.add_argument("--skip-development", action="store_true",
                        help="Run clinical development here; run the six general comparisons on another authorized host")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_bytes())
    models = Path(plan["models"])
    relation = Path(plan["relation"])
    expected = source_manifest()[1]
    state = runtime / "v6/evidence-pipeline-state.json"
    logs = runtime / "logs/v6-evidence-pipeline"
    logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "LITERATURE_V6_MODEL_HOME": str(models),
           "LITERATURE_PROCESSING_LOCK_HOME": str(runtime / "locks"),
           "LITERATURE_NLP_LOCK": str(runtime / "locks/nlp-cpu.lock")}
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[name] = "1"
    prefix = [sys.executable, "-u", "-m", "nutri_literature.cli_v6", "--home", str(runtime),
              "--models", str(models)]
    stages = [
        ("features", Path(plan["features"]) / "manifest.json",
         ["prepare-features", "--data", str(relation / "relation-train.json"),
          "--audit", str(relation / "audit.json"), "--out", plan["features"]]),
        ("relation", models / "learned-v6/manifest.json",
         ["train", "--features", plan["features"], "--audit", str(relation / "audit.json"),
          "--out", str(models / "learned-v6")]),
        ("clinical_base", models / "clinical/manifest.json",
         ["train-clinical-base", "--sources", plan["sources"], "--audit", str(relation / "audit.json"),
          "--benchmarks", str(runtime / "public_benchmarks"), "--out", str(models / "clinical")]),
    ]
    for kind in ("outcome", "evidence"):
        directory = Path(plan[kind])
        stages.append((kind, models / ("clinical-v6-" + kind) / "manifest.json",
                       ["train-clinical-head", "--kind", kind,
                        "--data", str(directory / "relation-train.json"),
                        "--audit", str(directory / "audit.json"),
                        "--out", str(models / ("clinical-v6-" + kind))]))

    def save(stage, status):
        value = {"stage": stage, "status": status, "rules_version": expected,
                 "at": datetime.now(timezone.utc).isoformat(), "default_engine": "heuristic-v1",
                 "sealed_final_opened": False}
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(state)
        print(json.dumps(value), flush=True)

    def execute(stage, args):
        if source_manifest()[1] != expected:
            save(stage, "stopped_source_changed")
            raise ValueError("Scientific source changed; restart for the new artifact")
        save(stage, "running")
        with (logs / (stage + ".log")).open("ab") as stream:
            result = subprocess.run(prefix + args, cwd=ROOT, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT)
        if result.returncode:
            save(stage, "failed")
            return False
        save(stage, "completed")
        return True

    for stage, receipt, stage_args in stages:
        if receipt.exists():
            save(stage, "already_present")
            continue
        if stage == "outcome":
            launch = runtime / "v6/clinical-outcome-launch.json"
            deadline = time.monotonic() + 12 * 3600
            while launch.exists():
                previous = json.loads(launch.read_bytes())
                cmd = Path("/proc") / str(previous["pid"]) / "cmdline"
                if not cmd.exists() or b"train-clinical-head" not in cmd.read_bytes():
                    break
                save(stage, "waiting_for_other_outcome_fit")
                if time.monotonic() > deadline:
                    save(stage, "other_fit_wait_timed_out")
                    return 1
                time.sleep(30)
        if not execute(stage, stage_args) or not receipt.exists():
            return 1
    pointer = json.loads((models / "mesh-snapshot.json").read_bytes())
    configurations = (("bioredirect", "end_to_end", "abstract", 100),
                                     ("bioredirect", "gold_entities", "abstract", 100),
                                     ("evidenceoutcomes", "end_to_end", "abstract", 200),
                                     ("ebmnlp", "end_to_end", "abstract", 200),
                                     ("bioscope", "end_to_end", "abstract", 200),
                                     ("bioscope", "end_to_end", "fulltext", 200))
    for corpus, mode, level, limit in (() if args.skip_development else configurations):
        if not execute("development-" + corpus + "-" + mode + "-" + level,
                       ["evaluate-development", "--corpus", corpus, "--mode", mode,
                        "--level", level, "--limit", str(limit),
                        "--lexicon", str(models / "mesh" / pointer["filename"]), "--provider", "union"]):
            return 1
    if not execute("clinical-development", ["evaluate-clinical-development", "--spec", plan["clinical_spec"],
                                            "--audit", str(relation / "audit.json"),
                                            str(Path(plan["evidence"]) / "audit.json"), "--provider", "union"]):
        return 1
    save("pipeline", "completed_development_only")
    return 0


if __name__ == "__main__":
    from nutri_literature.locking import lock

    with lock(ROOT / "runtime/locks/v6-evidence-pipeline.lock"):
        sys.exit(main())
