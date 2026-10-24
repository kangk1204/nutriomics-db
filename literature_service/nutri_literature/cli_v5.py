"""V5 train, development, immutable freeze/final and isolated batch commands."""

import argparse
import json
import os
from pathlib import Path
import time

from .db import connect, dumps, initialize, now
from .settings import Settings


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--models", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--clinical-manifest", type=Path, required=True)
    p = sub.add_parser("prepare-features")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("train")
    p.add_argument("--features", type=Path, required=True)
    p.add_argument("--audit", type=Path, required=True)
    p = sub.add_parser("train-clinical")
    p.add_argument("--sources", type=Path, required=True)
    p.add_argument("--benchmark-inputs", type=Path, required=True)
    p = sub.add_parser("train-scope")
    p.add_argument("--gold", type=Path, required=True)
    p = sub.add_parser("evaluate-development")
    p.add_argument(
        "--corpus", choices=["bioredirect", "bioscope", "ebmnlp", "evidenceoutcomes"], required=True
    )
    p.add_argument("--lexicon", type=Path, required=True)
    p.add_argument("--provider", choices=["union", "biomedbert", "bionlp13cg"], default="union")
    p.add_argument("--mode", choices=["end_to_end", "gold_entities"], default="end_to_end")
    p.add_argument("--ablation", choices=["entities", "relations", "context"], default="context")
    p.add_argument("--level", choices=["abstract", "fulltext"], default="abstract")
    p.add_argument("--limit", type=int, default=200)
    for name in ("process", "worker", "reprocess"):
        p = sub.add_parser(name)
        p.add_argument("--limit", type=int, default=10)
    for name in ("status", "observe", "compare"):
        sub.add_parser(name)
    p = sub.add_parser("freeze")
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("final-evaluate")
    p.add_argument("--spec", type=Path, required=True)
    p.add_argument("--freeze", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.home:
        settings = Settings(args.home, settings.config)
    if args.models:
        os.environ["LITERATURE_V5_MODEL_HOME"] = str(args.models)
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ.setdefault("LITERATURE_NLP_LOCK", str(settings.home / "locks/nlp-cpu.lock"))
    if hasattr(os, "nice"):
        os.nice(10)
    initialize(settings)
    from . import operations_v5 as operations

    home = operations.model_home(settings)
    if args.command in {"prepare", "prepare-features", "train", "train-clinical", "train-scope"}:
        from .locking import lock

        with lock(settings.home / "locks/training-v5.lock"), operations.processing_slot(settings):
            if args.command == "prepare":
                from .corpora_v5 import prepare

                result = prepare(settings.home, home.parent / "training-data", args.clinical_manifest)
            elif args.command == "prepare-features":
                from .training_v5 import prepare_features

                result = prepare_features(args.data, home, args.out)
            elif args.command == "train":
                from .training_v5 import train

                result = train(args.features, home / "learned", args.audit)
            elif args.command == "train-clinical":
                from .training_v4 import train

                result = train(args.sources, home / "clinical", args.benchmark_inputs)
            else:
                from .training_v5 import train_scope
                from .db import digest

                gold = json.loads(args.gold.read_bytes())
                reserved = [
                    d["pmid"]
                    for d in sorted(
                        gold["documents"], key=lambda d: digest("v3-development-20261003:" + d["pmid"])
                    )[:200]
                ]
                result = train_scope(args.gold, reserved, home / "scope.joblib")
                (home / "scope-manifest.json").write_text(dumps(result), encoding="utf-8")
    elif args.command == "evaluate-development":
        if args.limit < 1:
            parser.error("limit must be positive")
        from .evaluation_v5 import development

        with operations.processing_slot(settings):
            report = development(
                settings,
                args.corpus,
                args.lexicon,
                args.provider,
                args.mode,
                args.ablation,
                args.level,
                args.limit,
            )
        result = {
            k: report[k] for k in ("output_path", "metrics", "failure_counts", "papers", "provider", "mode")
        }
    elif args.command == "observe":
        result = operations.observe(settings)
    elif args.command == "status":
        result = operations.operation_status(settings)
    elif args.command == "compare":
        from .evaluation_v5 import reports, selection, compare

        saved = reports(settings)["items"]
        result = {"selection": selection(saved), "comparisons": compare(settings, saved), "reports": saved}
    elif args.command == "freeze":
        from .evaluation_v5 import freeze

        result = freeze(settings, args.audit, args.out)
    elif args.command == "final-evaluate":
        from .evaluation_v5 import final_evaluate

        result = final_evaluate(settings, args.spec, args.freeze)
    else:
        from .locking import lock
        from .dictionaries import load_dictionaries

        with lock(settings.home / "locks/worker-v5.lock"):
            values = operations.components(settings)
            dictionaries = load_dictionaries(settings, "heuristic-v2")
            if args.command == "reprocess":
                result = operations.register(settings, values, dictionaries, queue_all=True)
            else:
                with connect(settings) as con:
                    con.execute(
                        "UPDATE engine_queue SET status='pending',due_at=?,error='v5 exclusive worker restarted' WHERE engine_version='heuristic-v5' AND status='running'",
                        (now(),),
                    )
                    con.execute(
                        "UPDATE runs SET status='interrupted',finished_at=?,error='v5 exclusive worker restarted' WHERE kind='process_extract_heuristic-v5' AND status='running'",
                        (now(),),
                    )
                while True:
                    result = operations.process(settings, args.limit, values, dictionaries)
                    operations.observe(settings)
                    if args.command != "worker":
                        break
                    if result["processed"] or result["failed"]:
                        print(dumps(result), flush=True)
                    time.sleep(15 if result["processed"] else 300)
    print(dumps(result), flush=True)


def main():
    from .resources_v5 import memory_budget

    with memory_budget(8):
        _main()


if __name__ == "__main__":
    main()
