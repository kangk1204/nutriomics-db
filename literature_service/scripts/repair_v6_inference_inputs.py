"""Create corrected official boolean-input versions; preserve the failed ones."""

import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import corpora_v6
    from nutri_literature.benchmarks import _write_immutable
    from nutri_literature.clinical_identity_v6 import auditable_identity
    from nutri_literature.db import digest, dumps, now

    home = ROOT / "runtime"
    previous_path = home / "v6/evidence-preparation.json"
    previous = json.loads(previous_path.read_bytes())
    recovery = home / "v6/training-data/recovery"
    root = recovery / "inference-official-booleans"
    root.mkdir(parents=True, exist_ok=True)
    release = home / "v6/clinical-base-sources-recovery/evidenceinference.tar.gz"
    idmap = recovery / "inference-idmap/id-map.json"
    if not (root / "train-raw.json").exists():
        corpora_v6.inference_train(release, root / "train-raw.json", idmap)
    train = json.loads((root / "train-raw.json").read_bytes())
    mapped = {**train, "documents": [d for d in train["documents"] if d["pmid_mapping_known"]],
              "unmapped_training_pmcids_excluded": [d["pmcid"] for d in train["documents"]
                                                    if not d["pmid_mapping_known"]]}
    if not (root / "train-mapped.json").exists():
        _write_immutable(root / "train-mapped.json", mapped)
    if not (root / "development/spec.json").exists():
        corpora_v6.inference_development(release, root / "development", idmap)
    old_spec = json.loads(Path(previous["clinical_spec"]).read_bytes())
    old_inputs = json.loads(Path(old_spec["inputs_path"]).read_bytes())
    old_by_id = {d["pmid"]: d for d in old_inputs["documents"]}
    spec = json.loads((root / "development/spec.json").read_bytes())
    inputs = json.loads(Path(spec["inputs_path"]).read_bytes())
    confirmed = root / "development-confirmed"
    confirmed.mkdir(parents=True, exist_ok=True)
    for doc in inputs["documents"]:
        old = old_by_id[doc["pmid"]]
        if doc["source_hash"] != old["source_hash"] or doc["pmcid"] != old["pmcid"]:
            raise ValueError("Boolean parsing must not change any development source")
        if not doc["pmid_mapping_known"]:
            for key in ("identifier_resolution", "pmc_only_receipts"):
                doc[key] = old[key]
            for receipt in doc["pmc_only_receipts"].values():
                source = Path(old_spec["inputs_path"]).parent / receipt["filename"]
                target = confirmed / receipt["filename"]
                if not target.exists():
                    shutil.copyfile(source, target)
            if not auditable_identity(doc, confirmed):
                raise ValueError("Confirmed PMC-only identity receipt differs")
    new_inputs = confirmed / "inputs.json"
    if not new_inputs.exists():
        _write_immutable(new_inputs, inputs)
    if digest(new_inputs.read_bytes()) != digest(Path(old_spec["inputs_path"]).read_bytes()):
        raise ValueError("Source-only development input unexpectedly changed")
    new_spec = {**spec, "inputs_path": str(new_inputs), "inputs_sha256": digest(new_inputs.read_bytes()),
                "confirmed_pmc_only_articles": old_spec["confirmed_pmc_only_articles"],
                "pmc_only_policy": old_spec["pmc_only_policy"]}
    if not (confirmed / "spec.json").exists():
        _write_immutable(confirmed / "spec.json", new_spec)
    audited = root / "train-audited"
    if not (audited / "audit.json").exists():
        corpora_v6.audit_training(root / "train-mapped.json", [new_inputs], audited,
                                 prior_audit=Path(previous["relation"]) / "audit.json")
    prepared = json.loads((audited / "relation-train.json").read_bytes())
    gold = json.loads(Path(spec["gold_path"]).read_bytes())
    counts = {"training_papers": len(prepared["documents"]),
              "training_papers_with_comparisons": sum(bool(d["comparisons"]) for d in prepared["documents"]),
              "training_comparisons": sum(len(d["comparisons"]) for d in prepared["documents"]),
              "development_papers": len(gold["documents"]),
              "development_comparisons": sum(len(d["comparisons"]) for d in gold["documents"])}
    if not counts["training_comparisons"] or not counts["development_comparisons"]:
        raise ValueError("Official annotations still produced zero comparisons")
    plan = {**previous, "evidence": str(audited), "clinical_spec": str(confirmed / "spec.json"),
            "previous_plan_sha256": digest(previous_path.read_bytes()), "at": now(),
            "annotation_repair": "True/False official CSV; -1/-1 means missing evidence", **counts}
    destination = home / "v6/evidence-preparation-official-booleans.json"
    if not destination.exists():
        _write_immutable(destination, plan)
    print(dumps({"plan": str(destination), **counts, "source_only_inputs_unchanged": True,
                 "sealed_final_opened": False}))


if __name__ == "__main__":
    main()
