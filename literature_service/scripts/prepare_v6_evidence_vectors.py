"""Prepare the exact evidence-head numeric cache concurrently; fit no model."""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    production = Path("/home/keunsoo/projects/nutriomics-literature/runtime/locks")
    locks = production if production.exists() else ROOT / "runtime/locks"
    os.environ.setdefault("LITERATURE_NLP_LOCK", str(locks / "nlp-cpu.lock"))
    os.environ.setdefault("LITERATURE_PROCESSING_LOCK_HOME", str(locks))
    os.nice(10)
    from nutri_literature.audit_v6 import upstream_clinical
    from nutri_literature.clinical_training_v6 import inference_features, numeric_examples
    from nutri_literature.db import digest, dumps
    from nutri_literature.embeddings_v6 import FrozenEncoder
    from nutri_literature.features_v6 import source_hashes
    from nutri_literature.locking import lock
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.settings import Settings
    from nutri_literature.text import sentence_spans
    from nutri_literature.training_v4 import text_key

    home = ROOT / "runtime"
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, default=home / "v6/evidence-preparation-official-booleans.json")
    parser.add_argument("--last-half", action="store_true",
                        help="Precompute the reverse final half; retain the full-training identity")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_bytes())
    data, audit_path = (Path(plan["evidence"]) / name for name in ("relation-train.json", "audit.json"))
    value, audit = json.loads(data.read_bytes()), json.loads(audit_path.read_bytes())
    if (value["split"] != "train" or not audit["passed"] or audit["protected_overlap"]
            or digest(data.read_bytes()) != audit["audited_training_sha256"]):
        raise ValueError("Exact audited evidence training data required")
    documents = [d for d in value["documents"] if d["pmid"] in audit["training_pmids"]
                 and text_key(d["text"]) not in audit["protected_text_hashes"]]
    if any(not d["pmid_mapping_known"] for d in documents):
        raise ValueError("Training vectors require mapped official IDs")
    original_papers = len(documents)
    if args.last_half:
        documents = list(reversed(documents[original_papers // 2:]))
    with memory_budget(), processing_slot(Settings(home, ROOT / "config/topics.json")), lock(
        home / "embedding-cache-v6/clinical-training/evidence-fitting.lock"
    ):
        models = Path(plan["models"])
        upstream = upstream_clinical(models / "clinical", audit)
        encoder = FrozenEncoder(models / "gene/selected_model", home / "embedding-cache-v6")
        encoder.load()
        columns = ["context_" + str(i) for i in range(encoder.manifest["dimension"])] + [
            field + suffix for field in ("intervention_description", "comparator_description", "outcome_description")
            for suffix in ("_similarity", "_coverage", "_missing")]
        identity = {"kind": "evidence", "features": source_hashes("clinical"), "encoder": encoder.manifest,
                    "training_sha256": digest(data.read_bytes()), "audit_sha256": digest(audit_path.read_bytes()),
                    "upstream_clinical_audit": upstream}

        def generate(document):
            for comparison in document.get("comparisons", []):
                for sentence in sentence_spans(document["text"], document["sections"]):
                    label = int(any(q["start"] < sentence["end"] and sentence["start"] < q["end"]
                                    for q in comparison["evidence"]))
                    yield inference_features(document["text"], sentence, comparison, encoder), label

        matrix, labels, pmids, receipts = numeric_examples(
            documents, columns, home / "embedding-cache-v6/clinical-training" / digest(dumps(identity))[:24],
            identity, generate)
        print(dumps({"feature_preparation_complete": True, "training_models_fitted": False,
                     "articles": len(set(pmids)), "examples": len(labels), "paper_checkpoints": len(receipts),
                     "identity_sha256": digest(dumps(identity)), "matrix_shape": list(matrix.shape),
                     "complete_training_cohort": original_papers, "subset_only": args.last_half,
                     "numeric_cache_directory": str(home / "embedding-cache-v6/clinical-training" /
                                                    digest(dumps(identity))[:24])}), flush=True)


if __name__ == "__main__":
    main()
