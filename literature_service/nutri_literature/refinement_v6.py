"""Experimental v6-r1: joint relation decisions and source-aligned clinical roles.

The frozen v6 feature extractors, weights and evaluators remain unchanged.
Nothing in this module reads development annotations during fitting/inference.
"""

from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import re

from .db import digest, dumps, now

VERSION = "heuristic-v6-r1"
ROLES = ("intervention", "comparator", "outcome")
WORD = re.compile(r"\w+", re.UNICODE)
SEED = 20261010


def source_identity():
    return digest(Path(__file__).read_bytes())


def words(text):
    return [m.group().casefold() for m in WORD.finditer(text)]


def tokens(text):
    return [{"text": m.group(), "start": m.start(), "end": m.end()} for m in WORD.finditer(text)]


def literal_matches(sequence, descriptions):
    """Return exact token-sequence alignments, without inventing span gold."""
    positions = defaultdict(list)
    values = [t["text"].casefold() for t in sequence]
    for i, value in enumerate(values):
        positions[value].append(i)
    result = set()
    for description in descriptions:
        query = words(description)
        if not query:
            continue
        for i in positions.get(query[0], []):
            if values[i : i + len(query)] == query:
                result.add((i, i + len(query)))
    return sorted(result)


def windows(sequence, size=96):
    for start in range(0, len(sequence), size):
        yield start, sequence[start : start + size]


