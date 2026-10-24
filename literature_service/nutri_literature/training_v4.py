"""Train-only CPU learners; held-out identifiers are excluded before fitting.

Official test labels are never read here. Development/test source files may be
read only for identifiers and text hashes. Training parameters are fixed.
"""
import json
from pathlib import Path
import re
import time

from .db import digest, dumps, now
from .clinical_v4 import TOKEN, token_features
from .text import sentence_spans


def text_key(text):
    return digest(" ".join(re.findall(r"\w+", text.casefold())))


def rct_documents(path, labels=False):
    records = []
    current = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("###"):
            current = {"pmid": line[3:].strip(), "sentences": []}
            records.append(current)
        elif line.strip():
            tag, text = line.split("\t", 1)
            if current is None:
                raise ValueError("RCT source requires article marker")
            current["sentences"].append({"text": text, **({"label": tag} if labels else {})})
    return records


def exclusions(source_directory, benchmark_directory=None):
    """Read input-only benchmark files and test filename metadata, not labels."""
    from .public_corpora import _archive_members
    sources = Path(source_directory)
    identifiers, texts, records = set(), set(), []
    paths = list(Path(benchmark_directory).rglob("inputs.json")) if benchmark_directory else []
    for path in paths:
        value = json.loads(path.read_bytes())
        documents = value.get("documents", value.get("papers", []))
        # EBM's former "development" release is the entire official training
        # partition. Reserve the actual 200-paper v3 cohort, not all train data.
        # Earlier v2 broad rule evaluation exposure is recorded, not hidden.
        reserve = value.get("corpus") == "ebmnlp" and value.get("split") == "development"
        if reserve:
            documents = sorted(documents, key=lambda d: digest("v3-development-20261003:" + d["pmid"]))[:200]
        for doc in documents:
            identifiers.add(str(doc["pmid"]))
            texts.add(text_key(doc["text"]))
        records.append({"path": str(path), "sha256": digest(path.read_bytes()),
                        "excluded_articles": len(documents), "ebm_train_source_v3_reserved": reserve,
                        "prior_v2_rule_evaluation_exposure": reserve})
    for split in ("dev", "test"):
        path = sources / f"rct-{split}.txt"
        for doc in rct_documents(path, labels=False):
            identifiers.add(doc["pmid"])
            texts.add(text_key(" ".join(s["text"] for s in doc["sentences"])))
        records.append({"path": str(path), "sha256": digest(path.read_bytes()), "labels_read": False})
    raw = sources / "ebm_nlp_2_00.tar.gz"
    for name in _archive_members(raw.read_bytes()):
        match = re.search(r"starting_spans/[^/]+/test/([^/]+)\.ann$", name)
        if match:
            identifiers.add(match[1].removesuffix(".AGGREGATED"))
    for path in sources.glob("*-heldout-ids.txt"):
        identifiers.update(path.read_text(encoding="utf-8").split())
        records.append({"path": str(path), "sha256": digest(path.read_bytes()), "labels_read": False})
    return identifiers, texts, records


def train(source_directory, output_directory, benchmark_directory=None):
    import joblib
    import numpy as np
    import sklearn
    import sklearn_crfsuite
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import SGDClassifier
    from sklearn.pipeline import make_pipeline
    from .public_corpora import _archive_members, _ebm_members

    start = time.monotonic()
    sources, output = Path(source_directory), Path(output_directory)
    if (output / "manifest.json").exists():
        raise ValueError("Training artifacts are immutable; choose a new output directory")
    held_ids, held_text, held_sources = exclusions(sources, benchmark_directory)
    rct = rct_documents(sources / "rct-train.txt", labels=True)
    rct_used, rct_removed = [], []
    for doc in rct:
        target = rct_removed if doc["pmid"] in held_ids or text_key(" ".join(s["text"] for s in doc["sentences"])) in held_text else rct_used
        target.append(doc)
    if len(rct_used) < 1000:
        raise ValueError("Too few independent RCT training articles")
    rows = [sentence for doc in rct_used for sentence in doc["sentences"]]
    role = make_pipeline(TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=150000,
                                       sublinear_tf=True, dtype=np.float64),
                         SGDClassifier(loss="log_loss", alpha=1e-5, max_iter=30, tol=1e-3,
                                       class_weight="balanced", random_state=20261009, n_jobs=1))
    print(dumps({"stage": "role_fit", "articles": len(rct_used), "sentences": len(rows)}), flush=True)
    role.fit([r["text"] for r in rows], [r["label"] for r in rows])
    documents = _ebm_members(_archive_members((sources / "ebm_nlp_2_00.tar.gz").read_bytes()), "development")
    ebm_used, ebm_removed = [], []
    for doc in documents:
        target = ebm_removed if doc["pmid"] in held_ids or text_key(doc["text"]) in held_text else ebm_used
        target.append(doc)
    features, labels = [], {kind: [] for kind in ("participants", "interventions", "outcomes")}
    for doc in ebm_used:
        for sentence in sentence_spans(doc["text"], doc["sections"]):
            tokens = [{"text": m.group(), "start": sentence["start"] + m.start(), "end": sentence["start"] + m.end()}
                      for m in TOKEN.finditer(sentence["text"])]
            if not tokens:
                continue
            features.append([token_features(tokens, i) for i in range(len(tokens))])
            for kind in labels:
                spans = [s for s in doc["annotated_spans"] if s["label"] == kind]
                tags, previous = [], None
                for token in tokens:
                    covering = next(((s["start"], s["end"]) for s in spans
                                     if s["start"] <= token["start"] and token["end"] <= s["end"]), None)
                    tags.append("O" if covering is None else "I" if covering == previous else "B")
                    previous = covering
                labels[kind].append(tags)
    if len(ebm_used) < 1000:
        raise ValueError("Too few independent EBM training articles")
    pico = {}
    for kind in labels:
        print(dumps({"stage": "pico_fit", "kind": kind, "articles": len(ebm_used), "sequences": len(features)}), flush=True)
        model = sklearn_crfsuite.CRF(algorithm="lbfgs", c1=.1, c2=.1, max_iterations=50,
                                    all_possible_transitions=True)
        model.fit(features, labels[kind])
        pico[kind] = model
    output.mkdir(parents=True, exist_ok=True)
    joblib.dump(role, output / "role.joblib")
    joblib.dump(pico, output / "pico.joblib")
    manifest = {"schema": "clinical-cpu-v4", "created_at": now(), "training_split": "train",
                "test_used_for_selection": False, "sklearn_version": sklearn.__version__,
                "training_pmids": {"rct": sorted(d["pmid"] for d in rct_used), "ebm": sorted(d["pmid"] for d in ebm_used)},
                "excluded_pmids": {"rct": sorted(d["pmid"] for d in rct_removed), "ebm": sorted(d["pmid"] for d in ebm_removed)},
                "source_hashes": {name: digest((sources / name).read_bytes())
                                  for name in ("rct-train.txt", "ebm_nlp_2_00.tar.gz")},
                "heldout_source_audit": held_sources, "elapsed_seconds": time.monotonic() - start,
                "parameters": {"role": "tfidf-1,2gram/SGD-log_loss-alpha1e-5-30iter",
                               "pico": "three-independent-CRFs-lbfgs-c1=.1-c2=.1-50iter"},
                "pretraining_overlap": "BiomedBERT/scispaCy pretraining article overlap unknown",
                "files": {name: digest((output / name).read_bytes()) for name in ("role.joblib", "pico.joblib")}}
    (output / "manifest.json").write_text(dumps(manifest), encoding="utf-8")
    return manifest
