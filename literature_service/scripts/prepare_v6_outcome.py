"""Protect RCT dev/test source texts as well as all prior reserved corpora."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("This preparation uses the isolated Linux v6 assets only")
    from nutri_literature.corpora_v6 import audit_training
    from nutri_literature.training_v4 import exclusions, rct_documents
    from nutri_literature.db import dumps
    from nutri_literature.benchmarks import _write_immutable

    sources = ROOT / "runtime/v6/clinical-base-sources"
    excluded, _, _ = exclusions(sources, ROOT / "runtime/public_benchmarks")
    preparation = json.loads((ROOT / "runtime/v6/data-preparation.json").read_bytes())
    prior = ROOT / Path(preparation["audited_directory"].replace("\\", "/")) / "audit.json"
    directory = ROOT / "runtime/v6/training-data"
    paths = []
    for split in ("dev", "test"):
        path = directory / ("rct-" + split + "-source-only.json")
        documents = [
            {"pmid": row["pmid"], "text": "\n".join(s["text"] for s in row["sentences"])}
            for row in rct_documents(sources / ("rct-" + split + ".txt"))
        ]
        if not path.exists():
            _write_immutable(
                path, {"split": "development" if split == "dev" else "test", "documents": documents}
            )
        paths.append(path)
    result = audit_training(
        directory / "outcome-raw.json",
        paths,
        directory / "outcome-audited-safe",
        protected_pmids=excluded,
        prior_audit=prior,
    )
    print(
        dumps(
            {
                "training_articles": len(result["training_pmids"]),
                "excluded_articles": len(result["excluded_pmids"]),
                "protected_pmids": len(result["protected_pmids"]),
                "protected_overlap": result["protected_overlap"],
                "test_labels_read": False,
            }
        )
    )


if __name__ == "__main__":
    main()
