"""Freeze complete D/C dictionaries and audit official train sources; no fitting/test labels."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.corpora_v6 import merge_training, audit_training
    from nutri_literature.db import digest, dumps

    data = ROOT / "runtime/v6/training-data"
    prior = json.loads((data / "audited-direction-v6/audit.json").read_bytes())
    protected = [ROOT / Path(r["path"].replace("\\", "/")) for r in prior["protected_sources"]]
    if not all(path.is_file() for path in protected):
        raise ValueError("Protected source-only inputs must all exist")
    merged = data / "official-merged-train-v6.json"
    sources = [
        data / "official-relation-train-direction-v6.json",
        data / "official-nlm-train/nlmgene-train.json",
        data / "official-nlm-train/nlmchem-train.json",
    ]
    merge_training(sources, merged)
    output = data / ("audited-complete-v6-" + digest(merged.read_bytes())[:16])
    audit = audit_training(merged, protected, output, prior["protected_pmids"])
    mesh = ROOT / "runtime/v6/models/mesh"
    snapshot = next(mesh.glob("mesh-v6-*.json"))
    value = json.loads(snapshot.read_bytes())
    pointer = {
        "filename": snapshot.name,
        "sha256": digest(snapshot.read_bytes()),
        "records_sha256": value["manifest"]["records_sha256"],
    }
    (mesh.parent / "mesh-snapshot.json").write_text(dumps(pointer), encoding="utf-8")
    summary = {
        "audited_directory": str(output.relative_to(ROOT)),
        "training_articles": len(audit["training_pmids"]),
        "excluded_articles": len(audit["excluded_pmids"]),
        "protected_pmids": len(audit["protected_pmids"]),
        "protected_overlap": audit["protected_overlap"],
        "audited_training_sha256": audit["audited_training_sha256"],
        "dictionary": {**pointer, "counts": value["manifest"]["namespace_counts"]},
        "test_labels_read": False,
        "models_trained": False,
        "scientific_accuracy_measured": False,
    }
    (ROOT / "runtime/v6/data-preparation.json").write_text(dumps(summary), encoding="utf-8")
    print(dumps(summary))


if __name__ == "__main__":
    main()
