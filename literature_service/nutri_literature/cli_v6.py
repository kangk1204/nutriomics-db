"""Auditable v6 CPU preparation, fitting, evaluation and isolated batch jobs."""

import argparse
import os
from pathlib import Path
import time

from .db import dumps, initialize, connect, now
from .settings import Settings


def _main():
    for name in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        os.environ[name] = "1"
    if hasattr(os, "nice"):
        os.nice(10)
    parser = argparse.ArgumentParser(description="heuristic-v6 CPU candidate beta")
    parser.add_argument("--home", type=Path, default=Settings.from_env().home)
    parser.add_argument("--models", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    mesh = sub.add_parser("build-mesh")
    mesh.add_argument("--out", type=Path, required=True)
    release = sub.add_parser("prepare-relation-train")
    release.add_argument("--release", type=Path, required=True)
    release.add_argument("--out", type=Path, required=True)
    corpora = sub.add_parser("download-train-corpora")
    corpora.add_argument("--out", type=Path, required=True)
    ei = sub.add_parser("prepare-inference-train")
    ei.add_argument("--release", type=Path, required=True)
    ei.add_argument("--out", type=Path, required=True)
    ei.add_argument("--id-map", type=Path)
    ei_map = sub.add_parser("prepare-inference-idmap")
    ei_map.add_argument("--release", type=Path, required=True)
    ei_map.add_argument("--out", type=Path, required=True)
    outcome = sub.add_parser("prepare-outcome-train")
    outcome.add_argument("--release", type=Path, required=True)
    outcome.add_argument("--out", type=Path, required=True)
    ei_dev = sub.add_parser("prepare-inference-development")
    ei_dev.add_argument("--release", type=Path, required=True)
    ei_dev.add_argument("--out", type=Path, required=True)
    ei_dev.add_argument("--id-map", type=Path)
    clinical_eval = sub.add_parser("evaluate-clinical-development")
    clinical_eval.add_argument("--spec", type=Path, required=True)
    clinical_eval.add_argument("--audit", type=Path, nargs="+", required=True)
    clinical_eval.add_argument("--provider", choices=("union", "biomedbert", "bionlp13cg"), default="union")
    clinical = sub.add_parser("train-clinical-head")
    clinical.add_argument("--data", type=Path, required=True)
    clinical.add_argument("--kind", choices=("outcome", "evidence"), required=True)
    clinical.add_argument("--audit", type=Path, required=True)
    clinical.add_argument("--out", type=Path, required=True)
    base = sub.add_parser("train-clinical-base")
    base.add_argument("--sources", type=Path, required=True)
    base.add_argument("--benchmarks", type=Path, required=True)
    base.add_argument("--audit", type=Path, required=True)
    base.add_argument("--out", type=Path, required=True)
    audit = sub.add_parser("audit-training")
    audit.add_argument("--train", type=Path, required=True)
    audit.add_argument("--protected", type=Path, nargs="+", default=[])
    audit.add_argument("--prior-audit", type=Path)
    audit.add_argument("--out", type=Path, required=True)
    merge = sub.add_parser("merge-training")
    merge.add_argument("--sources", type=Path, nargs="+", required=True)
    merge.add_argument("--out", type=Path, required=True)
    prepare = sub.add_parser("prepare-features")
    prepare.add_argument("--data", type=Path, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--audit", type=Path, required=True)
    train = sub.add_parser("train")
    train.add_argument("--features", type=Path, required=True)
    train.add_argument("--audit", type=Path, required=True)
    train.add_argument("--out", type=Path, required=True)
    for name in ("process", "worker", "reprocess"):
        command = sub.add_parser(name)
        command.add_argument("--limit", type=int, default=10)
        command.add_argument("--provider", choices=("union", "biomedbert", "bionlp13cg"), default="union")
    for name in ("status", "observe", "compare", "development-gates"):
        sub.add_parser(name)
    evaluate = sub.add_parser("evaluate-development")
    evaluate.add_argument(
        "--corpus", choices=("bioredirect", "bioscope", "ebmnlp", "evidenceoutcomes"), required=True
    )
    evaluate.add_argument("--lexicon", type=Path, required=True)
    evaluate.add_argument("--provider", choices=("union", "biomedbert", "bionlp13cg"), default="union")
    evaluate.add_argument("--mode", choices=("end_to_end", "gold_entities"), default="end_to_end")
    evaluate.add_argument("--level", choices=("abstract", "fulltext"), default="abstract")
    evaluate.add_argument("--limit", type=int, default=200)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--audit", type=Path, required=True)
    freeze.add_argument("--out", type=Path, required=True)
    freeze.add_argument("--holdout-audit", type=Path)
    final = sub.add_parser("final-evaluate")
    final.add_argument("--spec", type=Path, required=True)
    final.add_argument("--freeze", type=Path, required=True)
    rescore = sub.add_parser("rescore")
    rescore.add_argument("--predictions", type=Path, required=True)
    rescore.add_argument("--gold", type=Path, required=True)
    rescore.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(args.home, Settings.from_env().config)
    initialize(settings)
    if args.models:
        os.environ["LITERATURE_V6_MODEL_HOME"] = str(args.models)
    from . import operations_v6 as ops
    from . import evaluation_v6 as evaluation

    models = args.models or ops.model_home(settings)
    if args.command == "build-mesh":
        from .chemical_v6 import build_snapshot

        result = build_snapshot(args.out)
        if args.out.name == "mesh":
            pointer = args.out.parent / "mesh-snapshot.json"
            from .db import digest

            temporary = pointer.with_suffix(".tmp")
            temporary.write_text(
                dumps(
                    {
                        "filename": Path(result["path"]).name,
                        "sha256": result["sha256"],
                        "records_sha256": result["records_sha256"],
                    }
                ),
                encoding="utf-8",
            )
            temporary.replace(pointer)
            result["pointer_sha256"] = digest(pointer.read_bytes())
    elif args.command == "prepare-relation-train":
        from .corpora_v6 import official_relation_train

        result = official_relation_train(args.release, args.out)
    elif args.command == "download-train-corpora":
        from .corpora_v6 import download_train_corpora

        result = download_train_corpora(args.out)
    elif args.command == "prepare-inference-train":
        from .corpora_v6 import inference_train

        result = inference_train(args.release, args.out, args.id_map)
    elif args.command == "prepare-inference-idmap":
        from .corpora_v6 import inference_idmap

        result = inference_idmap(args.release, args.out)
    elif args.command == "prepare-outcome-train":
        from .corpora_v6 import outcome_train

        result = outcome_train(args.release, args.out)
    elif args.command == "prepare-inference-development":
        from .corpora_v6 import inference_development

        result = inference_development(args.release, args.out, args.id_map)
    elif args.command == "evaluate-clinical-development":
        from .clinical_evaluation_v6 import development

        with ops.processing_slot(settings):
            result = development(settings, args.spec, args.provider, args.audit)
    elif args.command == "train-clinical-head":
        from .clinical_training_v6 import train

        with ops.processing_slot(settings):
            result = train(
                args.data, args.kind, models, settings.home / "embedding-cache-v6", args.audit, args.out
            )
    elif args.command == "train-clinical-base":
        from .audit_v6 import train_clinical_base

        with ops.processing_slot(settings):
            result = train_clinical_base(args.sources, args.out, args.benchmarks, args.audit)
    elif args.command == "audit-training":
        from .corpora_v6 import audit_training

        if not args.protected and args.prior_audit is None:
            raise ValueError("At least one protected cohort or prior protection audit is required")
        result = audit_training(args.train, args.protected, args.out, prior_audit=args.prior_audit)
    elif args.command == "merge-training":
        from .corpora_v6 import merge_training

        result = merge_training(args.sources, args.out)
    elif args.command == "prepare-features":
        from .training_v6 import prepare_features

        with ops.processing_slot(settings):
            result = prepare_features(
                args.data, models, settings.home / "embedding-cache-v6", args.out, args.audit
            )
    elif args.command == "train":
        from .training_v6 import train

        with ops.processing_slot(settings):
            result = train(args.features, args.out, args.audit)
    elif args.command == "evaluate-development":
        with ops.processing_slot(settings):
            result = evaluation.development(
                settings,
                args.corpus,
                args.lexicon,
                args.provider,
                args.mode,
                level=args.level,
                limit=args.limit,
            )
    elif args.command == "freeze":
        result = evaluation.freeze(settings, args.audit, args.out, args.holdout_audit)
    elif args.command == "final-evaluate":
        with ops.processing_slot(settings):
            result = evaluation.final_evaluate(settings, args.spec, args.freeze)
    elif args.command == "rescore":
        result = evaluation.rescore(args.predictions, args.gold, args.out)
    elif args.command == "compare":
        result = evaluation.compare(settings, evaluation.reports(settings)["items"])
    elif args.command == "development-gates":
        result = evaluation.development_gates(settings)
    elif args.command == "observe":
        result = ops.observe(settings)
    elif args.command == "status":
        result = ops.operation_status(settings)
    else:
        from .locking import lock
        from .dictionaries import load_dictionaries

        with lock(settings.home / "locks" / "worker-v6.lock"):
            values = ops.components(settings, args.provider)
            dictionaries = load_dictionaries(settings, "heuristic-v2")
            if args.command in {"worker", "reprocess"}:
                with connect(settings) as con:
                    con.execute(
                        "UPDATE engine_queue SET status='pending',due_at=? WHERE engine_version='heuristic-v6' AND status='running'",
                        (now(),),
                    )
                ops.register(settings, values, dictionaries, queue_all=args.command == "reprocess")
            while True:
                result = ops.process(settings, args.limit, values, dictionaries)
                ops.observe(settings)
                if args.command != "worker":
                    break
                print(dumps(result), flush=True)
                time.sleep(15 if result["processed"] else 60)
    if args.command == "download-train-corpora":
        result = {k: v for k, v in result.items() if k != "sources"} | {
            "source_receipts": len(result["sources"])
        }
    elif args.command in {"train", "train-clinical-base", "train-clinical-head", "prepare-features"}:
        from .db import digest

        result = {
            "command": args.command,
            "manifest": str(args.out / "manifest.json"),
            "manifest_sha256": digest((args.out / "manifest.json").read_bytes()),
            "schema": result.get("schema"),
            "training_split": result.get("training_split", result.get("split")),
            "training_articles": len(result["training_pmids"])
            if isinstance(result.get("training_pmids"), list)
            else {k: len(v) for k, v in result.get("training_pmids", {}).items()}
            if isinstance(result.get("training_pmids"), dict)
            else result.get("papers"),
        }
    elif args.command == "audit-training":
        result = {
            "audit": str(args.out / "audit.json"),
            "passed": result["passed"],
            "training_articles": len(result["training_pmids"]),
            "protected_pmids": len(result["protected_pmids"]),
            "excluded_articles": len(result["excluded_pmids"]),
            "protected_overlap": result["protected_overlap"],
        }
    print(dumps(result), flush=True)


def main():
    from .resources_v6 import memory_budget

    with memory_budget(8):
        _main()


if __name__ == "__main__":
    main()
