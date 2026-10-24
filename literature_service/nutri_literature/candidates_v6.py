"""Raw candidate retention and independent automatic/review decisions."""

from .candidates_v4 import collect_candidates
from .features_v5 import span_features
from .identity_v6 import normalize, cluster_mentions


def collect(text, sentences, dictionaries, metadata):
    mentions, definitions = collect_candidates(
        text, sentences, dictionaries, metadata, metadata["_candidate_provider"], metadata["_gene_index"]
    )
    hypotheses, selected = [], []
    for mention in mentions:
        payload = mention.setdefault("payload", {})
        if metadata.get("_gold_entities") is not None:
            payload.update(
                provided_native_ids=mention.get("native_ids", []),
                origin="entity_oracle",
                automatic_selected=True,
            )
            if mention["kind"] == "gene":
                # Same text-only taxon policy as automatic inference; supplied IDs
                # never determine mention clustering.
                from .identity_v6 import taxon_evidence

                sentence = next(
                    (s for s in sentences if s["start"] <= mention["start"] < mention["end"] <= s["end"]),
                    None,
                )
                if sentence:
                    taxon, model, evidence = taxon_evidence(mention, sentence, sentences, text, metadata)
                    payload.update(taxon_id=taxon, study_model=model, taxon_evidence=evidence)
            selected.append(mention)
            continue
        if mention["kind"] == "gene":
            learned = metadata["_learned_models"]
            score = float(learned.score("span", [span_features(mention, text)])[0])
            automatic = score >= learned.manifest["span_threshold"]
            sentence = next(
                (s for s in sentences if s["start"] <= mention["start"] < mention["end"] <= s["end"]), None
            )
            if sentence is None:
                payload.update(automatic_selected=False, detection_reason="sentence_boundary_unresolved")
                selected.append(mention)
                continue
            models = metadata.get("_v6_models")
            if models is not None and "span" in models.heads:
                from .embeddings_v6 import span_context_features

                features = span_context_features(mention, text, sentence, metadata["_encoder"])
                score = float(models.heads["span"].probabilities([features])[0].get("1", 0))
                automatic = score >= models.manifest["selections"]["span"]["threshold"]
            mention, hypothesis = normalize(
                mention,
                sentence,
                sentences,
                text,
                metadata["_id_search"],
                metadata,
                definitions,
                metadata.get("_v6_models"),
            )
            mention["payload"].update(
                detection_priority_score=score,
                automatic_selected=automatic,
                raw_candidate_preserved=True,
                detection_head="frozen_context_v6"
                if models is not None and "span" in models.heads
                else "legacy_v5_comparison",
                detection_reason=None if automatic else "span_priority_below_threshold",
            )
            hypotheses.append(hypothesis)
        else:
            payload["automatic_selected"] = True
            if mention["kind"] in {"component", "nutrient"}:
                lexicon = metadata.get("_chemical_lexicon", {})
                candidates = lexicon.get("index", {}).get(mention["text"].casefold(), [])
                unique = {r["id"]: r for r in candidates}
                if len(unique) == 1:
                    row = next(iter(unique.values()))
                    mention["normalized_id"] = row["id"]
                    payload.update(
                        native_id=row["native_id"], identity_status=row["record_kind"] + "_concept"
                    )
                elif len(unique) > 1:
                    mention["normalized_id"] = None
                    payload.update(identity_reason="chemical_alias_collision")
                hypotheses.append(
                    {
                        "kind": "component",
                        "start": mention["start"],
                        "end": mention["end"],
                        "text": mention["text"],
                        "selected_id": mention.get("normalized_id"),
                        "candidates": [{"id": r["id"], "native_id": r["native_id"]} for r in unique.values()],
                        "reason": payload.get("identity_reason"),
                    }
                )
        selected.append(mention)
    return (
        selected,
        definitions,
        hypotheses,
        cluster_mentions(selected, definitions, metadata["_id_search"], metadata.get("_chemical_lexicon")),
    )
