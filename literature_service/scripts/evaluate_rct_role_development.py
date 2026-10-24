"""Role classifier only, official RCT development data; never uses final test."""
import argparse
import json
from pathlib import Path
import time

from nutri_literature.clinical_v4 import ClinicalModels
from nutri_literature.db import digest, dumps, now
from nutri_literature.nlp_v2 import cpu_inference_lock
from nutri_literature.training_v4 import rct_documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--clinical", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.source.name != "rct-dev.txt":
        raise ValueError("This adapter accepts only the prepared official development file")
    source_sha = digest(args.source.read_bytes())
    manifest = json.loads((args.clinical / "manifest.json").read_bytes())
    expected_sha={entry["sha256"] for entry in manifest["heldout_source_audit"]
                  if Path(entry["path"]).name=="rct-dev.txt" and entry.get("labels_read") is False}
    if expected_sha != {source_sha}:
        raise ValueError("Official development source hash changed")
    inputs = list(rct_documents(args.source, False))
    overlap = {d["pmid"] for d in inputs} & set(manifest["training_pmids"]["rct"])
    if overlap:
        raise ValueError("Role training and development article IDs overlap")
    models = ClinicalModels(args.clinical)
    models.load()
    experiment = {"source_sha256": source_sha, "role_model_sha256": manifest["files"]["role.joblib"],
                  "adapter_sha256": digest(Path(__file__).read_bytes()), "split": "development"}
    directory = args.output / digest(dumps(experiment))
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.json"
    if report_path.exists():
        print(report_path.read_text(), flush=True)
        return
    texts = [s["text"] for d in inputs for s in d["sentences"]]
    started = time.monotonic()
    with cpu_inference_lock():
        predicted = [str(label).casefold() for label in models.role.predict(texts)]
    prediction_path = directory / "predictions.json"
    with prediction_path.open("x", encoding="utf-8") as stream:
        stream.write(dumps({"experiment": experiment, "documents": inputs, "predicted_roles": predicted}))
    # Role labels are read only after persisting the source-only predictions.
    gold = list(rct_documents(args.source, True))
    expected = [s["label"].casefold() for d in gold for s in d["sentences"]]
    assert len(expected) == len(predicted)
    from sklearn.metrics import accuracy_score, f1_score, classification_report
    report = {"at": now(), "scope": "rhetorical_role_classifier_only_official_development",
              "experiment": experiment, "papers": len(inputs), "sentences": len(expected),
              "accuracy": accuracy_score(expected, predicted),
              "macro_f1": f1_score(expected, predicted, average="macro"),
              "by_role": classification_report(expected, predicted, output_dict=True),
              "known_role_training_overlap": 0, "pretraining_overlap": "unknown",
              "elapsed_seconds": time.monotonic() - started, "prediction_sha256": digest(prediction_path.read_bytes()),
              "nutrition_accuracy": "unmeasured", "final_test": False}
    with report_path.open("x", encoding="utf-8") as stream:
        stream.write(dumps(report))
    (args.output / "rct-role-development.json").write_text(dumps(report), encoding="utf-8")
    print(dumps({"path": str(report_path), "accuracy": report["accuracy"], "macro_f1": report["macro_f1"],
                 "papers": len(inputs)}), flush=True)


if __name__ == "__main__":
    main()
