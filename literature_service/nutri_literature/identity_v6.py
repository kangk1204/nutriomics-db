"""Separate candidate retrieval, relative ranking, taxon evidence and selection."""

from collections import defaultdict
from array import array
from copy import deepcopy
import re

from .candidates_v4 import SPECIES, term_key
from .candidates_v5 import spelling_keys
from .db import digest, dumps
from .features_v5 import identity_features

SUPPORTED_TAXA = {9606, 10090, 10116}
CELL = re.compile(r"\b(?:HeLa|HEK[- ]?293|HepG2|HUVEC|MCF[- ]?7|cells?|cell lines?)\b", re.I)


def definition_for(definitions, surface, start):
    rows = (
        [row for values in definitions.values() for row in values]
        if isinstance(definitions, dict)
        else definitions
    )
    matches = [
        d
        for d in rows
        if d["short"].casefold() == surface.casefold() and d.get("short_start", d["end"]) <= start
    ]
    return max(matches, key=lambda d: d.get("short_start", d["end"]), default=None)


def taxon_evidence(mention, sentence, sentences, text, metadata):
    sources = [("sentence", sentence["text"])]
    index = next((i for i, s in enumerate(sentences) if s["start"] == sentence["start"]), 0)
    sources.append(
        (
            "adjacent_sentence",
            " ".join(s["text"] for s in sentences[max(0, index - 1) : index + 2] if s is not sentence),
        )
    )
    sources.append(("document", text))
    sources.append(("metadata", " ".join(str(v) for v in metadata.get("mesh", []))))
    evidence = []
    for source, line in sources:
        hits = sorted(t for t, pattern in SPECIES.items() if re.search(pattern, line, re.I))
        if hits:
            evidence.append({"source": source, "taxon_ids": hits})
    explicit = sorted(set(metadata.get("taxon_ids", [])))
    if explicit:
        evidence.append({"source": "explicit_metadata", "taxon_ids": explicit})
    first = evidence[0]["taxon_ids"] if evidence else []
    taxon = first[0] if len(first) == 1 and first[0] in SUPPORTED_TAXA else None
    cell = bool(CELL.search(sentence["text"]))
    model = (
        "cell"
        if cell
        else "animal"
        if taxon in {10090, 10116}
        else "clinical"
        if re.search(r"\b(?:patients?|participants?|volunteers?)\b", sentence["text"], re.I)
        else "unknown"
    )
    return taxon, model, evidence


def grams(term):
    value = " " + term_key(term) + " "
    return {value[i : i + 3] for i in range(max(0, len(value) - 2))}


class CandidateSearch:
    def __init__(self, gene_index):
        self.gene_index = gene_index
        self.exact = defaultdict(lambda: array("I"))
        self.postings = defaultdict(lambda: array("I"))
        self.gram_counts = array("I")
        for i, record in enumerate(gene_index.records):
            terms = [record["symbol"], record["name"], *record["aliases"]]
            combined = set()
            exact_keys = set()
            for term in terms:
                exact_keys.update(spelling_keys(term))
                combined.update(grams(term))
            for key in exact_keys:
                self.exact[key].append(i)
            self.gram_counts.append(len(combined))
            for gram in combined:
                self.postings[gram].append(i)

    def retrieve(self, surface, taxon=None, limit=10):
        exact = set().union(*(self.exact.get(k, set()) for k in spelling_keys(surface)))
        query = grams(surface)
        counts = defaultdict(int)
        if len(term_key(surface)) >= 4 and len(exact) < limit:
            for gram in query:
                for i in self.postings.get(gram, ()):
                    counts[i] += 1
        ranked = sorted(
            exact | set(counts),
            key=lambda i: (
                i not in exact,
                taxon is not None and self.gene_index.records[i]["taxon_id"] != taxon,
                -counts[i] / max(1, len(query) + self.gram_counts[i] - counts[i]),
                self.gene_index.records[i]["id"],
            ),
        )
        return [
            {
                **self.gene_index.records[i],
                "retrieval_exact": i in exact,
                "retrieval_similarity": counts[i] / max(1, len(query) + self.gram_counts[i] - counts[i]),
            }
            for i in ranked[:limit]
        ]


