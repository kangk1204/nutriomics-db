"""Lossless metadata-reference storage around the unchanged scientific CLI."""

import copy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def compact_prediction(value, model_home, serializer_sha, references):
    from nutri_literature.db import digest, dumps

    if value.get("metadata_storage", {}).get("schema") == "lossless-model-metadata-reference-v6-1":
        return value
    if not value.get("manifest", {}).get("v6_models", {}).get("clinical_heads"):
        return value
    saved = dict(value)
    manifest = dict(value["manifest"])
    models = dict(manifest["v6_models"])
    heads = {}
    for kind, head in models["clinical_heads"].items():
        if not head.get("numeric_features", {}).get("paper_checkpoints"):
            heads[kind] = head
            continue
        if kind not in references:
            path = model_home / ("clinical-v6-" + kind) / "manifest.json"
            raw = path.read_bytes()
            original = json.loads(raw)
            if {**original, "manifest_sha256": digest(raw)} != head:
                raise ValueError("The full clinical head snapshot differs from the loaded manifest")
            references[kind] = {
                "manifest_path": str(path),
                "manifest_sha256": digest(raw),
                "field_path": "numeric_features.paper_checkpoints",
                "paper_checkpoints": len(original["numeric_features"]["paper_checkpoints"]),
            }
        compact = dict(head)
        numeric = dict(head["numeric_features"])
        numeric.pop("paper_checkpoints")
        numeric["paper_checkpoint_reference"] = references[kind]
        compact["numeric_features"] = numeric
        heads[kind] = compact
    models["clinical_heads"] = heads
    manifest["v6_models"] = models
    saved["manifest"] = manifest
    saved["metadata_storage"] = {
        "schema": "lossless-model-metadata-reference-v6-1",
        "serializer_sha256": serializer_sha,
        "scientific_artifact_identity": value["artifact_identity"],
        "materialization_identity": digest(
            dumps(
                {
                    "science": value["artifact_identity"],
                    "serializer": serializer_sha,
                    "references": references,
                }
            )
        ),
        "predicted_values_changed": False,
    }
    return saved


def restore_prediction(value):
    """Verify full snapshot hashes before reconstructing the original manifest."""
    from nutri_literature.db import digest

    restored = copy.deepcopy(value)
    stored = restored.pop("metadata_storage", None)
    if stored is None:
        return restored
    if stored["schema"] != "lossless-model-metadata-reference-v6-1":
        raise ValueError("Unknown metadata storage schema")
    for head in restored["manifest"]["v6_models"]["clinical_heads"].values():
        numeric = head.get("numeric_features", {})
        reference = numeric.pop("paper_checkpoint_reference", None)
        if reference is None:
            continue
        raw = Path(reference["manifest_path"]).read_bytes()
        if digest(raw) != reference["manifest_sha256"]:
            raise ValueError("Referenced clinical snapshot changed")
        original = json.loads(raw)
        numeric["paper_checkpoints"] = original["numeric_features"]["paper_checkpoints"]
    return restored


def main():
    from nutri_literature import benchmarks as scoring
    from nutri_literature.cli_v6 import main as scientific_cli
    from nutri_literature.db import digest

    args = sys.argv[1:]
    model_home = (
        Path(args[args.index("--models") + 1])
        if "--models" in args
        else Path(os.environ["LITERATURE_V6_MODEL_HOME"])
    )
    serializer_sha = digest(Path(__file__).read_bytes())
    original_write = scoring._write_immutable
    original_loads = json.loads
    references = {}

    def stored(path, value):
        # Reports, receipts, source/identity files and scientific in-memory
        # results remain untouched. Only duplicate prediction metadata moves
        # to immutable, hashed snapshots; native scoring uses the same values.
        path = Path(path)
        if path.name.startswith("paper-") and value.get("artifact_identity"):
            value = compact_prediction(value, model_home, serializer_sha, references)
        return original_write(path, value)

    def decoded(raw, *args, **kwargs):
        value = original_loads(raw, *args, **kwargs)
        if isinstance(value, dict) and value.get("artifact_identity") and value.get("manifest"):
            # Resume legacy files without retaining a separate 50-MB audit
            # list for every paper. Existing files remain byte-for-byte intact.
            return compact_prediction(value, model_home, serializer_sha, references)
        return value

    scoring._write_immutable = stored
    json.loads = decoded
    try:
        scientific_cli()
    finally:
        scoring._write_immutable = original_write
        json.loads = original_loads


if __name__ == "__main__":
    main()
