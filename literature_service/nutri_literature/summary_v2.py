"""Deterministic TF-IDF MMR; selected source sentences are never paraphrased."""

from collections import Counter
import math
import re
from .extraction import UNKNOWN, _PRIOR, _METHOD, _LIMIT, _CUE, _NULL, _STAT


def _vectors(sentences):
    counts = [Counter(re.findall(r"\w+", sentence["text"].casefold())) for sentence in sentences]
    df = Counter(word for row in counts for word in row)
    vectors = []
    for row in counts:
        vector = {
            word: (1 + math.log(count)) * (1 + math.log((len(counts) + 1) / (df[word] + 1)))
            for word, count in row.items()
        }
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1
        vectors.append({word: value / norm for word, value in vector.items()})
    return vectors


def _cosine(left, right):
    return sum(value * right.get(word, 0) for word, value in left.items())


def summarize(sentences, events, mmr_lambda=0.7):
    usable = [
        s
        for s in sentences
        if not re.search(r"references?|bibliography", s["label"], re.I) and not _PRIOR.search(s["text"])
    ]
    methods = [
        s for s in usable if re.search(r"methods?|materials?", s["label"], re.I) or _METHOD.search(s["text"])
    ]
    limits = [s for s in usable if _LIMIT.search(s["text"])]
    event_spans = {
        (q["start"], q["end"]) for e in events if e.get("status") == "candidate" for q in e["evidence_quotes"]
    }
    candidates = []
    for s in usable:
        if re.search(r"introduction|background|methods?|materials?", s["label"], re.I) or _LIMIT.search(
            s["text"]
        ):
            continue
        score = (
            3 * bool(re.search(r"results?|conclusions?|findings", s["label"], re.I))
            + 2 * ((s["start"], s["end"]) in event_spans)
            + 2 * bool(_CUE.search(s["text"]) or _NULL.search(s["text"]))
            + bool(_STAT.search(s["text"]))
        )
        if score >= 2:
            candidates.append({**s, "rule_score": int(score)})
    vectors = _vectors(candidates)
    selected = []
    # Keep explicit null/non-significant observations if present, without
    # fabricating an outcome or assuming nonsignificance means no effect.
    nulls = [i for i, s in enumerate(candidates) if _NULL.search(s["text"])]
    if nulls:
        selected.append(min(nulls, key=lambda i: (-candidates[i]["rule_score"], candidates[i]["start"])))
    maximum = max((s["rule_score"] for s in candidates), default=1)
    while len(selected) < min(4, len(candidates)):
        remaining = [i for i in range(len(candidates)) if i not in selected]
        # Exact duplicates keep one result quote. All event evidence still
        # survives outside summary, including repeated confirmations.
        remaining = [
            i for i in remaining if not any(candidates[i]["text"] == candidates[j]["text"] for j in selected)
        ]
        if not remaining:
            break

        def score(index):
            relevance = candidates[index]["rule_score"] / maximum
            redundancy = max((_cosine(vectors[index], vectors[j]) for j in selected), default=0)
            return mmr_lambda * relevance - (1 - mmr_lambda) * redundancy

        selected.append(min(remaining, key=lambda i: (-score(i), candidates[i]["start"])))
    return {
        "labels": {"method": "연구설계", "results": "주요 결과", "limitations": "한계"},
        "method": methods[0] if methods else UNKNOWN,
        "results": [candidates[i] for i in sorted(selected, key=lambda i: candidates[i]["start"])],
        "limitations": limits[0] if limits else UNKNOWN,
        "status": "extractive_candidate",
        "missing_results": not bool(selected),
        "notice": "자동 추출 후보입니다. 원문·연구설계·통계 해석을 검수해야 합니다.",
        "selection": {
            "method": "tfidf-mmr",
            "lambda": mmr_lambda,
            "null_result_reserved": bool(nulls),
            "candidate_sentences": len(candidates),
        },
    }
