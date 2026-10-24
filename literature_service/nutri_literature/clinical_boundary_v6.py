"""Train-only clinical span-boundary reranking; frozen strict ICO evaluation."""

from bisect import bisect_left, bisect_right
from collections import Counter
import math
from pathlib import Path
import re

from .db import digest, dumps, now
from .refinement_v6 import (
    clinical_role_candidates,
    literal_matches,
    read_training,
    tokens,
    words,
)
from .clinical_refinement_v6 import STOP, arm_key, distinct_arms, MemoEncoder
from .text import sentence_spans

VERSION = "clinical-boundary-v6-r2"
SHIFTS = (-12, -8, -4, -2, -1, 0, 1, 2, 4, 8)


def implementation_sha():
    return digest(Path(__file__).read_bytes())


def span_features(text, sequence, lo, hi, role):
    value = sequence[lo:hi]
    surface = text[value[0]["start"] : value[-1]["end"]]
    before = sequence[lo - 1]["text"] if lo else "BOS"
    after = sequence[hi]["text"] if hi < len(sequence) else "EOS"
    row = {
        "role": role,
        "length": math.log1p(hi - lo),
        "first": value[0]["text"].casefold(),
        "last": value[-1]["text"].casefold(),
        "before": before.casefold(),
        "after": after.casefold(),
        "first_upper": value[0]["text"].isupper(),
        "last_upper": value[-1]["text"].isupper(),
        "starts_article": value[0]["text"].casefold() in {"the", "a", "an"},
        "starts_measurement": value[0]["text"].casefold()
        in {"risk", "mean", "total", "duration", "concentration", "change", "rate", "score", "incidence"},
        "has_number": any(t["text"].isdigit() for t in value),
        "dose": bool(re.search(r"\b(?:mg|kg|mcg|ug|μg|g|ml|iu)\b|%", surface, re.I)),
        "frequency": bool(re.search(r"\b(?:daily|weekly|once|twice|day)\b", surface, re.I)),
        "parenthesis_balance": surface.count("(") - surface.count(")"),
        "list_separator": ";" in surface,
        "comma": "," in surface,
        "leading_boundary": text[sequence[lo - 1]["end"] : value[0]["start"]] if lo else "",
        "trailing_boundary": text[value[-1]["end"] : sequence[hi]["start"]] if hi < len(sequence) else "",
    }
    for term in words(surface):
        row["inside=" + term] = 1.0
    return row


def variants(lo, hi, size, sentence_bounds=None):
    allowed = {(lo, hi)}
    for shift in range(-12, 9):
        allowed.add((lo + shift, hi))
        allowed.add((lo, hi + shift))
    for left in (-3, -2, -1, 1, 2, 3):
        for right in (-3, -2, -1, 1, 2, 3):
            allowed.add((lo + left, hi + right))
    return sorted(
        (a, b)
        for a, b in allowed
        if 0 <= a < b <= size
        and b - a <= 50
        and (sentence_bounds is None or sentence_bounds[0] <= a < b <= sentence_bounds[1])
    )


class BoundaryHead:
    def fit(self, rows, labels):
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.linear_model import LogisticRegression

        self.vectorizer = DictVectorizer()
        matrix = self.vectorizer.fit_transform(rows)
        self.model = LogisticRegression(
            C=1.0, class_weight="balanced", solver="liblinear", max_iter=250, random_state=20261010
        ).fit(matrix, labels)
        return self

    def scores(self, rows):
        if not rows:
            return []
        return self.model.predict_proba(self.vectorizer.transform(rows))[
            :, list(self.model.classes_).index(1)
        ].tolist()


