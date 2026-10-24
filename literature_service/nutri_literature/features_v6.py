"""Bind cached/trained features to their actual implementation, not names alone."""

from pathlib import Path

from .db import digest


def source_hashes(kind="relation"):
    common = {"features_v6", "embeddings_v6", "nlp_v2", "models_v6", "clinical_v6"}
    if kind == "relation":
        modules = common | {
            "training_v6",
            "identity_v6",
            "provider_v6",
            "chemical_v6",
            "candidates_v4",
            "candidates_v5",
            "features_v5",
            "extraction_v3",
        }
    elif kind == "clinical":
        modules = common | {"clinical_training_v6", "clinical_v4"}
    else:
        raise ValueError("Unknown v6 feature implementation contract")
    directory = Path(__file__).parent
    return {module + ".py": digest((directory / (module + ".py")).read_bytes()) for module in sorted(modules)}
