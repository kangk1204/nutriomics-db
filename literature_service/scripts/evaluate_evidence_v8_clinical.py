"""Input-first exposed-development PIO evaluation using unchanged native scoring."""
import argparse
import hashlib
import json
import random
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutri_literature import benchmarks as scoring
from nutri_literature.clinical_v4 import TOKEN, token_features
from nutri_literature.text import sentence_spans

ROLES = ("participants", "interventions", "outcomes")
CORPORA = ("ebmnlp", "evidenceoutcomes")


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def filehash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path = Path(path)
    content = canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError("Immutable clinical experiment changed: " + str(path))
        return
    temporary = path.with_name(".v8clinical-" + __import__("uuid").uuid4().hex[:8] + ".part")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        __import__("os").fsync(stream.fileno())
    temporary.replace(path)


def read(path):
    return json.loads(Path(path).read_bytes())


def code_hashes():
    import nutri_literature.clinical_v4 as features
    import nutri_literature.evaluation_v6 as evaluator
    import nutri_literature.text as text
    return {"script": filehash(__file__), "native_scoring": filehash(scoring.__file__),
            "v6_evaluation": filehash(evaluator.__file__), "features": filehash(features.__file__),
            "sentence_splitter": filehash(text.__file__)}


def predict_document(document, models):
    spans = []
    for sentence in sentence_spans(document["text"], document.get("sections", [])):
        tokens = [{"text": m.group(), "start": sentence["start"] + m.start(), "end": sentence["start"] + m.end()}
                  for m in TOKEN.finditer(sentence["text"])]
        if not tokens:
            continue
        features = [token_features(tokens, index) for index in range(len(tokens))]
        for role in ROLES:
            tags = list(models[role].predict_single(features))
            if len(tags) != len(tokens) or any(tag not in {"B", "I", "O"} for tag in tags):
                raise ValueError("Invalid native CRF BIO output")
            begin = None
            for index, tag in enumerate(tags + ["O"]):
                if begin is not None and tag != "I":
                    start, end = tokens[begin]["start"], tokens[index - 1]["end"]
                    span = {"label": role, "start": start, "end": end,
                            "text": document["text"][start:end], "origin": "v8-native-pio-crf", "status": "candidate"}
                    scoring.validate_span(document["text"], span)
                    spans.append(span)
                    begin = None
                if tag in {"B", "I"} and begin is None:
                    begin = index
    return {"pmid": document["pmid"], "source_hash": document["source_hash"], "annotated_spans": spans}