def train(data_path, audit_path, output, role_manifest):
    import json
    import joblib

    data, _ = read_training(data_path, audit_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "manifest.json").exists():
        raise ValueError("Boundary artifact directory must be new")
    manifest = {
        "schema": "clinical-boundary-v6-r2-1",
        "created_at": now(),
        "version": VERSION,
        "source_sha256": implementation_sha(),
        "training_sha256": digest(Path(data_path).read_bytes()),
        "audit_sha256": digest(Path(audit_path).read_bytes()),
        "training_split": "train",
        "training_pmids": [d["pmid"] for d in data["documents"]],
        "test_used_for_selection": False,
        "upstream_role_manifest_sha256": digest(Path(role_manifest).read_bytes()),
        "weak_supervision": "exact training prompt/source alignments; boundary perturbations are negatives unless another positive alignment",
        "parameters": {
            "C": 1.0,
            "selection": "fixed before development scoring; no dev threshold tuning",
            "max_occurrences_per_description": 2,
            "max_words": 50,
        },
        "files": {},
        "counts": {},
    }
    for role in ("arms", "outcome"):
        rows, labels = [], []
        for doc in data["documents"]:
            text = doc["text"]
            sequence = tokens(text)
            fields = ("intervention", "comparator") if role == "arms" else ("outcome",)
            names = {c[k + "_description"] for c in doc.get("comparisons", []) for k in fields}
            positives = set(literal_matches(sequence, names))
            selected = set()
            for name in sorted(names):
                selected.update(literal_matches(sequence, [name])[:2])
            candidates = set()
            for lo, hi in selected:
                candidates.add((lo, hi))
                for shift in SHIFTS:
                    if 0 <= lo + shift < hi <= len(sequence):
                        candidates.add((lo + shift, hi))
                    if 0 <= lo < hi + shift <= len(sequence):
                        candidates.add((lo, hi + shift))
                for left, right in ((-1, 1), (1, -1), (-2, 2), (2, -2)):
                    if 0 <= lo + left < hi + right <= len(sequence):
                        candidates.add((lo + left, hi + right))
            for lo, hi in sorted(candidates):
                if hi - lo > 50:
                    continue
                rows.append(span_features(text, sequence, lo, hi, role))
                labels.append(int((lo, hi) in positives))
        head = BoundaryHead().fit(rows, labels)
        path = output / (role + ".joblib")
        joblib.dump(head, path)
        manifest["files"][path.name] = digest(path.read_bytes())
        manifest["counts"][role] = {
            "rows": len(rows),
            "positive": sum(labels),
            "negative": len(labels) - sum(labels),
        }
        print(dumps({"stage": "boundary_fit", "role": role, **manifest["counts"][role]}), flush=True)
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    return manifest


def load(path):
    import json
    import joblib

    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    if manifest["source_sha256"] != implementation_sha() or manifest["training_split"] != "train":
        raise ValueError("Boundary source/training identity differs")
    models = {}
    for name, sha in manifest["files"].items():
        if Path(name).name != name or digest((path / name).read_bytes()) != sha:
            raise ValueError("Boundary weights differ")
        models[name.removesuffix(".joblib")] = joblib.load(path / name)
    return models, manifest


