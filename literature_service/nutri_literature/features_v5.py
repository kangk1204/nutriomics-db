"""Versioned CPU features. Identities are grouping keys, never learned inputs."""

from collections import Counter, defaultdict, deque
import math
import re

from .clinical_v4 import TOKEN, token_features
from .db import digest, dumps

SEED = 20261009
C_GRID = (0.1, 1.0, 10.0)
THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)
FEATURE_SPEC = {
    "schema": "cpu-features-v5-1",
    "ids_in_features": False,
    "relation": "masked-context-1,2gram/relative-offset/dependency-path/voice/modifiers",
    "span": "surface-shape/masked-neighbours/detector-origin/biological-context",
    "identity": "taxon/case/symbol/full-name/abbreviation/description-jaccard",
    "scope": "token-features/cue-distance/cue-lemma/clause-barrier",
    "bag": "document-pair; mean pooling baseline / maximum-witness latent MIL",
}
FEATURE_SHA = digest(dumps(FEATURE_SPEC))
BIO = re.compile(
    r"\b(?:gene|protein|receptor|kinase|expression|transcription|methylation|enzyme|mutant|knockout)\b", re.I
)


def span_features(mention, text):
    start, end = mention["start"], mention["end"]
    surface = mention["text"]
    before = re.findall(r"\w+", text[max(0, start - 70) : start].lower())[-3:]
    after = re.findall(r"\w+", text[end : end + 70].lower())[:3]
    origin = mention.get("payload", {}).get("origin", "dictionary")
    return {
        "length": min(len(surface), 80),
        "words": len(surface.split()),
        "uppercase": surface.isupper(),
        "titlecase": surface.istitle(),
        "has_digit": bool(re.search(r"\d", surface)),
        "has_hyphen": "-" in surface,
        "origin=" + origin: 1,
        "biological_context": bool(BIO.search(text[max(0, start - 100) : end + 100])),
        **{"before=" + str(i) + ":" + v: 1 for i, v in enumerate(before)},
        **{"after=" + str(i) + ":" + v: 1 for i, v in enumerate(after)},
    }


def identity_features(mention, record, line, taxon, definition=None):
    def words(value):
        return set(re.findall(r"[a-z]+", value.casefold())) - {"the", "of", "and", "protein", "gene"}

    context, description = words(line), words(record["name"])
    surface = definition["long"] if definition else mention["text"]
    return {
        "taxon_match": taxon == record["taxon_id"],
        "taxon_known": taxon is not None,
        "symbol_exact_case": surface == record["symbol"],
        "symbol_casefold": surface.casefold() == record["symbol"].casefold(),
        "name_exact": surface.casefold() == record["name"].casefold(),
        "definition_used": definition is not None,
        "description_similarity": len(context & description) / max(1, len(context | description)),
        "gene_type=" + record["gene_type"]: 1,
        "biological_context": bool(BIO.search(line)),
    }


def _head(tokens, mention):
    local = [t for t in tokens if t["start"] < mention["end"] and mention["start"] < t["end"]]
    ids = {t["i"] for t in local}
    return next(
        (t for t in local if t["head"] not in ids or t["head"] == t["i"]), local[-1] if local else None
    )


def dependency_path(tokens, chemical, gene):
    a, b = _head(tokens, chemical), _head(tokens, gene)
    if a is None or b is None:
        return [], None, None
    graph = defaultdict(list)
    lookup = {t["i"]: t for t in tokens}
    for t in tokens:
        if t["head"] in lookup and t["head"] != t["i"]:
            graph[t["i"]].append((t["head"], "up:" + t["dep"]))
            graph[t["head"]].append((t["i"], "down:" + t["dep"]))
    queue = deque([(a["i"], [])])
    seen = {a["i"]}
    while queue:
        node, path = queue.popleft()
        if node == b["i"]:
            return path, a, b
        for other, label in graph[node]:
            if other not in seen:
                seen.add(other)
                queue.append((other, path + [label]))
    return [], a, b


