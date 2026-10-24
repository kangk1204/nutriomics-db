"""OOF gene identity repair -> deployment-noise training -> same dev scoring."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.automatic_identity_v6 import train
    from nutri_literature.automatic_features_v6 import endpoint_features, training_labels
    from nutri_literature.automatic_models_v6 import train as train_relations
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.settings import Settings
    from nutri_literature.benchmarks import _write_immutable
    from nutri_literature.db import digest, dumps
    work, raw_features = ROOT / "runtime/v6/refinement-r5", ROOT / "runtime/v6/refinement-r4/automatic-features"
    work.mkdir(parents=True, exist_ok=True)
    started = time.time()
    while not (raw_features / "manifest.json").exists():
        if time.time() - started > 4 * 3600:
            raise TimeoutError("Raw deployment features incomplete")
        time.sleep(20)
    base = ROOT / "runtime/v6/training-data/recovery/relation-evidence-audited"
    identity_dir, features = work / "identity-head", work / "automatic-features-oof"
    with memory_budget() as resources, processing_slot(Settings(ROOT / "runtime", ROOT / "config/topics.json")):
        if not (identity_dir / "manifest.json").exists():
            train(base / "relation-train.json", base / "audit.json", raw_features,
                  ROOT / "runtime/v6/refinement-r1/frozen-models", identity_dir)
        records = json.loads((identity_dir / "oof-identities.json").read_bytes())
        selected = {(r["pmid"], r["start"], r["end"]): r["record"] for r in records if r["record"]}
        manifest = json.loads((raw_features / "manifest.json").read_bytes())
        documents = {d["pmid"]: d for d in json.loads((base / "relation-train.json").read_bytes())["documents"]}
        features.mkdir(parents=True, exist_ok=True)
        files = []
        for f in manifest["files"]:
            raw = (raw_features / f["path"]).read_bytes()
            assert digest(raw) == f["sha256"]
            record = json.loads(raw)
            for bag in record["bags"]:
                for pair in bag["endpoints"]:
                    gene = pair["gene"]
                    replacement = selected.get((record["pmid"], gene["start"], gene["end"]))
                    if replacement and not gene.get("normalized_id"):
                        gene["normalized_id"] = replacement["id"]
                        gene["payload"].update(native_id=replacement["native_id"], identity_reason=None,
                                               normalization_rule="paper_oof_absolute_identity_r5")
                bag["features"] = {k: v for k, v in bag["features"].items() if not k.startswith("quality:")}
                bag["features"].update(endpoint_features(bag["endpoints"]))
                bag["labels"] = training_labels(documents[record["pmid"]], bag["endpoints"])
            record["identity_oof"] = {"manifest_sha256": digest((identity_dir / "manifest.json").read_bytes()),
                                      "all_replacements_held_out_by_paper": True}
            path = features / f["path"]
            if path.exists():
                assert json.loads(path.read_bytes()) == record
            else:
                _write_immutable(path, record)
            files.append({"path": f["path"], "sha256": digest(path.read_bytes())})
        new_manifest = {**manifest, "files": files, "identity_oof_manifest": digest((identity_dir / "manifest.json").read_bytes())}
        if not (features / "manifest.json").exists():
            _write_immutable(features / "manifest.json", new_manifest)
        if not (work / "relation-head/manifest.json").exists():
            train_relations(features, ROOT / "runtime/v6/validation/relation-refinement-training-export.json.gz", work / "relation-head")
    # Exit the training resource/slot scope before launching evaluation.
    output = work / "relations-end_to_end"
    if not (output / "report.json").exists():
        result = subprocess.run([sys.executable, "scripts/evaluate_v6_automatic_relations.py",
            "--models", "runtime/v6/refinement-r1/frozen-models", "--head", str(work / "relation-head"),
            "--normalizer", str(identity_dir), "--baseline-predictions", "runtime/v6/refinement-r1/development/end_to_end",
            "--mode", "end_to_end", "--output", str(output)], cwd=ROOT)
        if result.returncode:
            raise RuntimeError("Identity-aware relation evaluation failed")
    _write_immutable(work / "identity-pipeline-complete.json", {"completed": True, "elapsed_seconds": time.time() - started})


if __name__ == "__main__":
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    if os.name == "posix":
        os.nice(10)
    main()