def sequence_features(sequence, offset=0):
    result = []
    for i, token in enumerate(sequence):
        text = token["text"]
        row = {
            "bias": 1.0,
            "lower": text.casefold(),
            "suffix3": text[-3:].casefold(),
            "prefix3": text[:3].casefold(),
            "digit": text.isdigit(),
            "upper": text.isupper(),
            "title": text.istitle(),
            "paper_position": str(min((offset + i) // 500, 20)),
        }
        for step in (-2, -1, 1, 2):
            j = i + step
            if 0 <= j < len(sequence):
                row[f"{step}:lower"] = sequence[j]["text"].casefold()
            else:
                row[f"{step}:boundary"] = True
        result.append(row)
    return result


def read_training(path, audit_path):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    audit = json.loads(Path(audit_path).read_bytes())
    if (
        data.get("split") != "train"
        or not audit.get("passed")
        or audit.get("protected_overlap") != 0
        or audit.get("audited_training_sha256") != digest(raw)
        or {str(d["pmid"]) for d in data["documents"]} != set(audit["training_pmids"])
    ):
        raise ValueError("Refinement requires the exact audited official training cohort")
    return data, audit


class JointRelationHead:
    """One distribution including NONE; label combinations must occur in training."""

    def fit(self, features, labels, c=1.0):
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.preprocessing import MaxAbsScaler
        from sklearn.linear_model import LogisticRegression

        self.vectorizer = DictVectorizer()
        matrix = self.vectorizer.fit_transform(features)
        self.scaler = MaxAbsScaler().fit(matrix)
        self.model = LogisticRegression(C=c, random_state=SEED, max_iter=1200).fit(
            self.scaler.transform(matrix), labels
        )
        return self

    def predict(self, features):
        if not features:
            return []
        matrix = self.scaler.transform(self.vectorizer.transform(features))
        return [dict(zip(self.model.classes_, map(float, row))) for row in self.model.predict_proba(matrix)]


def selected_types(probabilities, threshold):
    best = max(probabilities, key=probabilities.get)
    return best.split("|") if best != "NONE" and probabilities[best] >= threshold else []


def train_relations(export, audit_path, output):
    import joblib
    from .training_v6 import folds
    from .benchmarks import _metrics

    data = json.loads(gzip.decompress(Path(export).read_bytes()))
    source = data["source_features_manifest"]
    audit = json.loads(Path(audit_path).read_bytes())
    if (
        source["split"] != "train"
        or not audit["passed"]
        or audit["protected_overlap"] != 0
        or source["audit_sha256"] != digest(Path(audit_path).read_bytes())
        or set(audit["training_pmids"]) != {d["pmid"] for d in data["papers"]}
    ):
        raise ValueError("Relation export and protected training audit differ")
    samples = [(d["pmid"], bag) for d in data["papers"] for bag in d["bags"]]
    assignment = folds([p for p, _ in samples])
    x = [b["features"] for _, b in samples]
    labels = ["|".join(sorted(b["labels"])) or "NONE" for _, b in samples]
    expected = {(i, t) for i, (_, b) in enumerate(samples) for t in b["labels"]}
    configurations = []
    for c in (0.1, 1.0, 10.0):
        scores = [None] * len(samples)
        for fold in range(5):
            fit = [i for i, (p, _) in enumerate(samples) if assignment[p] != fold]
            held = [i for i, (p, _) in enumerate(samples) if assignment[p] == fold]
            head = JointRelationHead().fit([x[i] for i in fit], [labels[i] for i in fit], c)
            for i, score in zip(held, head.predict([x[i] for i in held])):
                scores[i] = score
        for threshold in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
            observed = {(i, t) for i, score in enumerate(scores) for t in selected_types(score, threshold)}
            metric = _metrics(expected, observed)
            configurations.append({"C": c, "threshold": threshold, **metric})
    eligible = [c for c in configurations if c["tp"] and c["precision"] >= 0.85]
    selected = max(
        eligible or configurations,
        key=lambda c: (c["recall"] if eligible else c["f1"], c["f1"], -c["C"], c["threshold"]),
    )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise ValueError("Refinement artifact output must be new")
    model = JointRelationHead().fit(x, labels, selected["C"])
    joblib.dump(model, output / "joint-relation.joblib")
    manifest = {
        "schema": "joint-relation-v6-r1-1",
        "created_at": now(),
        "engine": VERSION,
        "source_sha256": source_identity(),
        "training_split": "train",
        "test_used_for_selection": False,
        "training_pmids": sorted(assignment),
        "training_export_sha256": digest(Path(export).read_bytes()),
        "source_features_manifest_sha256": data["source_features_manifest_sha256"],
        "audit_sha256": digest(Path(audit_path).read_bytes()),
        "selection": selected,
        "selection_source": "5-fold paper-separated training bags; no development labels",
        "target_precision_met": bool(eligible),
        "configurations": configurations,
        "samples": len(samples),
        "class_counts": dict(Counter(labels)),
        "fold_assignments": assignment,
        "files": {"joint-relation.joblib": digest((output / "joint-relation.joblib").read_bytes())},
    }
    (output / "manifest.json").write_text(dumps(manifest))
    return manifest


def automatic_relation_eligible(hypothesis, oracle=False, require_selected_span=False):
    reasons = []
    for kind in ("chemical", "gene"):
        entity = hypothesis[kind]
        supplied = oracle and entity.get("payload", {}).get("provided_native_ids")
        if not entity.get("normalized_id") and not supplied:
            reasons.append(kind + "_identity_unresolved")
    if (
        require_selected_span
        and not oracle
        and not hypothesis["gene"].get("payload", {}).get("automatic_selected", False)
    ):
        reasons.append("gene_boundary_not_automatically_selected")
    return not reasons, reasons


def train_clinical_roles(data_path, audit_path, output):
    import joblib
    import sklearn_crfsuite

    data, _ = read_training(data_path, audit_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise ValueError("Clinical refinement artifact output must be new")
    manifest = {
        "schema": "clinical-description-roles-v6-r1-1",
        "created_at": now(),
        "engine": VERSION,
        "source_sha256": source_identity(),
        "training_split": "train",
        "test_used_for_selection": False,
        "training_sha256": digest(Path(data_path).read_bytes()),
        "audit_sha256": digest(Path(audit_path).read_bytes()),
        "training_pmids": sorted(str(d["pmid"]) for d in data["documents"]),
        "weak_supervision": "exact source token alignment of official training prompt descriptions",
        "unmatched_descriptions_are_span_gold": False,
        "parameters": {
            "c1": 0.1,
            "c2": 0.1,
            "max_iterations": 35,
            "span_priority_threshold": 0.5,
            "source_window_words": 96,
            "hyperparameters_selected_on_development": False,
        },
        "roles": {},
        "files": {},
    }
    for role in ROLES:
        features, tags, phrases = [], [], Counter()
        matched = descriptions = 0
        for doc in data["documents"]:
            sequence = tokens(doc["text"])
            names = {c[role + "_description"] for c in doc.get("comparisons", [])}
            descriptions += len(names)
            alignments = literal_matches(sequence, names)
            phrases.update(" ".join(words(name)) for name in names if literal_matches(sequence, [name]))
            matched += sum(bool(literal_matches(sequence, [name])) for name in names)
            # Longest non-overlapping positive span. Other descriptions remain
            # weak/unannotated rather than literal gold for quantitative claims.
            labels = ["O"] * len(sequence)
            for lo, hi in sorted(alignments, key=lambda p: (-(p[1] - p[0]), p[0])):
                if any(v != "O" for v in labels[lo:hi]):
                    continue
                labels[lo] = "B"
                labels[lo + 1 : hi] = ["I"] * (hi - lo - 1)
            negative = 0
            for offset, chunk in windows(sequence):
                target = labels[offset : offset + len(chunk)]
                if not any(t != "O" for t in target):
                    # Deterministic source-only negative sampling, bounded per paper.
                    if negative >= 2 or int(digest(doc["pmid"] + ":" + str(offset))[:8], 16) % 11:
                        continue
                    negative += 1
                features.append(sequence_features(chunk, offset))
                tags.append(target)
        model = sklearn_crfsuite.CRF(
            algorithm="lbfgs",
            c1=0.1,
            c2=0.1,
            max_iterations=35,
            all_possible_transitions=True,
        ).fit(features, tags)
        path = output / (role + ".joblib")
        joblib.dump(model, path)
        manifest["files"][path.name] = digest(path.read_bytes())
        manifest["roles"][role] = {
            "sequences": len(features),
            "aligned_descriptions": matched,
            "descriptions": descriptions,
            "training_phrases": dict(phrases),
        }
        print(
            dumps(
                {
                    "stage": "clinical_role_fit",
                    "role": role,
                    "sequences": len(features),
                    "aligned_descriptions": matched,
                }
            ),
            flush=True,
        )
    (output / "manifest.json").write_text(dumps(manifest))
    return manifest


def load_artifact(path):
    import joblib

    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    if manifest["source_sha256"] != source_identity() or manifest["training_split"] != "train":
        raise ValueError("Refinement source or training contract differs")
    models = {}
    for name, sha in manifest["files"].items():
        if Path(name).name != name or digest((path / name).read_bytes()) != sha:
            raise ValueError("Refinement weights differ")
        models[name.removesuffix(".joblib")] = joblib.load(path / name)
    return models, manifest


def clinical_role_candidates(text, models, manifest):
    sequence = tokens(text)
    found = {role: {} for role in ROLES}
    for role in ROLES:
        for offset, chunk in windows(sequence):
            features = sequence_features(chunk, offset)
            tags = models[role].predict_single(features)
            marginal = models[role].predict_marginals_single(features)
            begin = None
            for i, tag in enumerate(list(tags) + ["O"]):
                if begin is not None and tag != "I":
                    start, end = chunk[begin]["start"], chunk[i - 1]["end"]
                    score = sum(marginal[j].get(tags[j], 0) for j in range(begin, i)) / (i - begin)
                    if score >= manifest["parameters"]["span_priority_threshold"]:
                        surface = text[start:end]
                        key = tuple(words(surface))
                        found[role].setdefault(key, []).append(
                            {
                                "text": surface,
                                "start": start,
                                "end": end,
                                "score": score,
                                "origin": "train-aligned-description-crf-v6-r1",
                            }
                        )
                    begin = None
                if tag in {"B", "I"} and begin is None:
                    begin = i
        phrases = manifest["roles"][role]["training_phrases"]
        known = [p for p, count in phrases.items() if len(words(p)) > 1 or count >= 2]
        for lo, hi in literal_matches(sequence, known):
            start, end = sequence[lo]["start"], sequence[hi - 1]["end"]
            surface = text[start:end]
            found[role].setdefault(tuple(words(surface)), []).append(
                {
                    "text": surface,
                    "start": start,
                    "end": end,
                    "score": 1.0,
                    "origin": "training-only-description-lexicon-v6-r1",
                }
            )
    return found