def predict(models_directory, inputs_path, input_manifest, output, limit=200):
    import joblib
    models_directory, output = Path(models_directory), Path(output)
    inputs, receipt = read(inputs_path), read(input_manifest)
    if (inputs.get("schema_version") != "literature-public-inputs-v1" or inputs.get("split") != "development"
            or inputs.get("corpus") not in CORPORA or receipt.get("split") != "development"
            or inputs.get("corpus") != receipt.get("corpus") or not 1 <= limit <= 200
            or filehash(inputs_path) != receipt["inputs_sha256"]):
        raise ValueError("Only unchanged exposed-development source inputs are permitted")
    documents = inputs["documents"]
    if len({d["pmid"] for d in documents}) != len(documents):
        raise ValueError("Duplicate source PMID")
    for document in documents:
        if (any(k in document for k in ("entities", "relations", "scopes", "cues", "annotated_spans", "labels", "gold"))
                or sha(document["text"]) != document["source_hash"]):
            raise ValueError("Source-only prediction input violated")
    documents = sorted(documents, key=lambda d: sha("v3-development-20261003:" + d["pmid"]))[:limit]
    model_manifest = read(models_directory / "manifest.json")
    if model_manifest.get("trained") is not True:
        raise ValueError("Clinical model training is incomplete")
    models, model_hashes = {}, {}
    for role in ROLES:
        path = models_directory / (role + ".joblib")
        model_hashes[role] = filehash(path)
        if model_hashes[role] != model_manifest["models"][role]["model_sha256"]:
            raise ValueError("Clinical model SHA-256 mismatch")
        models[role] = joblib.load(path)
    freeze = {"corpus": inputs["corpus"], "split": "development", "limit": limit,
              "input_sha256": filehash(inputs_path), "input_manifest_sha256": filehash(input_manifest),
              "expected_gold_sha256": receipt["gold_sha256"], "model_sha256": model_hashes,
              "model_manifest_sha256": filehash(models_directory / "manifest.json"),
              "code_sha256": code_hashes(), "selection": "v3-development-20261003 SHA256 sort; first limit",
              "pmids": [d["pmid"] for d in documents], "source_hashes": {d["pmid"]: d["source_hash"] for d in documents},
              "cohort_sha256": sha(canonical(sorted(d["pmid"] for d in documents))), "independent_final_performance": False}
    write(output / "freeze.json", freeze)
    started = time.monotonic()
    receipts = []
    for document in documents:
        path = output / "predictions" / ("paper-" + document["pmid"] + ".json")
        value = predict_document(document, models)
        write(path, value)
        receipts.append({"path": str(path.relative_to(output)).replace("\\", "/"), "sha256": filehash(path)})
    # The only artifact making gold readable is written after every prediction.
    complete = {"status": "all_predictions_saved_no_gold_read", "freeze_sha256": filehash(output / "freeze.json"),
                "documents": len(documents), "files": receipts}
    write(output / "predictions-complete.json", complete)
    return {"corpus": freeze["corpus"], "documents": len(documents), "predicted_spans": sum(
            len(read(output / r["path"])["annotated_spans"]) for r in receipts), "seconds": time.monotonic() - started,
            "output": str(output), "status": complete["status"]}


def score(output, gold_path):
    output = Path(output)
    freeze, complete = read(output / "freeze.json"), read(output / "predictions-complete.json")
    if complete.get("status") != "all_predictions_saved_no_gold_read" or complete["freeze_sha256"] != filehash(output / "freeze.json"):
        raise ValueError("Complete source-only prediction freeze required before gold")
    if freeze["code_sha256"] != code_hashes():
        raise ValueError("Evaluator/feature source changed after prediction freeze")
    predictions = {}
    for entry in complete["files"]:
        path = output / entry["path"]
        if filehash(path) != entry["sha256"]:
            raise ValueError("Saved prediction SHA-256 mismatch")
        document = read(path)
        if document["pmid"] in predictions or document["source_hash"] != freeze["source_hashes"][document["pmid"]]:
            raise ValueError("Duplicate or changed prediction source")
        predictions[document["pmid"]] = document
    if set(predictions) != set(freeze["pmids"]) or len(predictions) != complete["documents"]:
        raise ValueError("Missing prediction; gold remains unreadable")
    # Gold is first opened here, after all saved predictions have been verified.
    raw = Path(gold_path).read_bytes()
    if sha(raw) != freeze["expected_gold_sha256"]:
        raise ValueError("Gold does not match the frozen official development receipt")
    gold = json.loads(raw)
    if (gold.get("schema_version") != "literature-public-gold-v1" or gold.get("split") != "development"
            or gold.get("corpus") != freeze["corpus"]
            or gold.get("manifest", {}).get("annotation_status") != "published_corpus_annotations"):
        raise ValueError("Only native published development annotations are permitted")
    labels = {d["pmid"]: d for d in gold["documents"]}
    roles = ROLES if freeze["corpus"] == "ebmnlp" else ("outcomes",)
    rows = []
    for pmid in freeze["pmids"]:
        document = labels[pmid]
        if sha(document["text"]) != freeze["source_hashes"][pmid]:
            raise ValueError("Native label source differs from frozen prediction text")
        expected, observed = scoring._span_sets(document, predictions[pmid], "annotated_spans", "label", roles)
        row = {"pmid": pmid}
        for role in roles:
            row["spans_" + role] = scoring._metrics({k for k in expected if k[1] == role},
                                                   {k for k in observed if k[1] == role})
        rows.append(row)
    endpoints = ["spans_" + role for role in roles]
    result = {"engine": "nutriomics-evidence-v8-pio-only", "corpus": freeze["corpus"], "split": "development",
              "documents": len(rows), "input_sha256": freeze["input_sha256"], "gold_sha256": sha(raw),
              "cohort_sha256": freeze["cohort_sha256"], "model_sha256": freeze["model_sha256"],
              "metrics": {key: scoring._aggregate(rows, key) for key in endpoints}, "per_paper": rows,
              "intervals": {key: scoring._paper_intervals(rows, key, 2000, 20261023) for key in endpoints},
              "source_position_errors": 0, "metric_definition": "unchanged benchmarks._span_sets whitespace-token overlap",
              "independent_final_performance": False, "nutrition_accuracy": "unmeasured",
              "full_ico_group_dose_time": "not_evaluated", "code_sha256": freeze["code_sha256"]}
    report = output / "report.json"
    if report.exists():
        if read(report) != result:
            raise ValueError("Offline rescore differs from the saved native result")
        write(output / "offline-rescore.json", {"identical": True, "report_sha256": filehash(report), "gold_sha256": sha(raw)})
    else:
        write(report, result)
    return {k: v for k, v in result.items() if k not in {"per_paper", "code_sha256", "model_sha256"}}


