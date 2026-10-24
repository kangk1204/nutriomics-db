"""Evaluate an unchanged beta after its relation fit; never open sealed tests."""

from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Development runner is restricted to the isolated v6 checkout")
    sys.path.insert(0, str(ROOT))
    from nutri_literature.extraction_v6 import source_manifest

    parser = argparse.ArgumentParser()
    parser.add_argument("--recovered-auto", type=Path)
    parser.add_argument("--models", type=Path, default=ROOT / "runtime/v6/models")
    parser.add_argument("--core-only", action="store_true")
    parser.add_argument("--clinical-first", action="store_true")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--log-home", type=Path)
    parser.add_argument("--compact-metadata", action="store_true")
    args = parser.parse_args()

    home = ROOT / "runtime"
    expected = json.loads(Path("/home/keunsoo/projects/delivery-v6.json").read_bytes())["rules_version"]
    state = args.state_file or home / "v6/development-state.json"
    logs = args.log_home or home / "logs/v6-development"
    logs.mkdir(parents=True, exist_ok=True)

    def save(status, **fields):
        record = {
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
            "rules_version": expected,
            "sealed_final_opened": False,
            "default_engine": "heuristic-v1",
            **fields,
        }
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(state)
        print(json.dumps(record), flush=True)

    def check_source():
        if source_manifest()[1] != expected:
            save("stopped_source_changed")
            raise ValueError(
                "Scientific source changed; this development job must be restarted for the new artifact"
            )

    save("waiting_for_relation_model")
    deadline = time.monotonic() + 24 * 3600
    while not (args.models / "learned-v6/manifest.json").exists():
        check_source()
        if (home / "v6/training-state.json").exists():
            training = json.loads((home / "v6/training-state.json").read_bytes())
            if training.get("status") == "failed":
                save("blocked_by_training_failure", training=training)
                return 1
        if time.monotonic() > deadline:
            save("relation_model_wait_timed_out")
            return 1
        time.sleep(30)
    models = args.models
    pointer = json.loads((models / "mesh-snapshot.json").read_bytes())
    lexicon = models / "mesh" / pointer["filename"]
    env = {
        **os.environ,
        "LITERATURE_NLP_LOCK": "/home/keunsoo/projects/nutriomics-literature/runtime/locks/nlp-cpu.lock",
        "LITERATURE_PROCESSING_LOCK_HOME": "/home/keunsoo/projects/nutriomics-literature/runtime/locks",
    }
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[name] = "1"
    commands = [
        ("bioredirect", "end_to_end", "abstract", 100),
        ("bioredirect", "gold_entities", "abstract", 100),
        ("evidenceoutcomes", "end_to_end", "abstract", 200),
        ("ebmnlp", "end_to_end", "abstract", 200),
        ("bioscope", "end_to_end", "abstract", 200),
        ("bioscope", "end_to_end", "fulltext", 200),
    ]
    if args.core_only:
        commands = [command for command in commands if command[0] != "bioscope"]
    if args.clinical_first:
        commands.sort(key=lambda command: 0 if command[0] in {"evidenceoutcomes", "ebmnlp"} else 1)
    if args.recovered_auto:
        from nutri_literature.benchmarks import verify_receipt
        from nutri_literature.db import digest
        from nutri_literature.settings import Settings

        recovered = json.loads(args.recovered_auto.read_bytes())
        if (
            not verify_receipt(
                Settings(home, ROOT / "config/topics.json"), "v6_recovered_development_comparison", recovered
            )
            or recovered["mode"] != "end_to_end"
            or recovered["papers"] != 100
            or recovered["prediction_artifact"]["v6_models"]["manifest_sha256"]
            != digest((models / "learned-v6/manifest.json").read_bytes())
        ):
            raise ValueError("Recovered automatic cohort/model receipt differs")
        commands = commands[1:]
    for corpus, mode, level, limit in commands:
        check_source()
        save("running", corpus=corpus, mode=mode, level=level)
        name = corpus + "-" + mode + "-" + level
        entry = (
            [str(ROOT / "scripts/cli_v6_compact.py")]
            if args.compact_metadata
            else ["-m", "nutri_literature.cli_v6"]
        )
        command = [
            str(ROOT / ".venv/bin/python"),
            "-u",
            *entry,
            "--home",
            str(home),
            "--models",
            str(models),
            "evaluate-development",
            "--corpus",
            corpus,
            "--mode",
            mode,
            "--level",
            level,
            "--limit",
            str(limit),
            "--lexicon",
            str(lexicon),
            "--provider",
            "union",
        ]
        with (logs / (name + ".log")).open("a", encoding="utf-8") as stream:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            save("failed", corpus=corpus, mode=mode, level=level, returncode=result.returncode)
            return 1
    save(
        "completed",
        interpretation="Development beta only; clinical heads, sealed tests and real operation remain separate",
        recovered_auto_source=str(args.recovered_auto) if args.recovered_auto else None,
    )
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    from nutri_literature.locking import lock

    with lock(ROOT / "runtime/locks/v6-development-runner.lock"):
        sys.exit(main())
