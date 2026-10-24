"""Resumable automatic-candidate training features, official training only."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.refinement_v6 import read_training
    from nutri_literature.automatic_features_v6 import prepare, labelled_rows, source_sha
    from nutri_literature.extraction_v6 import source_manifest
    from nutri_literature.operations_v6 import components, processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.dictionaries import load_dictionaries
    from nutri_literature.settings import Settings
    from nutri_literature.db import digest, dumps
    from nutri_literature.benchmarks import _write_immutable

    parser = argparse.ArgumentParser()
    for key in ("data", "audit", "models", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    os.environ["LITERATURE_V6_MODEL_HOME"] = str(args.models.resolve())
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    data, audit = read_training(args.data, args.audit)
    docs = [d for d in data["documents"] if d.get("relation_annotation_complete") is not False]
    args.output.mkdir(parents=True, exist_ok=True)
    with memory_budget() as resources, processing_slot(settings):
        values = components(settings, "union")
        dictionaries = load_dictionaries(settings, "heuristic-v2")
        identity = {
            "source_sha256": source_sha(), "frozen_source": source_manifest()[1],
            "training_sha256": digest(args.data.read_bytes()), "audit_sha256": digest(args.audit.read_bytes()),
            "provider": values["_candidate_provider"].manifest,
            "upstream_models": values["_v6_models"].manifest["manifest_sha256"],
            "clinical_models": values["_clinical_models"].manifest["manifest_sha256"],
            "dictionary_sha256": values["_gene_index"].manifest["records_sha256"],
            "chemical_sha256": values["_chemical_lexicon"]["manifest"]["snapshot_sha256"],
        }
        files = []
        for i, doc in enumerate(docs):
            path = args.output / ("paper-" + doc["pmid"] + ".json")
            if path.exists():
                record = json.loads(path.read_bytes())
                assert record["identity"] == identity and record["source_hash"] == digest(doc["text"])
            else:
                _, mentions, hypotheses, clusters, bags = prepare(doc, dictionaries, values)
                record = {
                    "pmid": doc["pmid"], "source_hash": digest(doc["text"]), "identity": identity,
                    "entities": mentions, "entity_hypotheses": hypotheses, "clusters": clusters,
                    "bags": labelled_rows(doc, bags), "gold_relations": doc.get("relations", []),
                    "source_only_features_before_labels": True,
                }
                _write_immutable(path, record)
            files.append({"path": path.name, "sha256": digest(path.read_bytes())})
            print(dumps({"stage": "automatic_train_features", "papers": i + 1, "total": len(docs),
                         "bags": len(record["bags"]), "rss": resources.get("peak_rss_bytes")}), flush=True)
        _write_immutable(args.output / "manifest.json", {
            "schema": "automatic-candidate-v6-r4-1", "identity": identity, "files": files,
            "training_split": "train", "test_used_for_selection": False,
            "training_pmids": sorted(d["pmid"] for d in docs), "resources": resources,
            "upstream_models_fixed_not_fold_refit": True,
        })


if __name__ == "__main__":
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = "1"
    if os.name == "posix":
        os.nice(10)
        production = Path("/home/keunsoo/projects/nutriomics-literature/runtime/locks")
        if production.exists():
            os.environ["LITERATURE_PROCESSING_LOCK_HOME"] = str(production)
            os.environ["LITERATURE_NLP_LOCK"] = str(production / "nlp-cpu.lock")
    main()
