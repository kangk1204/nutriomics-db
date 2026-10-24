"""Import verified source-only snapshots into a new experiment, never old globals."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resolve-models", action="store_true")
    args = parser.parse_args()
    os.environ["EVIDENCE_V8_HOME"] = str(args.output.resolve())
    from nutri_literature.evidence_v8.common import owner
    from nutri_literature.evidence_v8.data import audit, import_r2
    with owner():
        imported = import_r2(args.source_root)
        checked = audit()
        result = {"imported_files": len(imported["files"]), "audit": checked, "generative_model_calls": 0}
        if args.resolve_models:
            from nutri_literature.evidence_v8.retrieval import resolve_models
            result["models"] = resolve_models()
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
