"""Evaluate the v4 scope component on preregistered BioScope fulltext development.

This is a component evaluation, not an end-to-end entity/relation evaluation.
It uses the same frozen scope function, sentence splitter and CPU parser as v4.
"""
import argparse
import json
import os
from pathlib import Path
import time

from nutri_literature import benchmarks as scoring
from nutri_literature.db import digest, dumps, now, uid
from nutri_literature.grammar_v3 import scopes
from nutri_literature.nlp_v2 import parser_for, parser_manifest, validate_tokens
from nutri_literature.operations_v4 import processing_slot
from nutri_literature.public_corpora import validate_span
from nutri_literature.registry_v4 import engine_rules_version
from nutri_literature.settings import Settings
from nutri_literature.text import sentence_spans


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--inputs", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    settings = Settings.from_env()
    inputs, receipt, input_sha = scoring._load_inputs(settings, args.inputs)
    if inputs["corpus"] != "bioscope" or inputs["split"] != "development":
        raise ValueError("Only preregistered BioScope development inputs are permitted")
    documents = inputs["documents"]
    if not documents or any(d["source_level"] != "fulltext" for d in documents):
        raise ValueError("A nonempty biological fulltext development partition is required")
    artifact_path = settings.home / "engine-artifacts/heuristic-v4.json"
    artifact = json.loads(artifact_path.read_bytes())
    rules = engine_rules_version("heuristic-v4")
    if artifact["rules_version"] != rules:
        raise ValueError("Current registered rules differ")
    experiment = {"input_sha256": input_sha, "artifact_sha256": digest(artifact_path.read_bytes()),
                  "rules_version": rules, "adapter_sha256": digest(Path(__file__).read_bytes()),
                  "evaluator_source_hashes": scoring.evaluator_source_hashes(),
                  "scope": "scope_component_only_fulltext_development"}
    directory = args.output / digest(dumps(experiment))
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.json"
    if report_path.exists():
        report = json.loads(report_path.read_bytes())
        if not scoring.verify_receipt(settings, "v4_scope_component_development", report):
            raise ValueError("Completed scope component receipt changed")
        print(dumps({"path": str(report_path), "metrics": report["metrics"]}), flush=True)
        return
    parser = parser_for({})
    predictions = {}
    started = time.monotonic()
    if os.name == "posix":
        os.nice(10)
    # Gold labels are opened only after every source-only prediction is persisted.
    for document in documents:
        path = directory / ("paper-" + document["pmid"] + ".json")
        with processing_slot(settings):
            predicted = []
            for sentence in sentence_spans(document["text"], document["sections"]):
                predicted.extend(scopes(sentence, validate_tokens(parser.parse(sentence), sentence)))
            for value in predicted:
                validate_span(document["text"], value)
            saved = {"pmid": document["pmid"], "source_hash": document["source_hash"],
                     "scopes": predicted, "experiment": experiment}
            if path.exists():
                if json.loads(path.read_bytes()) != saved:
                    raise ValueError("Resumed source-only prediction changed")
            else:
                scoring._write_immutable(path, saved)
            predictions[document["pmid"]] = saved
        print(dumps({"completed": len(predictions), "papers": len(documents)}), flush=True)
    parser_record = parser_manifest(parser)
    if (not parser_record.get("available") or parser_record.get("mode") != "production"
            or parser_record.get("model_sha256") != artifact.get("model_sha256")
            or engine_rules_version("heuristic-v4") != rules):
        raise ValueError("Parser/rules changed during evaluation")
    gold, gold_receipt, gold_sha = scoring._load_gold(settings, args.inputs.with_name("gold.json"))
    if gold_receipt != receipt or {d["pmid"] for d in gold["documents"]} != set(predictions):
        raise ValueError("Gold cohort differs from prediction cohort")
    per_paper = [dict(pmid=d["pmid"], source_level="fulltext",
                      **scoring._paper_counts(d, predictions[d["pmid"]], {}, "end_to_end",
                                              annotated_tasks=["negation_scope", "speculation_scope"]))
                 for d in gold["documents"]]
    metrics = {key: scoring._aggregate(per_paper, key) for key in
               ("scopes", "scopes:negation", "scopes:speculation", "scopes_exact")}
    report = {"id": uid(), "at": now(), "engine": "heuristic-v4", "experiment": experiment,
              "source_level": "fulltext", "split": "development", "papers": len(documents),
              "gold_sha256": gold_sha, "parser": parser_record, "metrics": metrics,
              "per_paper": per_paper, "provenance_valid": True,
              "intervals": {key: scoring._paper_intervals(per_paper, key) for key in metrics},
              "prediction_sha256": {p.name: digest(p.read_bytes()) for p in directory.glob("paper-*.json")},
              "elapsed_seconds": time.monotonic()-started, "pretraining_overlap": "unknown",
              "end_to_end_entity_relation_evaluation": False, "final_test": False,
              "nutrition_accuracy": "unmeasured", "promotion_eligible": False}
    scoring._write_immutable(report_path, report)
    scoring._receipt(settings, "v4_scope_component_development", report["id"], report)
    (args.output / "bioscope-fulltext-development.json").write_text(dumps(report), encoding="utf-8")
    print(dumps({"path": str(report_path), "metrics": metrics}), flush=True)


if __name__ == "__main__":
    main()