def compare(output, baseline_report):
    output = Path(output)
    candidate, baseline = read(output / "report.json"), read(baseline_report)
    endpoint = "spans_outcomes"
    a = {r["pmid"]: r for r in candidate.get("per_paper", [])}
    b = {r["pmid"]: r for r in baseline.get("per_paper", [])}
    same = (bool(a) and set(a) == set(b) and candidate["input_sha256"] == baseline.get("input_sha256")
            and all(a[p][endpoint]["tp"] + a[p][endpoint]["fn"] == b[p][endpoint]["tp"] + b[p][endpoint]["fn"] for p in a))
    result = {"same_cohort_and_gold_token_denominators": same, "baseline_report_sha256": filehash(baseline_report),
              "candidate": candidate["metrics"][endpoint], "baseline": baseline.get("metrics", {}).get(endpoint),
              "difference": None, "independent_final_performance": False}
    if same:
        result["difference"] = {k: candidate["metrics"][endpoint][k] - baseline["metrics"][endpoint][k]
                                if candidate["metrics"][endpoint][k] is not None and baseline["metrics"][endpoint][k] is not None else None
                                for k in ("precision", "recall", "f1")}
        rng, deltas, keys = random.Random(20261023), [], sorted(a)
        for _ in range(2000):
            selected = rng.choices(keys, k=len(keys))
            x = scoring._aggregate([a[k] for k in selected], endpoint)["f1"]
            y = scoring._aggregate([b[k] for k in selected], endpoint)["f1"]
            if x is not None and y is not None:
                deltas.append(x - y)
        deltas.sort()
        result["paired_bootstrap_f1_difference_ci95"] = {"lower": deltas[int(.025 * (len(deltas) - 1))],
                                                         "upper": deltas[int(.975 * (len(deltas) - 1))], "iterations": 2000}
    write(output / ("comparison-" + filehash(baseline_report)[:12] + ".json"), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("predict")
    for name in ("models", "inputs", "input-manifest", "output"):
        p.add_argument("--" + name, required=True, type=Path)
    p.add_argument("--limit", type=int, default=200)
    for command in ("score", "compare"):
        q = sub.add_parser(command)
        q.add_argument("--output", required=True, type=Path)
        q.add_argument("--gold" if command == "score" else "--baseline-report", required=True, type=Path)
    args = parser.parse_args()
    result = (predict(args.models, args.inputs, args.input_manifest, args.output, args.limit) if args.command == "predict"
              else score(args.output, args.gold) if args.command == "score" else compare(args.output, args.baseline_report))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