def relation_features(sentence, chemical, gene, tokens):
    lo, hi = min(chemical["end"], gene["end"]), max(chemical["start"], gene["start"])
    between = sentence["text"][max(0, lo - sentence["start"]) : max(0, hi - sentence["start"])]
    masked = sentence["text"]
    for mention, tag in sorted(((chemical, " CHEMICAL "), (gene, " GENE ")), key=lambda p: -p[0]["start"]):
        a, b = mention["start"] - sentence["start"], mention["end"] - sentence["start"]
        masked = masked[:a] + tag + masked[b:]
    # Do not learn lexical IDs or the target entities' surface names.
    words = re.findall(r"\w+", masked.lower())
    path, a, b = dependency_path(tokens, chemical, gene)
    features = {
        "chem_first": chemical["start"] < gene["start"],
        "distance": math.log1p(max(0, hi - lo)),
        "path_length": len(path),
        "path=" + "/".join(path): 1,
        "chemical_kind=" + chemical["kind"]: 1,
        "gene_kind=" + gene["kind"]: 1,
        "chem_dep=" + (a["dep"] if a else "missing"): 1,
        "gene_dep=" + (b["dep"] if b else "missing"): 1,
        "passive": any(t["dep"] in {"nsubjpass", "auxpass", "agent"} for t in tokens),
        "nominal": bool(
            re.search(r"\b(?:expression|activation|inhibition|regulation|binding)\b", between, re.I)
        ),
        "coordination": bool(re.search(r"\b(?:and|or|but|whereas)\b", between, re.I)),
        "comparison": bool(re.search(r"\b(?:versus|compared|than)\b", masked, re.I)),
        "negation": bool(re.search(r"\b(?:not|no|without|neither|failed)\b", between, re.I)),
        "speculation": bool(re.search(r"\b(?:may|might|could|suggest|possible)\b", masked, re.I)),
    }
    for n in (1, 2):
        for i in range(len(words) - n + 1):
            features["context=" + " ".join(words[i : i + n])] = 1
    for word in re.findall(r"\w+", between.lower()):
        features["between=" + word] = 1
    return features


def pair_candidates(sentences, mentions, parse):
    pairs = []
    for sentence in sentences:
        local = [m for m in mentions if sentence["start"] <= m["start"] < m["end"] <= sentence["end"]]
        genes = [m for m in local if m["kind"] == "gene"]
        chemicals = [m for m in local if m["kind"] in {"component", "nutrient"}]
        if not genes or not chemicals:
            continue
        tokens = parse(sentence)
        for chemical in chemicals:
            for gene in genes:
                if chemical["start"] < gene["end"] and gene["start"] < chemical["end"]:
                    continue
                pairs.append(
                    {
                        "chemical": chemical,
                        "gene": gene,
                        "sentence": sentence,
                        "features": relation_features(sentence, chemical, gene, tokens),
                        "tokens": tokens,
                    }
                )
    return pairs


def pool_features(rows):
    values = Counter()
    for row in rows:
        values.update(row)
    return {k: v / len(rows) for k, v in values.items()} if rows else {}


def scope_features(tokens, index, cue_indices=()):
    base = token_features(tokens, index)
    if cue_indices:
        distance = min(cue_indices, key=lambda i: abs(i - index)) - index
        lo, hi = sorted((index, index + distance))
        base.update(
            {
                "cue_distance": max(-40, min(40, distance)),
                "is_cue": index in cue_indices,
                "cue_word": " ".join(tokens[i]["text"].lower() for i in cue_indices),
                "barrier": any(
                    tokens[i]["text"].lower() in {"but", "however", ";", "."} for i in range(lo, hi)
                ),
            }
        )
    return base


def tokens_for(sentence):
    return [
        {"text": m.group(), "start": sentence["start"] + m.start(), "end": sentence["start"] + m.end()}
        for m in TOKEN.finditer(sentence["text"])
    ]
