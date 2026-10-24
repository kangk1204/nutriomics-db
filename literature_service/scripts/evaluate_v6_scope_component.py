"""Measure the reused independent scope module; never claim full-engine coverage."""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import benchmarks as scoring, evaluation_v5, evaluation_v6
    from nutri_literature.db import digest, dumps, now, uid
    from nutri_literature.models_v5 import LearnedModels
    from nutri_literature.scope_v5 import predict
    from nutri_literature.settings import Settings
    from nutri_literature.text import sentence_spans
    from scripts.compare_v5_v6 import paired_rows

    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--baseline-home", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(args.home, ROOT / "config/topics.json")
    old_settings = Settings(args.baseline_home, settings.config)
    index = json.loads((settings.home / "public_benchmarks/v2-measurement-index.json").read_bytes())
    model = LearnedModels(args.models)
    model.load()
    scope_sha = model.manifest["files"]["scope.joblib"]
    scope_sources = {name: digest((ROOT / "nutri_literature" / name).read_bytes())
                     for name in ("scope_v5.py", "features_v5.py", "text.py")}
    results = []
    for level in ("abstract", "fulltext"):
        prepared = index["steps"]["prepare:development:bioscope:" + level]
        inputs, _, input_sha = scoring._load_inputs(settings, prepared["inputs_path"])
        if inputs["split"] != "development":
            raise ValueError("Only previously used scope development inputs are allowed")
        baseline = max((r for r in evaluation_v5.reports(old_settings)["items"]
                        if r["corpus"] == "bioscope" and r["source_level"] == level
                        and r["ablation"] == "context" and r["provider"] == "union"
                        and r["input_sha256"] == input_sha), key=lambda r: r["at"])
        if baseline["artifact"]["learned_models"]["files"]["scope.joblib"] != scope_sha:
            raise ValueError("The scope model differs from the authenticated v5 baseline")
        documents = sorted(inputs["documents"], key=lambda d: digest("v3-development-20261003:" + d["pmid"]))[
            :baseline["papers"]]
        baseline_location = next(p.parent for p in (old_settings.home / "experiments/v5").glob("*/report.json")
                                 if json.loads(p.read_bytes())["id"] == baseline["id"])
        directory = args.out / (level + "-" + digest(input_sha + scope_sha)[:24])
        directory.mkdir(parents=True, exist_ok=True)
        partials = {}
        for folder in (settings.home / "experiments/v6").glob("*"):
            path = folder / "identity.json"
            if not path.exists():
                continue
            identity = json.loads(path.read_bytes())
            if (identity.get("corpus") == "bioscope" and identity.get("input_sha256") == input_sha
                    and identity["artifact"]["learned_models"]["files"].get("scope.joblib") == scope_sha):
                for path in folder.glob("paper-*.json"):
                    pred = json.loads(path.read_bytes())
                    if pred["artifact_identity"] != identity["artifact"]["artifact_identity"]:
                        raise ValueError("Full-path partial prediction identity differs")
                    partials[pred["pmid"]] = pred
        matched = 0
        for document in documents:
            scope = predict(sentence_spans(document["text"], document["sections"]), model)
            if document["pmid"] in partials:
                old = partials[document["pmid"]]
                if old["source_hash"] != digest(document["text"]) or old["scopes"] != scope:
                    raise ValueError("Independent scope output differs from the actual v6 full path")
                matched += 1
            prediction = {"pmid": document["pmid"], "source_hash": digest(document["text"]),
                          "entities": [], "scopes": scope, "annotated_spans": [], "relations": [],
                          "evaluation_path": "independent_scope_component", "scope_model_sha256": scope_sha}
            path = directory / ("paper-" + document["pmid"] + ".json")
            if path.exists():
                if json.loads(path.read_bytes()) != prediction:
                    raise ValueError("Saved scope component prediction changed")
            else:
                scoring._write_immutable(path, prediction)
        _, _, gold_sha = scoring._load_gold(settings, prepared["gold_path"])
        candidate = evaluation_v6.rescore(directory, prepared["gold_path"], directory / "rescore-v6.json")
        old = evaluation_v6.rescore(baseline_location, prepared["gold_path"], directory / "rescore-v5.json")
        result = {"source_level": level, "papers": len(documents), "input_sha256": input_sha,
                  "gold_sha256": gold_sha, "baseline_id": baseline["id"],
                  "scope_model_sha256": scope_sha, "scope_source_hashes": scope_sources,
                  "full_path_predictions_matched": matched, "metrics": paired_rows(old, candidate),
                  "prediction_directory": str(directory)}
        results.append(result)
    report = {"id": uid(), "at": now(), "corpus": "bioscope", "engine": "heuristic-v6",
              "evaluation_path": "independent_scope_component", "full_engine_evaluation": False,
              "reused_v5_module": True, "comparisons": results, "sealed_test_opened": False,
              "nutrition_accuracy": "unmeasured", "default_engine": "heuristic-v1"}
    scoring._write_immutable(args.out / "scope-component-comparison.json", report)
    scoring._receipt(settings, "v6_scope_component_development", report["id"], report)
    print(dumps({"scope_component_evaluation_complete": True, "full_engine_evaluation": False,
                 "comparisons": [{k: row[k] for k in ("source_level", "papers", "full_path_predictions_matched", "metrics")}
                                 for row in results]}))


if __name__ == "__main__":
    production = Path("/home/keunsoo/projects/nutriomics-literature/runtime/locks")
    os.environ.setdefault("LITERATURE_NLP_LOCK", str(production / "nlp-cpu.lock"))
    main()
