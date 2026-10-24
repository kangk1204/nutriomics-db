"""Require upstream learned components to respect the same protected paper IDs."""

import json
from pathlib import Path

from .db import digest


def upstream_clinical(path, audit):
    raw = (Path(path) / "manifest.json").read_bytes()
    record = json.loads(raw)
    training = record.get("training_pmids")
    if not isinstance(training, dict) or not training or record.get("training_split") != "train":
        raise ValueError("Upstream clinical training article provenance is missing")
    ids = {str(pmid) for group in training.values() for pmid in group}
    overlap = ids & set(audit.get("protected_pmids", []))
    if overlap:
        raise ValueError(
            "Upstream clinical models require retraining; protected PMID overlap: " + str(len(overlap))
        )
    return {
        "manifest_sha256": digest(raw),
        "training_articles": len(ids),
        "protected_pmid_overlap": 0,
        "pmcid_and_pretraining_overlap": "not established by this PMID check",
    }


def train_clinical_base(sources, output, benchmarks, audit_path):
    """Reuse the frozen trainer with new inputs and isolated output; never edit v4."""
    from .training_v4 import train

    audit = json.loads(Path(audit_path).read_bytes())
    if not audit.get("passed") or audit.get("protected_overlap", 1):
        raise ValueError("Protected-source audit required")
    sources = Path(sources)
    if sources.is_symlink():
        raise ValueError("Clinical training requires an isolated source directory, not a legacy symlink")
    target = sources / "v6-protected-heldout-ids.txt"
    encoded = "\n".join(sorted(set(audit["protected_pmids"]))) + "\n"
    if target.exists() and target.read_text(encoding="utf-8") != encoded:
        raise ValueError("Clinical training exclusions changed; use a new source directory")
    target.write_text(encoded, encoding="utf-8")
    result = train(sources, output, benchmarks)
    upstream_clinical(output, audit)
    return result
