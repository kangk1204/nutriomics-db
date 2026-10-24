"""Cache exactly equivalent individual span means from one sentence forward pass."""

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def aliases(encoder, text, sentence, candidates):
    import numpy as np
    from nutri_literature.db import digest, dumps
    from nutri_literature.nlp_v2 import cpu_inference_lock

    candidates = list({(q["start"], q["end"]): q for q in candidates}.values())
    if not candidates:
        return 0
    groups = [(), *((q,) for q in candidates)]
    keys = [
        digest(
            dumps(
                {
                    "source": digest(text),
                    "sentence": [sentence["start"], sentence["end"]],
                    "entities": [(e["start"], e["end"]) for e in group],
                    "encoder": encoder.manifest,
                }
            )
        )
        for group in groups
    ]
    if all(
        (encoder.cache / (key + ".npy")).exists() and (encoder.cache / (key + ".json")).exists()
        for key in keys[1:]
    ):
        return 0
    values = encoder.encode(text, sentence, tuple(candidates))
    added = 0
    for index, key in enumerate(keys):
        value = values[[0]] if index == 0 else values[[0, index]]
        array, receipt = (encoder.cache / (key + suffix) for suffix in (".npy", ".json"))
        with cpu_inference_lock(encoder.cache / (key + ".lock")):
            if array.exists() and receipt.exists():
                record = json.loads(receipt.read_bytes())
                if (
                    record["key"] != key
                    or digest(array.read_bytes()) != record["sha256"]
                    or not np.array_equal(np.load(array, allow_pickle=False), value)
                ):
                    raise ValueError("Batched span mean differs from an existing individual forward pass")
                continue
            temporary = encoder.cache / (uuid.uuid4().hex + ".alias-tmp")
            with temporary.open("wb") as stream:
                np.save(stream, value, allow_pickle=False)
            temporary.replace(array)
            raw = dumps(
                {
                    "key": key,
                    "sha256": digest(array.read_bytes()),
                    "shape": list(value.shape),
                    "manifest": encoder.manifest,
                }
            )
            temporary = encoder.cache / (uuid.uuid4().hex + ".alias-tmp")
            temporary.write_text(raw, encoding="utf-8")
            temporary.replace(receipt)
            added += 1
    return added


def main():
    from nutri_literature import benchmarks as scoring
    from nutri_literature.clinical_v4 import ClinicalModels
    from nutri_literature.clinical_v6 import clauses, outcome_candidates, state
    from nutri_literature.db import digest, dumps
    from nutri_literature.embeddings_v6 import FrozenEncoder
    from nutri_literature.nlp_v2 import parser_for, validate_tokens
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.settings import Settings
    from nutri_literature.text import sentence_spans

    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--corpus", nargs="+", choices=("evidenceoutcomes", "ebmnlp", "bioredirect"),
                        default=("evidenceoutcomes", "ebmnlp"))
    args = parser.parse_args()
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    index = json.loads((settings.home / "public_benchmarks/v2-measurement-index.json").read_bytes())
    with memory_budget(), processing_slot(settings):
        encoder = FrozenEncoder(args.models / "gene/selected_model", settings.home / "embedding-cache-v6")
        encoder.load()
        if args.self_check:
            import numpy as np

            text = "Caffeine reduced VDR expression in α cells."
            sentence = {"start": 0, "end": len(text), "text": text}
            entities = [
                {"start": text.index(name), "end": text.index(name) + len(name), "text": name}
                for name in ("Caffeine", "VDR")
            ]
            aliases(encoder, text, sentence, entities)
            identical = [
                np.array_equal(
                    encoder._compute(sentence, (entity,)), encoder.encode(text, sentence, (entity,))
                )
                for entity in entities
            ]
            if not all(identical):
                raise ValueError("Individual and prewarmed frozen vectors differ")
            receipt = {
                "individual_vectors_identical": identical,
                "weights_updated": False,
                "encoder": encoder.manifest,
            }
            (settings.home / "v6/validation/individual-cache-alias-smoke.json").write_text(dumps(receipt))
            print(dumps(receipt), flush=True)
        clinical = ClinicalModels(args.models / "clinical")
        parsing = parser_for({})
        for corpus in args.corpus:
            prepared = index["steps"]["prepare:development:" + corpus + ":abstract"]
            inputs, _, _ = scoring._load_inputs(settings, prepared["inputs_path"])
            if inputs["split"] != "development":
                raise ValueError("Only source-only development inputs are allowed")
            docs = sorted(inputs["documents"], key=lambda d: digest("v3-development-20261003:" + d["pmid"]))[
                :(100 if corpus == "bioredirect" else 200)
            ]
            # Warm future papers first; retain the identical source-only cohort.
            docs = list(reversed(docs))
            added = 0
            for count, doc in enumerate(docs, 1):
                for sentence in clinical.predict(sentence_spans(doc["text"], doc["sections"])):
                    tokens = validate_tokens(parsing.parse(sentence), sentence)
                    added += aliases(encoder, doc["text"], sentence, outcome_candidates(sentence, tokens))
                    for clause in clauses(sentence):
                        if state(clause, {})[0] == "observed":
                            added += aliases(encoder, doc["text"], clause, outcome_candidates(clause, tokens))
                if count % 10 == 0:
                    print(
                        dumps({"corpus": corpus, "prewarmed_papers": count, "cache_aliases_added": added}),
                        flush=True,
                    )
        print(
            dumps(
                {
                    "prewarming_complete": True,
                    "weights_updated": False,
                    "labels_read": False,
                    "equivalence": "identical single-sentence hidden states and entity mean pooling",
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    locks = Path("/home/keunsoo/projects/nutriomics-literature/runtime/locks")
    os.environ["LITERATURE_NLP_LOCK"] = str(locks / "nlp-cpu.lock")
    os.environ["LITERATURE_PROCESSING_LOCK_HOME"] = str(locks)
    os.nice(10)
    main()
