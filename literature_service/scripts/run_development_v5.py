"""Resume immutable development runs; every model uses the same held-out papers."""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--lexicon", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    os.environ["LITERATURE_V5_MODEL_HOME"] = str(args.models)
    os.environ["LITERATURE_NLP_LOCK"] = str(args.home / "locks/nlp-cpu.lock")
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    if hasattr(os, "nice"):
        os.nice(10)
    from nutri_literature.settings import Settings
    from nutri_literature import operations_v5, evaluation_v5
    from nutri_literature.candidates_v5 import UnionCandidates
    from nutri_literature.models_v5 import LearnedModels
    from nutri_literature.resources_v5 import memory_budget

    stage = Path(__file__).resolve().parents[1]
    settings = Settings(args.home, stage / "config/topics.json")
    with memory_budget(8):
        base = operations_v5.components(settings, "union")
        cache = {"union": base}

        def components(settings, provider="union"):
            if provider not in cache:
                detector = UnionCandidates(args.models, provider)
                # Reuse the loaded CPU models; a second copy exceeds the AS budget.
                detector.providers = [
                    p
                    for p in base["_candidate_provider"].providers
                    if p.manifest["gene_provider"] == provider
                ]
                if len(detector.providers) != 1:
                    raise ValueError("Missing shared candidate provider: " + provider)
                detector.load()
                learned = LearnedModels(
                    Path(os.environ.get("LITERATURE_V5_LEARNED_HOME", str(args.models / "learned"))),
                    os.environ.get("LITERATURE_V5_RELATION_CONFIGURATION"),
                    float(os.environ["LITERATURE_V5_RELATION_THRESHOLD"])
                    if "LITERATURE_V5_RELATION_THRESHOLD" in os.environ
                    else None,
                    span_provider=provider,
                )
                learned.load()
                cache[provider] = {**base, "_candidate_provider": detector, "_learned_models": learned}
            return cache[provider]

        operations_v5.components = components
        jobs = [
            ("bioredirect", "union", "gold_entities", "context", "abstract", 100),
            ("bioredirect", "union", "end_to_end", "context", "abstract", 100),
            ("bioredirect", "biomedbert", "end_to_end", "context", "abstract", 100),
            ("bioredirect", "bionlp13cg", "end_to_end", "context", "abstract", 100),
            ("bioredirect", "union", "end_to_end", "entities", "abstract", 100),
            ("bioredirect", "union", "end_to_end", "relations", "abstract", 100),
            ("bioscope", "union", "end_to_end", "context", "abstract", 200),
            ("bioscope", "union", "end_to_end", "context", "fulltext", 7),
            ("evidenceoutcomes", "union", "end_to_end", "context", "abstract", 200),
            ("ebmnlp", "union", "end_to_end", "context", "abstract", 200),
        ]
        results = []
        for corpus, provider, mode, ablation, level, limit in jobs:
            for attempt in range(3):
                try:
                    with operations_v5.processing_slot(settings):
                        report = evaluation_v5.development(
                            settings, corpus, args.lexicon, provider, mode, ablation, level, limit
                        )
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).lower() or attempt == 2:
                        raise
                    print(
                        json.dumps(
                            {"stage": "resume_after_database_lock", "corpus": corpus, "attempt": attempt + 1}
                        ),
                        flush=True,
                    )
                    time.sleep(1)
            result = {
                k: report[k]
                for k in (
                    "id",
                    "corpus",
                    "provider",
                    "mode",
                    "ablation",
                    "source_level",
                    "papers",
                    "metrics",
                    "output_path",
                )
            }
            results.append(result)
            print(json.dumps({"stage": "experiment_complete", **result}), flush=True)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps({"experiments": results, "complete": False}, indent=2))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        current = [
            r for r in evaluation_v5.reports(settings)["items"] if r["id"] in {v["id"] for v in results}
        ]
        args.out.write_text(
            json.dumps(
                {
                    "experiments": results,
                    "complete": True,
                    "selection": evaluation_v5.selection(current),
                    "comparisons": evaluation_v5.compare(settings, current),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