def expanded_roles(document, role_models, role_manifest, boundary):
    text = document["text"]
    sequence = tokens(text)
    starts = [t["start"] for t in sequence]
    ends = [t["end"] for t in sequence]
    found = clinical_role_candidates(text, role_models, role_manifest)
    seeds = {"arms": set(), "outcome": set()}
    for role, values in found.items():
        group = "outcome" if role == "outcome" else "arms"
        for mentions in values.values():
            for m in mentions:
                seeds[group].add((bisect_left(starts, m["start"]), bisect_right(ends, m["end"])))
    # New drug acronyms with an explicit dose can be absent from the CRF vocabulary.
    # These are source anchors only; learned boundaries still rank the full phrase.
    for match in re.finditer(r"\b[A-Za-z][\w-]*\s+\d+(?:\.\d+)?\s*(?:mg|g|μg|mcg|IU|%)", text, re.I):
        seeds["arms"].add((bisect_left(starts, match.start()), bisect_right(ends, match.end())))
    sentence_ranges = [
        (bisect_left(starts, s["start"]), bisect_right(ends, s["end"]))
        for s in sentence_spans(text, document["sections"])
    ]
    ranked = {}
    for role in ("arms", "outcome"):
        candidates = set()
        for lo, hi in seeds[role]:
            bounds = next((p for p in sentence_ranges if p[0] <= lo < hi <= p[1]), None)
            if bounds is not None:
                candidates.update(variants(lo, hi, len(sequence), bounds))
        candidates = sorted(candidates)
        scores = boundary[role].scores([span_features(text, sequence, a, b, role) for a, b in candidates])
        unique = {}
        for (lo, hi), score in zip(candidates, scores):
            start, end = sequence[lo]["start"], sequence[hi - 1]["end"]
            surface = text[start:end]
            key = tuple(words(surface))
            value = {"text": surface, "start": start, "end": end, "score": score, "origin": VERSION}
            if key not in unique or score > unique[key]["score"]:
                unique[key] = value
        ranked[role] = sorted(unique.values(), key=lambda x: (-x["score"], x["start"]))

    # Explicit control arms are never emitted as their own active intervention.
    def control(value):
        return bool(
            re.search(
                r"\b(?:placebo|control|controls|usual care|standard care|no treatment)\b",
                value["text"],
                re.I,
            )
        )

    arms = ranked["arms"]
    intervention = [x for x in arms if not control(x)][:8]
    comparator = [x for x in arms if control(x)][:5] or arms[:5]
    # Preserve multiple boundary hypotheses per distinct arm rather than allowing
    # the most frequent short chemical name to erase dose/abbreviation variants.
    return {"intervention": intervention, "comparator": comparator, "outcome": ranked["outcome"][:20]}, {
        "raw_seed_counts": {k: len(v) for k, v in seeds.items()},
        "boundary_candidates": {k: len(v) for k, v in ranked.items()},
    }


def comparisons(document, role_models, role_manifest, boundary, evidence_head, threshold, encoder):
    from .clinical_v6 import state
    from .clinical_training_v6 import inference_features

    roles, trace = expanded_roles(document, role_models, role_manifest, boundary)
    text = document["text"]
    sentences = sentence_spans(text, document["sections"])
    observed = [s for s in sentences if state(s, {})[0] == "observed"]
    encoder = MemoEncoder(encoder)
    found = []
    counters = Counter()
    for intervention in roles["intervention"]:
        for comparator in roles["comparator"]:
            if not distinct_arms(intervention["text"], comparator["text"]):
                continue
            for outcome in roles["outcome"]:
                terms = set(words(outcome["text"])) - STOP
                if not terms or arm_key(outcome["text"]) in {
                    arm_key(intervention["text"]),
                    arm_key(comparator["text"]),
                }:
                    continue
                eligible = []
                for s in observed:
                    surface = set(words(s["text"]))
                    overlap = len(terms & surface) / len(terms)
                    arms = set(arm_key(intervention["text"])) | set(arm_key(comparator["text"]))
                    arms = {w for w in arms if not w.isdigit() and len(w) > 2}
                    if overlap >= 0.6 and arms & surface:
                        eligible.append((overlap, s))
                eligible = sorted(eligible, key=lambda x: (-x[0], x[1]["start"]))[:4]
                if not eligible:
                    continue
                prompt = {
                    "intervention_description": intervention["text"],
                    "comparator_description": comparator["text"],
                    "outcome_description": outcome["text"],
                }
                scores = evidence_head.probabilities(
                    [inference_features(text, s, prompt, encoder) for _, s in eligible]
                )
                evidence = [
                    {"start": s["start"], "end": s["end"], "quote": s["text"]}
                    for (_, s), score in zip(eligible, scores)
                    if score.get("1", 0) >= threshold
                ]
                counters["triples_scored"] += 1
                if evidence:
                    found.append(
                        {
                            **prompt,
                            "evidence": evidence,
                            "source_roles": {
                                "intervention": intervention,
                                "comparator": comparator,
                                "outcome": outcome,
                            },
                            "status": "needs_review",
                            "source_only": True,
                        }
                    )
    return found, {
        **trace,
        **counters,
        "retained_roles": {k: len(v) for k, v in roles.items()},
        "source_role_candidates": roles,
    }