def normalize(mention, sentence, sentences, text, search, metadata, definitions, ranker=None):
    value = deepcopy(mention)
    definition = definition_for(definitions, mention["text"], mention["start"])
    surface = definition["long"] if definition else mention["text"]
    taxon, model, evidence = taxon_evidence(mention, sentence, sentences, text, metadata)
    records = search.retrieve(surface, taxon)
    features = [
        {
            **identity_features(mention, r, sentence["text"], taxon, definition),
            "retrieval_exact": r["retrieval_exact"],
            "retrieval_similarity": r["retrieval_similarity"],
        }
        for r in records
    ]
    scores = (
        ranker.rank(features)
        if ranker is not None and records
        else [
            float(f["retrieval_exact"]) * 3
            + float(f["symbol_exact_case"]) * 2
            + float(f["taxon_match"]) * 2
            + f["description_similarity"]
            + f["retrieval_similarity"]
            for f in features
        ]
    )
    ranked = sorted(zip(records, scores), key=lambda p: (-float(p[1]), p[0]["id"]))
    eligible = [(r, s) for r, s in ranked if r["taxon_id"] == taxon]
    reason = None
    if taxon is None:
        reason = "species_unresolved_or_conflicting"
    elif not eligible:
        reason = "gene_id_not_retrieved"
    elif re.search(r"\b(?:family|genes|proteins)\b", surface, re.I):
        reason = "gene_group"
    elif not eligible[0][0]["retrieval_exact"]:
        reason = "approximate_alias_requires_review"
    elif len(eligible) > 1 and float(eligible[0][1]) - float(eligible[1][1]) < (
        getattr(ranker, "margin", 1.0) if ranker else 1.0
    ):
        reason = "alias_collision"
    record = eligible[0][0] if eligible and reason is None else None
    value.update(
        normalized_id=record["id"] if record else None, status="candidate" if record else "needs_review"
    )
    value.setdefault("payload", {}).update(
        taxon_id=taxon,
        study_model=model,
        taxon_evidence=evidence,
        identity_reason=reason,
        native_id=record["native_id"] if record else None,
        candidate_ids=[r["id"] for r, _ in ranked],
        taxon_compatible_candidate_ids=[r["id"] for r, _ in eligible],
        normalization_rule="relative_ranking_v6",
    )
    return value, {
        "kind": "gene",
        "start": value["start"],
        "end": value["end"],
        "text": value["text"],
        "selected_id": value["normalized_id"],
        "taxon_id": taxon,
        "reason": reason,
        "candidates": [
            {
                "id": r["id"],
                "native_id": r["native_id"],
                "taxon_id": r["taxon_id"],
                "exact": r["retrieval_exact"],
                "priority_score": float(s),
            }
            for r, s in ranked
        ],
        "scores_are_nutrition_probabilities": False,
    }


def cluster_mentions(mentions, definitions, search=None, chemicals=None):
    """One train/inference policy, independent of gold/predicted normalized IDs."""
    grouped = defaultdict(list)
    for mention in mentions:
        surface = mention["text"]
        definition = definition_for(definitions, surface, mention["start"])
        surface = definition["long"] if definition else surface
        # Unknown-taxon mentions are never silently joined to an explicit species.
        taxon = mention.get("payload", {}).get("taxon_id")
        if search is not None and mention["kind"] == "gene" and taxon is not None:
            records = [
                r for r in search.retrieve(surface, taxon) if r["retrieval_exact"] and r["taxon_id"] == taxon
            ]
            if len(records) == 1:
                surface = records[0]["symbol"]
        if chemicals is not None and mention["kind"] in {"component", "nutrient"}:
            records = chemicals.get("index", {}).get(surface.casefold(), [])
            names = {r["name"] for r in records}
            if len(names) == 1:
                surface = next(iter(names))
        key = (mention["kind"], sorted(spelling_keys(surface))[0], taxon)
        grouped[key].append(mention)
    result = []
    for key, members in grouped.items():
        cluster_id = "mention-cluster:" + digest(dumps(key))
        result.append(
            {
                "cluster_id": cluster_id,
                "kind": key[0],
                "taxon_id": key[2],
                "members": [{"start": m["start"], "end": m["end"], "text": m["text"]} for m in members],
                "policy": "surface-definition-official-unique-alias-taxon-v6; IDs not used",
            }
        )
        for mention in members:
            mention.setdefault("payload", {})["mention_cluster_id"] = cluster_id
    return result
