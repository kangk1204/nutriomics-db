"""Candidate union, learned span filtering and conservative native-ID hypotheses."""

from copy import deepcopy
import re

from .candidates_v4 import CpuCandidates, collect_candidates, taxon_context, term_key
from .features_v5 import identity_features, span_features


def spelling_keys(surface):
    """Only orthographic variants, never chemical identity or species inference."""
    greek = str.maketrans(
        {
            "α": "alpha",
            "β": "beta",
            "γ": "gamma",
            "δ": "delta",
            "κ": "kappa",
            "Α": "alpha",
            "Β": "beta",
            "Γ": "gamma",
        }
    )
    translated = surface.translate(greek)
    compact = re.sub(r"(?<=[A-Za-z])[- ](?=\d)|(?<=\d)[- ](?=[A-Za-z])", "", translated)
    return {term_key(surface), term_key(translated), term_key(compact)}


def lookup(surface, index):
    records = {}
    for key in spelling_keys(surface):
        for record in index.index.get(key, []):
            records[record["id"]] = record
    return list(records.values())


class UnionCandidates:
    def __init__(self, home, provider="union"):
        names = ("biomedbert", "bionlp13cg") if provider == "union" else (provider,)
        self.providers = [CpuCandidates(home, name) for name in names]
        self.manifest = {"available": False, "gene_provider": provider, "device": "cpu"}

    def load(self):
        for provider in self.providers:
            provider.load()
        self.manifest.update(available=True, members=[p.manifest for p in self.providers])

    def predict(self, text):
        self.load()
        merged = {}
        for provider in self.providers:
            for mention in provider.predict(text):
                key = (mention["kind"], mention["start"], mention["end"])
                if key in merged:
                    merged[key]["payload"]["origin"] = "union"
                else:
                    merged[key] = deepcopy(mention)
        return list(merged.values())


def gene_hypotheses(mention, sentence, text, index, metadata, definitions, learned):
    value = deepcopy(mention)
    payload = value.setdefault("payload", {})
    taxon, model, source = taxon_context(sentence["text"], text, metadata)
    definitions = [d for d in definitions.get(value["text"], []) if d["short_start"] <= value["start"]]
    definition = definitions[-1] if definitions else None
    surface = definition["long"] if definition else value["text"]
    candidates = lookup(surface, index)
    features = [identity_features(value, r, sentence["text"], taxon, definition) for r in candidates]
    scores = learned.score("identity", features) if candidates else []
    ranked = sorted(
        zip(candidates, scores),
        key=lambda p: (p[0]["taxon_id"] != taxon if taxon else False, -float(p[1]), p[0]["id"]),
    )
    eligible = [(r, s) for r, s in ranked if r["taxon_id"] == taxon]
    reason = None
    if definition and not candidates:
        reason = "abbreviation_conflict"
    elif payload.get("identity_reason") == "gene_group":
        reason = "gene_group"
    elif taxon is None:
        reason = "species_unresolved"
    elif not eligible:
        reason = "identity_unresolved"
    elif len(eligible) > 1 and (
        eligible[0][1] < learned.manifest["identity_threshold"]
        or eligible[0][1] - eligible[1][1] < learned.manifest["identity_margin"]
    ):
        reason = "alias_collision"
    elif eligible[0][1] < learned.manifest["identity_threshold"] and not (
        len(eligible) == 1
        and spelling_keys(surface)
        & (spelling_keys(eligible[0][0]["symbol"]) | spelling_keys(eligible[0][0]["name"]))
    ):
        reason = "insufficient_identity_evidence"
    record = eligible[0][0] if eligible and reason is None else None
    value.update(
        normalized_id=record["id"] if record else None, status="candidate" if record else "needs_review"
    )
    payload.update(
        taxon_id=taxon,
        taxon_source=source,
        study_model=model,
        species="human" if taxon == 9606 else "animal" if taxon else "unknown",
        native_id=record["native_id"] if record else None,
        symbol=record["symbol"] if record else value["text"],
        identity_reason=reason,
        ambiguous=bool(reason),
        candidate_ids=[r["id"] for r in candidates],
    )
    payload["taxon_compatible_candidate_ids"] = [r["id"] for r in candidates if r["taxon_id"] == taxon]
    payload["normalization_rule"] = (
        "unique_official_name_and_taxon"
        if record and len(eligible) == 1
        else "learned_collision_ranking"
        if record
        else "deferred"
    )
    hypothesis = {
        "start": value["start"],
        "end": value["end"],
        "text": value["text"],
        "taxon_id": taxon,
        "study_model": model,
        "selected_id": value["normalized_id"],
        "reason": reason,
        "candidates": [
            {
                "id": r["id"],
                "taxon_id": r["taxon_id"],
                "symbol": r["symbol"],
                "priority_score": float(s),
                "taxon_compatible": r["taxon_id"] == taxon,
            }
            for r, s in ranked[:3]
        ],
        "candidate_count": len(ranked),
        "scores_are_nutrition_probabilities": False,
    }
    return value, hypothesis


def collect(text, sentences, dictionaries, metadata):
    learned = metadata["_learned_models"]
    learned.load()
    mentions, definitions = collect_candidates(
        text, sentences, dictionaries, metadata, metadata["_candidate_provider"], metadata["_gene_index"]
    )
    hypotheses, kept, rejected = [], [], []
    for mention in mentions:
        if mention["kind"] != "gene":
            kept.append(mention)
            continue
        if metadata.get("_gold_entities") is not None:
            # Preserve *all* provided IDs. Do not fabricate automatic normalization metrics.
            mention["payload"].update(
                provided_native_ids=mention.get("native_ids", []), origin="entity_oracle"
            )
            kept.append(mention)
            continue
        score = float(learned.score("span", [span_features(mention, text)])[0])
        threshold = learned.manifest["span_threshold"]
        if score < threshold:
            rejected.append(
                {
                    "start": mention["start"],
                    "end": mention["end"],
                    "text": mention["text"],
                    "reason": "span_filter",
                    "priority_score": score,
                }
            )
            continue
        sentence = next(s for s in sentences if s["start"] <= mention["start"] and mention["end"] <= s["end"])
        ranked, hypothesis = gene_hypotheses(
            mention, sentence, text, metadata["_gene_index"], metadata, definitions, learned
        )
        ranked["payload"]["detection_priority_score"] = score
        kept.append(ranked)
        hypotheses.append(hypothesis)
    return kept, definitions, hypotheses, rejected
