"""Evaluate the clinical observation DAG without unrelated relation bag inference."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def observations_only(document, dictionaries, metadata):
    from nutri_literature.candidates_v6 import collect
    from nutri_literature.clinical_v6 import observations
    from nutri_literature.nlp_v2 import parser_for, validate_tokens
    from nutri_literature.text import sentence_spans

    text, sections = document["text"], document["sections"]
    sentences = sentence_spans(text, sections)
    mentions, _, _, _ = collect(text, sentences, dictionaries, metadata)
    # V4's intervening graph extraction copies these inputs and does not
    # change the clinical predictions, original mentions or observation DAG.
    annotated = metadata["_clinical_models"].predict(sentences)
    parsing = parser_for(metadata)
    records, _ = observations(
        text,
        annotated,
        mentions,
        metadata,
        lambda sentence: validate_tokens(parsing.parse(sentence), sentence),
        metadata["_encoder"],
        metadata["_v6_models"].heads.get("outcome"),
        metadata["_v6_models"].heads.get("evidence"),
    )
    return records


def main():
    from nutri_literature import benchmarks as scoring
    from nutri_literature.audit_v6 import upstream_clinical
    from nutri_literature.clinical_evaluation_v6 import (
        automatic_comparisons,
        evidence_counts,
        provided_comparisons,
    )
    from nutri_literature.clinical_identity_v6 import auditable_identity
    from nutri_literature.db import digest, dumps, now, uid
    from nutri_literature.dictionaries import load_dictionaries
    from nutri_literature.operations_v6 import components, identity, processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.settings import Settings
    from nutri_literature.training_v4 import text_key

    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--audit", type=Path, nargs="+")
    parser.add_argument(
        "--verify-saved", type=Path, help="Verify an actual full-path prediction from this artifact"
    )
    parser.add_argument("--verify-inputs", type=Path)
    args = parser.parse_args()
    os.environ["LITERATURE_V6_MODEL_HOME"] = str(args.models)
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    with memory_budget(), processing_slot(settings):
        values = components(settings, "union")
        dictionaries = load_dictionaries(settings, "heuristic-v2")
        artifact = identity(settings, values, dictionaries)
        values["_artifact_identity"] = artifact["artifact_identity"]
        if args.verify_saved:
            pred = json.loads(args.verify_saved.read_bytes())
            source = json.loads(args.verify_inputs.read_bytes())
            document = next(d for d in source["documents"] if d["pmid"] == pred["pmid"])
            if (
                pred["source_hash"] != digest(document["text"])
                or pred["artifact_identity"] != artifact["artifact_identity"]
            ):
                raise ValueError("Full-path equivalence requires the exact same source/model artifact")
            observed = observations_only(document, dictionaries, values)
            if dumps(observed) != dumps(pred["clinical_observations"]):
                raise ValueError("Clinical component differs from actual full-path observations")
            record = {
                "at": now(),
                "pmid": pred["pmid"],
                "observations": len(observed),
                "source_hash": pred["source_hash"],
                "artifact": artifact,
                "component_source_sha256": digest(Path(__file__).read_bytes()),
                "complete_observations_identical": True,
            }
            path = (
                settings.home / "v6/validation" / ("clinical-component-equivalence-" + pred["pmid"] + ".json")
            )
            scoring._write_immutable(path, record)
            print(dumps({"path": str(path), "observations": len(observed), "identical": True}))
            return
        if not args.spec or not args.audit:
            parser.error("Clinical development requires --spec and pre-fit --audit records")
        spec = json.loads(args.spec.read_bytes())
        raw = Path(spec["inputs_path"]).read_bytes()
        if spec["official_split"] != "development" or digest(raw) != spec["inputs_sha256"]:
            raise ValueError("Only the hashed official development input is allowed")
        inputs = json.loads(raw)
        if any(not auditable_identity(d, Path(spec["inputs_path"]).parent) for d in inputs["documents"]):
            raise ValueError("Clinical development source identities are not auditable")
        head_manifest = values["_v6_models"].manifest["clinical_heads"].get("evidence")
        head = values["_v6_models"].heads.get("evidence")
        if head is None or head_manifest is None:
            raise ValueError("Complete evidence-head weights required")
        audits = {digest(p.read_bytes()): json.loads(p.read_bytes()) for p in args.audit}
        hashes = {text_key(d["text"]) for d in inputs["documents"]}
        pmids = {d["pmid"] for d in inputs["documents"]}
        for record in (values["_v6_models"].manifest, head_manifest):
            audit = audits.get(record["audit_sha256"])
            if (
                audit is None
                or not audit["passed"]
                or audit["protected_overlap"]
                or pmids & set(record["training_pmids"])
                or not hashes <= set(audit["protected_text_hashes"])
            ):
                raise ValueError("Clinical cohort was not protected before this model fitting")
            upstream_clinical(values["_clinical_models"].path, audit)
        module_sha = digest(Path(__file__).read_bytes())
        directory = (
            settings.home
            / "experiments/v6/clinical-component"
            / digest(artifact["artifact_identity"] + digest(raw) + module_sha)
        )
        directory.mkdir(parents=True, exist_ok=True)
        if (directory / "report.json").exists():
            record = json.loads((directory / "report.json").read_bytes())
            if not scoring.verify_receipt(settings, "v6_clinical_component_development", record):
                raise ValueError("Clinical component report receipt differs")
            print(dumps({"report": str(directory / "report.json"), "metrics": record["metrics"]}))
            return
        started = time.monotonic()
        threshold = head_manifest["selection"]["threshold"]
        predictions = {}
        for doc in inputs["documents"]:
            path = directory / ("paper-" + doc["pmid"] + ".json")
            if path.exists():
                pred = json.loads(path.read_bytes())
                if (
                    pred["source_hash"] != doc["source_hash"]
                    or pred["artifact_identity"] != artifact["artifact_identity"]
                    or pred["component_source_sha256"] != module_sha
                ):
                    raise ValueError("Resumed clinical component prediction identity differs")
            else:
                if digest(doc["text"]) != doc["source_hash"]:
                    raise ValueError("Clinical source differs")
                records = observations_only(doc, dictionaries, values)
                pred = {
                    "pmid": doc["pmid"],
                    "source_hash": doc["source_hash"],
                    "artifact_identity": artifact["artifact_identity"],
                    "component_source_sha256": module_sha,
                    "automatic_comparisons": automatic_comparisons(
                        {"clinical_observations": records}, threshold
                    ),
                }
                scoring._write_immutable(path, pred)
            predictions[doc["pmid"]] = pred
            if len(predictions) % 5 == 0:
                print(
                    dumps(
                        {
                            "stage": "clinical-component-source-predict",
                            "papers": len(predictions),
                            "total": len(inputs["documents"]),
                        }
                    ),
                    flush=True,
                )
        raw_gold = Path(spec["gold_path"]).read_bytes()
        if digest(raw_gold) != spec["gold_sha256"]:
            raise ValueError("Clinical development annotations changed")
        gold = json.loads(raw_gold)
        if gold["split"] != "development" or {d["pmid"] for d in gold["documents"]} != set(predictions):
            raise ValueError("Clinical source-only and gold cohorts differ")
        rows = []
        for doc in gold["documents"]:
            if digest(doc["text"]) != predictions[doc["pmid"]]["source_hash"]:
                raise ValueError("Clinical annotation/source mismatch")
            path = directory / ("provided-" + doc["pmid"] + ".json")
            if path.exists():
                provided = json.loads(path.read_bytes())
            else:
                provided = provided_comparisons(doc, doc["comparisons"], values["_encoder"], head, threshold)
                scoring._write_immutable(path, provided)
            rows.append(
                {
                    "pmid": doc["pmid"],
                    **evidence_counts(doc, predictions[doc["pmid"]]["automatic_comparisons"], provided),
                }
            )
        endpoints = (
            "provided_ico_evidence_tokens",
            "provided_ico_verified_tokens",
            "automatic_ico_evidence_tokens",
        )
        report = {
            "id": uid(),
            "at": now(),
            "schema": "v6-clinical-component-development-1",
            "artifact": artifact,
            "component_source_sha256": module_sha,
            "evaluation_path": "clinical_observation_DAG_without_unrelated_relation_inference",
            "full_engine_evaluation": False,
            "papers": len(rows),
            "per_paper": rows,
            "inputs_sha256": digest(raw),
            "gold_sha256": digest(raw_gold),
            "metrics": {key: scoring._aggregate(rows, key) for key in endpoints},
            "intervals": {key: scoring._paper_intervals(rows, key, 2000, 20261010) for key in endpoints},
            "automatic_ico_mapping": "strict three-description lexical identity; unmatched predictions remain FP",
            "ico_literal_span_accuracy": "not annotated; not measured",
            "effect_label_accuracy": "not measured",
            "confirmed_pmc_only_articles": sum(
                d.get("identifier_resolution") == "confirmed_pmc_only" for d in inputs["documents"]
            ),
            "elapsed_seconds": time.monotonic() - started,
            "sealed_test_opened": False,
            "nutrition_accuracy": "unmeasured",
            "pretraining_overlap": "unknown",
        }
        scoring._write_immutable(directory / "report.json", report)
        scoring._receipt(settings, "v6_clinical_component_development", report["id"], report)
        print(
            dumps(
                {
                    "report": str(directory / "report.json"),
                    "metrics": report["metrics"],
                    "full_engine_evaluation": False,
                    "papers": len(rows),
                }
            )
        )


if __name__ == "__main__":
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    production = Path("/home/keunsoo/projects/nutriomics-literature/runtime/locks")
    locks = production if production.exists() else ROOT / "runtime/locks"
    os.environ["LITERATURE_NLP_LOCK"] = str(locks / "nlp-cpu.lock")
    os.environ["LITERATURE_PROCESSING_LOCK_HOME"] = str(locks)
    os.nice(10)
    main()
