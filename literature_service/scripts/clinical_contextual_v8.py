"""Actual CPU diagnostic: new PIO proposals + unchanged frozen v6 outcome head.

This does not replay legacy outcome predictions, retrain a model, or select a
threshold on development gold. Legacy scientific source files are read-only.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf8")).hexdigest()


def filehash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read(path):
    return json.loads(Path(path).read_bytes())


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical(value)
    if path.exists():
        if path.read_text(encoding="utf8") != content:
            raise ValueError("Immutable contextual diagnostic changed")
        return
    tmp = path.with_name(path.name + ".part")
    with tmp.open("x", encoding="utf8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def bootstrap(source_root):
    sys.path.insert(0, str(Path(source_root).resolve()))


def resources():
    import resource
    available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                         if line.startswith("MemAvailable:"))) * 1024
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    if available < 16 * 1024 ** 3 or peak > 8 * 1024 ** 3:
        raise MemoryError("Contextual CPU diagnostic requires 16GiB available and RSS below 8GiB")
    return {"available_bytes": available, "peak_rss_bytes": peak}


def install_readonly_shared_lock(paths):
    """Same legacy lock inodes, exclusive flock, opened read-only on RO host."""
    import fcntl
    import nutri_literature.nlp_v2 as nlp
    import nutri_literature.embeddings_v6 as embeddings
    original = nlp.cpu_inference_lock
    paths = sorted(set(Path(p).resolve() for p in paths))
    if not paths or any(not p.is_file() for p in paths):
        raise ValueError("Actual existing NLP lock files are required")

    @contextmanager
    def compatible(path=None):
        if path is not None:
            with original(path):
                yield path
            return
        streams = []
        try:
            for target in paths:
                stream = target.open("rb")
                streams.append(stream)
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield paths[0]
        finally:
            for stream in reversed(streams):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
    # Process-local control adapter only: no on-disk core/source/environment edit.
    nlp.cpu_inference_lock = embeddings.cpu_inference_lock = compatible
    return [{"path": str(p), "inode": p.stat().st_ino, "open_mode": "read-only",
             "flock": "exclusive"} for p in paths]


def readonly_encoder(model_path, cache, legacy_cache):
    import numpy as np
    from nutri_literature.embeddings_v6 import FrozenEncoder
    from nutri_literature.db import digest, dumps

    class ReadThroughEncoder(FrozenEncoder):
        def __init__(self):
            super().__init__(model_path, cache)
            self.cache_counts = Counter()

        def encode(self, text, sentence, entities=()):
            self.load()
            if text[sentence["start"]:sentence["end"]] != sentence["text"] or any(
                    text[e["start"]:e["end"]] != e["text"] for e in entities):
                raise ValueError("Contextual encoder Unicode offset mismatch")
            key = digest(dumps({"source": digest(text), "sentence": [sentence["start"], sentence["end"]],
                                "entities": [(e["start"], e["end"]) for e in entities], "encoder": self.manifest}))
            old = Path(legacy_cache) / (key + ".npy")
            receipt = old.with_suffix(".json")
            if old.is_file() and receipt.is_file():
                metadata = read(receipt)
                if metadata["key"] != key or filehash(old) != metadata["sha256"]:
                    raise ValueError("Read-only legacy embedding cache integrity failure")
                value = np.load(old, allow_pickle=False)
                if value.shape != (len(entities) + 1, self.manifest["dimension"]):
                    raise ValueError("Read-only legacy vector shape mismatch")
                self.cache_counts["verified_legacy_cache_hits"] += 1
                return value
            existed = (Path(cache) / (key + ".npy")).is_file()
            value = super().encode(text, sentence, entities)
            self.cache_counts["new_cache_hits" if existed else "actual_cpu_encoder_forwards"] += 1
            return value
    return ReadThroughEncoder()


def contextual_document(document, pio, parse, encoder, head, candidate_fn, threshold, sentence_fn):
    text = document["text"]
    if pio["pmid"] != document["pmid"] or pio["source_hash"] != sha(text):
        raise ValueError("New PIO proposal/source identity mismatch")
    output, counts = [], Counter()
    for span in pio["annotated_spans"]:
        if not (0 <= span["start"] < span["end"] <= len(text)) or text[span["start"]:span["end"]] != span["text"]:
            raise ValueError("PIO proposal offset mismatch")
        if span["label"] not in {"participants", "interventions", "outcomes"}:
            raise ValueError("Unknown PIO proposal kind")
        if span["label"] != "outcomes":
            output.append(span)
    for sentence in sentence_fn(text, document.get("sections", [])):
        sentence = {**sentence, "pico_spans": [s for s in pio["annotated_spans"]
                    if sentence["start"] <= s["start"] < s["end"] <= sentence["end"]]}
        candidates = candidate_fn(sentence, parse(sentence))
        for candidate in candidates:
            counts["proposals_" + candidate["origin"]] += 1
            if text[candidate["start"]:candidate["end"]] != candidate["text"]:
                raise ValueError("Contextual candidate Unicode offset mismatch")
            vector = encoder.encode(text, sentence, (candidate,)).reshape(-1)
            score = float(head.probabilities([{str(i): float(v) for i, v in enumerate(vector)}])[0].get("1", 0))
            if score >= threshold:
                output.append({**candidate, "priority_score": score, "scores_are_nutrition_probabilities": False,
                               "contextual_origin": "frozen_v6_head_new_v8_pio_proposals"})
                counts["accepted_" + candidate["origin"]] += 1
            else:
                counts["rejected_" + candidate["origin"]] += 1
    return {"pmid": document["pmid"], "source_hash": document["source_hash"], "annotated_spans": output,
            "diagnostics": dict(counts), "original_v6_outcome_predictions_replayed": False}


def predict(args):
    bootstrap(args.source_root)
    import joblib
    import torch
    import nutri_literature.embeddings_v6 as embedding_module
    import nutri_literature.nlp_v2 as nlp
    from nutri_literature.clinical_v6 import outcome_candidates
    import nutri_literature.clinical_v6 as clinical_module
    from nutri_literature.features_v6 import source_hashes
    from nutri_literature.text import sentence_spans
    os.environ.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    torch.set_num_threads(1)
    if torch.get_num_interop_threads() != 1:
        torch.set_num_interop_threads(1)
    os.nice(10)
    resources()
    bundle, output = read(args.bundle), Path(args.output)
    if bundle.get("schema") != "v8-contextual-source-input/1" or bundle.get("gold_read_by_adapter") is not False:
        raise ValueError("Source-only frozen PIO bundle required")
    head_manifest = read(args.head_manifest)
    if (head_manifest.get("kind") != "outcome" or head_manifest.get("training_split") != "train"
            or head_manifest.get("test_used_for_selection") is not False
            or head_manifest.get("feature_source_hashes") != source_hashes("clinical")):
        raise ValueError("Unchanged train-only v6 outcome-head source contract required")
    threshold = head_manifest["selection"]["threshold"]
    if head_manifest["selection"].get("source") != "paper-separated-train-OOF":
        raise ValueError("Threshold must be the original training-OOF choice")
    head_path = Path(args.head_manifest).parent / "outcome.joblib"
    if filehash(head_path) != head_manifest["files"]["outcome.joblib"]:
        raise ValueError("Original frozen v6 outcome model SHA mismatch")
    shared = install_readonly_shared_lock(args.shared_lock)
    head = joblib.load(head_path)
    encoder = readonly_encoder(args.encoder, output / "embedding-cache", args.legacy_cache)
    encoder.load()
    if encoder.manifest != head_manifest["encoder"]:
        raise ValueError("Unchanged frozen v6 encoder manifest mismatch")
    parser = nlp.parser_for({})
    parser._load()
    resources()
    freeze = {"bundle_sha256": filehash(args.bundle), "adapter_sha256": filehash(__file__),
              "head_manifest_sha256": filehash(args.head_manifest), "outcome_head_sha256": filehash(head_path),
              "threshold": threshold, "threshold_source": "unchanged train-paper-OOF; not selected using development gold",
              "encoder": encoder.manifest, "parser": parser.manifest, "shared_nlp_locks": shared,
              "clinical_source_sha256": filehash(clinical_module.__file__),
              "encoder_source_sha256": filehash(embedding_module.__file__), "parser_source_sha256": filehash(nlp.__file__),
              "new_v8_pio_original_v6_ranker_upstream_mismatch": True,
              "status": "diagnostic_only_not_promotable", "device": "cpu", "source_root_read_only": True,
              "original_outcome_predictions_replayed": False, "gold_read_by_adapter": False,
              "additional_model_api_calls": 0}
    atomic(output / "freeze.json", freeze)
    totals, files = Counter(), []
    for corpus in ("ebmnlp", "evidenceoutcomes"):
        selection = bundle["corpora"][corpus]
        for index, item in enumerate(selection["records"]):
            document, pio = item["document"], item["pio_prediction"]
            path = output / corpus / ("paper-" + document["pmid"] + ".json")
            if path.exists():
                value = read(path)
                if value["source_hash"] != document["source_hash"]:
                    raise ValueError("Contextual resumed source mismatch")
            else:
                value = contextual_document(document, pio,
                    lambda s: nlp.validate_tokens(parser.parse(s), s), encoder, head, outcome_candidates,
                    threshold, sentence_spans)
                atomic(path, value)
            totals.update(value["diagnostics"])
            files.append({"corpus": corpus, "pmid": document["pmid"], "path": str(path.relative_to(output)), "sha256": filehash(path)})
            if (index + 1) % 10 == 0:
                progress = {"corpus": corpus, "papers": index + 1, "corpus_total": len(selection["records"]),
                            "pid": os.getpid(), "resources": resources(), "cache": dict(encoder.cache_counts)}
                target = output / "progress.json"
                temporary = target.with_suffix(".new")
                temporary.write_text(canonical(progress)); temporary.replace(target)
                print(canonical(progress), flush=True)
    complete = {"status": "all_contextual_predictions_saved_no_gold_read", "files": files,
                "freeze_sha256": filehash(output / "freeze.json"), "diagnostics": dict(totals),
                "cache": dict(encoder.cache_counts), "resources": resources(), "prediction_documents": len(files)}
    atomic(output / "complete.json", complete)
    print(canonical({k:v for k,v in complete.items() if k != "files"}), flush=True)


def score(args):
    bootstrap(args.source_root)
    from nutri_literature import benchmarks as scoring
    bundle, output = read(args.bundle), Path(args.output)
    import inspect
    if sha(inspect.getsource(scoring._span_sets)) != bundle["native_span_scorer_function_sha256"]:
        raise ValueError("Native token scoring implementation differs from the frozen development comparison")
    freeze, complete = read(output / "freeze.json"), read(output / "complete.json")
    if complete["status"] != "all_contextual_predictions_saved_no_gold_read" or complete["freeze_sha256"] != filehash(output / "freeze.json"):
        raise ValueError("Complete contextual predictions required before native gold")
    if freeze["bundle_sha256"] != filehash(args.bundle) or freeze["adapter_sha256"] != filehash(__file__):
        raise ValueError("Contextual input/adapter freeze changed")
    predictions = {}
    for entry in complete["files"]:
        path = output / entry["path"]
        if filehash(path) != entry["sha256"]:
            raise ValueError("Contextual saved prediction changed")
        predictions[(entry["corpus"], entry["pmid"])] = read(path)
    if len(predictions) != len(complete["files"]):
        raise ValueError("Duplicate contextual prediction receipt")
    result = {}
    for corpus in ("ebmnlp", "evidenceoutcomes"):
        selection = bundle["corpora"][corpus]
        wanted = {r["document"]["pmid"] for r in selection["records"]}
        if {p for c,p in predictions if c == corpus} != wanted:
            raise ValueError("Missing contextual predictions")
        # Only exposed development gold is opened, after all predictions exist.
        raw = (Path(args.gold_directory) / corpus / "gold.json").read_bytes()
        if sha(raw) != selection["expected_gold_sha256"]:
            raise ValueError("Native development gold hash mismatch")
        value = json.loads(raw)
        if (value.get("split") != "development" or value.get("corpus") != corpus
                or value.get("schema_version") != "literature-public-gold-v1"
                or value.get("manifest", {}).get("annotation_status") != "published_corpus_annotations"):
            raise ValueError("Sealed/native split not permitted")
        gold = {d["pmid"]: d for d in value["documents"]}
        rows = []
        kinds = ("participants", "interventions", "outcomes") if corpus == "ebmnlp" else ("outcomes",)
        for item in selection["records"]:
            document = item["document"]
            label = gold[document["pmid"]]
            if sha(label["text"]) != document["source_hash"]:
                raise ValueError("Contextual gold/source text mismatch")
            expected, observed = scoring._span_sets(label, predictions[(corpus, document["pmid"])], "annotated_spans", "label", kinds)
            row = {"pmid": document["pmid"]}
            for role in kinds:
                row["spans_" + role] = scoring._metrics({k for k in expected if k[1] == role}, {k for k in observed if k[1] == role})
            rows.append(row)
        report = {"engine": "v8-new-pio-frozen-v6-contextual-diagnostic", "corpus": corpus,
                  "split": "development",
                  "input_sha256": selection["original_input_sha256"], "gold_sha256": sha(raw), "papers": len(rows),
                  "metrics": {"spans_" + k: scoring._aggregate(rows, "spans_" + k) for k in kinds}, "per_paper": rows,
                  "source_position_errors": 0, "full_ico_group_dose_time": "not_evaluated",
                  "upstream_proposal_mismatch": True, "original_outcome_predictions_replayed": False,
                  "independent_final_performance": False, "nutrition_accuracy": "unmeasured"}
        atomic(output / corpus / "report.json", report)
        result[corpus] = {k:v for k,v in report.items() if k != "per_paper"}
    print(canonical(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("predict")
    for key in ("source-root", "bundle", "head-manifest", "encoder", "legacy-cache", "output"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--shared-lock", type=Path, action="append", required=True)
    s = sub.add_parser("score")
    for key in ("source-root", "bundle", "output", "gold-directory"):
        s.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    predict(args) if args.command == "predict" else score(args)


if __name__ == "__main__":
    main()
