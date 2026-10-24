"""Auditable typed event extraction: dictionaries + pinned CPU dependencies.

Synthetic injected dependencies are labelled fixtures. If the production parser
cannot run, processing is blocked; no nearest-entity relation is substituted.
"""

from bisect import bisect_right
import hashlib
import json
from pathlib import Path
import re
from .extraction import _entity_mentions, _gene_mentions, _endpoint, _PRIOR, _REVIEW, _trie_matches
from .text import sentence_spans
from .context_v2 import organism_model, studies_context, conditions_for
from .summary_v2 import summarize
from .nlp_v2 import parser_for, parser_manifest, validate_tokens, ParserUnavailable

ENGINE_VERSION = "heuristic-v2"
_ROOT = Path(__file__).resolve().parents[1]
_RULE_FILE = _ROOT / "config" / "rules-v2.json"
_RULES = json.loads(_RULE_FILE.read_text(encoding="utf-8"))
_TRIGGERS = {word: kind for kind, words in _RULES["triggers"].items() for word in words}
_SOURCES = [
    "nutri_literature/extraction_v2.py",
    "nutri_literature/nlp_v2.py",
    "nutri_literature/context_v2.py",
    "nutri_literature/summary_v2.py",
    "nutri_literature/extraction.py",
    "nutri_literature/text.py",
    "nutri_literature/dictionaries.py",
    "nutri_literature/engines.py",
    "config/entities-v2.json",
    "config/ontology-v2.manifest.json",
    "config/rules-v2.json",
    "config/nlp-v2-requirements.txt",
]


def _source_manifest():
    sources, digest = {}, hashlib.sha256()
    for name in _SOURCES:
        path = _ROOT / name
        if not path.is_file():
            raise RuntimeError(f"Missing pinned v2 rules source: {name}")
        raw = path.read_bytes()
        sources[name] = hashlib.sha256(raw).hexdigest()
        digest.update(name.encode() + b"\0" + raw + b"\0")
    return sources, "rules-sha256:" + digest.hexdigest()


_RULE_SOURCE_HASHES, RULES_VERSION = _source_manifest()
_EXCLUDED = re.compile(
    r"introduction|background|references?|bibliograph|related work|methods?|materials?", re.I
)
_GENE_CONTEXT = re.compile(
    r"\b(?:genes?|expression|mRNA|transcripts?|proteins?|enzymes?|promoters?|methylation|receptors?|activity)\b",
    re.I,
)
_SPECULATION = re.compile(
    r"\b(?:may|might|could|potentially|hypothesi[sz]\w*|suggest\w*|propos\w*|possible|potential)\b", re.I
)
_NONSIG = re.compile(
    r"\b(?:not\s+(?:statistically\s+)?significant(?:ly)?|non[- ]significant|no significant)\b", re.I
)
_SIG = re.compile(r"\b(?:statistically\s+)?significant(?:ly)?\b", re.I)
_NO_EFFECT = re.compile(
    r"\b(?:no\s+(?:significant\s+)?(?:effect|change|difference|association|relationship)|unchanged|did not differ)\b",
    re.I,
)
_CLAUSE_BREAK = re.compile(r";|\b(?:but|whereas|however|although)\b", re.I)


def abbreviations(text):
    """Schwartz--Hearst right-to-left character alignment, document scoped."""
    result = {}
    for match in re.finditer(
        r"(?P<long>[^\n.!?;()]{3,180})\s*\((?P<short>[A-Za-z][A-Za-z0-9-]{1,11})\)", text
    ):
        short = match.group("short")
        if len(short) > 10 or not any(c.isalpha() for c in short):
            continue
        candidate = match.group("long").rstrip()
        words = candidate.split()
        max_words = min(len(short) + 5, len(short) * 2)
        candidate = " ".join(words[-max_words:])
        cursor = len(candidate) - 1
        start = None
        for position in range(len(short) - 1, -1, -1):
            char = short[position].casefold()
            if not char.isalnum():
                continue
            while cursor >= 0 and (
                candidate[cursor].casefold() != char
                or (position == 0 and cursor > 0 and candidate[cursor - 1].isalnum())
            ):
                cursor -= 1
            if cursor < 0:
                break
            start = cursor
            cursor -= 1
        else:
            if start is None:
                continue
            long = candidate[start:].strip()
            # Derive source offset from exact original suffix, never normalized
            # whitespace reconstructed above.
            original = match.group("long")
            index = original.casefold().rfind(long.casefold())
            if index < 0:
                continue
            definition = {
                "short": short,
                "long": original[index:].rstrip(),
                "start": match.start("long") + index,
                "end": match.start("long") + len(original.rstrip()),
                "short_start": match.start("short"),
                "short_end": match.end("short"),
            }
            result.setdefault(short, []).append(definition)
    return result


def _mentions(text, sentences, dictionaries, metadata):
    oracle = metadata.get("_gold_entities")
    if oracle is not None:
        mentions = []
        for value in oracle:
            mention = {**value, "payload": dict(value.get("payload", {})), "status": "candidate"}
            if text[mention["start"] : mention["end"]] != mention["text"]:
                raise ValueError("Oracle entity differs from immutable source")
            mentions.append(mention)
        return mentions, {}
    document_species, document_model = organism_model(text)
    mentions = _entity_mentions(text, dictionaries)
    entity_entries = {
        (start, end): entries[0] for start, end, entries in _trie_matches(text, dictionaries.entity_trie)
    }
    for mention in mentions:
        entry = entity_entries.get((mention["start"], mention["end"]), {})
        mention["payload"].update(
            {key: entry[key] for key in ("identity_status", "mapping_candidates") if key in entry}
        )
    mentions += _gene_mentions(text, sentences, document_species, dictionaries)
    # "vitamin D receptor" is a gene name, not a vitamin-D intervention.
    # A separately mentioned ligand elsewhere in the sentence remains intact.
    names = [
        m for m in mentions if m["kind"] == "gene" and len(m["text"]) > 8 and _GENE_CONTEXT.search(m["text"])
    ]
    mentions = [
        m
        for m in mentions
        if m["kind"] == "gene" or not any(g["start"] <= m["start"] and m["end"] <= g["end"] for g in names)
    ]
    definitions = abbreviations(text)
    starts = [s["start"] for s in sentences]
    for mention in mentions:
        index = bisect_right(starts, mention["start"]) - 1
        if index < 0:
            continue
        sentence = sentences[index]
        species, model = organism_model(sentence["text"])
        # Document fallback may normalize only single-species records. Cell
        # model never supplies human species by itself.
        if species == "unknown" and document_species in {"human", "animal"}:
            species = document_species
        if model == "unknown" and document_model in {"clinical", "cell", "animal"}:
            model = document_model
        payload = mention.setdefault("payload", {})
        payload.update({"organism": species, "species": species, "study_model": model})
        known = definitions.get(mention["text"], [])
        if known:
            preceding = [value for value in known if value["short_start"] <= mention["start"]]
            active = preceding[-1] if preceding else known[0]
            payload["abbreviation_definition"] = active
            long = active["long"].casefold()
            if mention["kind"] == "gene":
                entries = []
                node = dictionaries.gene_trie
                for token in re.findall(r"\w+", long):
                    node = node.get(token, {})
                entries = node.get("_entries", [])
                symbols = {entry["symbol"] for entry in entries}
                if payload.get("symbol") not in symbols:
                    mention["normalized_id"] = None
                    mention["status"] = "needs_review"
                    payload.update({"ambiguous": True, "abbreviation_conflict": True})
        if mention["kind"] == "gene" and species != "human":
            mention["normalized_id"] = None
            if mention["status"] != "needs_review":
                mention["status"] = "candidate_species_unresolved"
    # Introduce correctly defined local gene abbreviations without globalizing
    # them. Ambiguous/non-gene definitions cannot create an invented gene ID.
    occupied = {(m["start"], m["end"]) for m in mentions}
    for short, values in definitions.items():
        for value in values:
            node = dictionaries.gene_trie
            for token in re.findall(r"\w+", value["long"].casefold()):
                node = node.get(token, {})
            entries = node.get("_entries", [])
            unique = {entry["symbol"]: entry for entry in entries}
            if len(unique) != 1:
                continue
            entry = next(iter(unique.values()))
            for match in re.finditer(r"(?<!\w)" + re.escape(short) + r"(?!\w)", text):
                if (match.start(), match.end()) in occupied:
                    continue
                index = bisect_right(starts, match.start()) - 1
                species, model = organism_model(sentences[index]["text"])
                if species == "unknown" and document_species in {"human", "animal"}:
                    species = document_species
                mentions.append(
                    {
                        "kind": "gene",
                        "text": match.group(),
                        "normalized_id": entry["id"] if species == "human" else None,
                        "start": match.start(),
                        "end": match.end(),
                        "status": "candidate"
                        if entry["id"] and species == "human"
                        else "candidate_species_unresolved",
                        "payload": {
                            "symbol": entry["symbol"],
                            "organism": species,
                            "species": species,
                            "study_model": model,
                            "candidate_symbols": [entry["symbol"]],
                            "candidate_hgnc_ids": [entry["id"]] if entry["id"] else [],
                            "abbreviation_definition": value,
                            "ambiguous": False,
                            "hgnc_verified": bool(entry["id"]),
                        },
                    }
                )
                occupied.add((match.start(), match.end()))
    mentions.sort(key=lambda m: (m["start"], m["end"], m["kind"]))
    return mentions, definitions


def _children(tokens, index):
    return [token for token in tokens if token["head"] == index and token["i"] != index]


def _descendants(tokens, index, excluded=None):
    found, frontier = set(), [index]
    excluded = excluded or set()
    while frontier:
        current = frontier.pop()
        if current in found:
            continue
        found.add(current)
        frontier.extend(child["i"] for child in _children(tokens, current) if child["dep"] not in excluded)
    return found


def _argument(tokens, root, mentions, exclude_conj=False):
    excluded = {"ccomp", "advcl", "relcl", "acl", "parataxis"}
    if exclude_conj:
        excluded.add("conj")
    indexes = _descendants(tokens, root["i"], excluded)
    covered = [
        m
        for m in mentions
        if any(tokens[i]["start"] < m["end"] and m["start"] < tokens[i]["end"] for i in indexes)
    ]
    # A measurement wrapper ("VDR expression") identifies the measured gene;
    # a coincident generic omics mention is not a second event endpoint.
    genes = [m for m in covered if m["kind"] == "gene"]
    if genes and re.search(r"expression|activity|methylation|protein|mRNA|transcript", root["text"], re.I):
        covered = [m for m in covered if m["kind"] != "omics"]
    # Nested token overlap must not make partial, non-subtree mention endpoints.
    covered = [
        m
        for m in covered
        if all(
            i in indexes
            for i, token in enumerate(tokens)
            if token["start"] < m["end"] and m["start"] < token["end"]
        )
    ]
    if len(covered) > 1:
        # Explicit coordination supports multiple endpoints; all other compound
        # participants are ambiguous rather than silently selecting one.
        anchors = [
            next((token for token in tokens if m["start"] <= token["start"] < m["end"]), None)
            for m in covered
        ]
        if not all(anchor and (anchor["i"] == root["i"] or anchor["dep"] == "conj") for anchor in anchors):
            return [], "ambiguous_argument_subtree", covered
    return covered, None, covered


def _subject_roots(tokens, trigger):
    roots = [
        token
        for token in _children(tokens, trigger["i"])
        if token["dep"] in {"nsubj", "csubj", "nsubjpass", "csubjpass"}
    ]
    if not roots and trigger["dep"] == "conj":
        parent = tokens[trigger["head"]]
        roots = [
            token
            for token in _children(tokens, parent["i"])
            if token["dep"] in {"nsubj", "csubj", "nsubjpass", "csubjpass"}
        ]
    return roots


def _preposition_objects(tokens, token, allowed):
    roots = []
    for child in _children(tokens, token["i"]):
        if child["dep"] in {"prep", "agent"} and child["text"].casefold() in allowed:
            roots.extend(value for value in _children(tokens, child["i"]) if value["dep"] in {"pobj", "obj"})
        # Universal dependencies in injected corpora can attach obl + case.
        if child["dep"] in {"obl", "nmod"} and any(
            c["dep"] == "case" and c["text"].casefold() in allowed for c in _children(tokens, child["i"])
        ):
            roots.append(child)
    return roots


def _roles(tokens, trigger, kind, mentions):
    if kind == "association" and trigger.get("pos") == "NOUN":
        # "association between A and B": syntax provides the between-object
        # and its coordinated partner; entity order alone provides no relation.
        between = _preposition_objects(tokens, trigger, {"between"})
        if not between and trigger["head"] != trigger["i"]:
            between = _preposition_objects(tokens, tokens[trigger["head"]], {"between"})
        if len(between) != 1:
            return [], [], ["missing_explicit_between_roles"], []
        root = between[0]
        partners = [child for child in _children(tokens, root["i"]) if child["dep"] == "conj"]
        if len(partners) != 1:
            return [], [], ["ambiguous_between_coordination"], []
        controllers, reason1, possible1 = _argument(tokens, root, mentions, exclude_conj=True)
        themes, reason2, possible2 = _argument(tokens, partners[0], mentions, exclude_conj=True)
        reasons = [reason for reason in (reason1, reason2) if reason]
        if not controllers or not themes:
            reasons.append("role_has_no_supported_entity")
        return controllers, themes, reasons, possible1 + possible2
    subjects = _subject_roots(tokens, trigger)
    passive = any(root["dep"] in {"nsubjpass", "csubjpass"} for root in subjects) or any(
        child["dep"] in {"auxpass"} for child in _children(tokens, trigger["i"])
    )
    object_roots = [
        token for token in _children(tokens, trigger["i"]) if token["dep"] in {"dobj", "obj", "attr"}
    ]
    if passive and kind != "association":
        controller_roots = _preposition_objects(tokens, trigger, {"by"})
        theme_roots = subjects
    else:
        controller_roots, theme_roots = subjects, object_roots
        if not theme_roots:
            theme_roots = _preposition_objects(
                tokens, trigger, {"with", "to"} if kind in {"association", "binding"} else {"on"}
            )
    if kind == "no_effect":
        # "A had no effect on B": effect is the nominal trigger; subject lives
        # on "had". Only an explicit on-preposition supplies the theme.
        parent = tokens[trigger["head"]]
        controller_roots = _subject_roots(tokens, parent)
        theme_roots = _preposition_objects(tokens, trigger, {"on", "with"})
        if not theme_roots:
            # The pinned biomedical parser attaches "on B" to "had" in
            # "A had no effect on B", retaining an explicit on-case edge.
            theme_roots = _preposition_objects(tokens, parent, {"on", "with"})
    if not controller_roots or not theme_roots:
        return [], [], ["missing_explicit_dependency_role"], []
    controllers, themes, reasons, candidates = [], [], [], []
    for roots, output in [(controller_roots, controllers), (theme_roots, themes)]:
        for root in roots:
            found, reason, possible = _argument(tokens, root, mentions)
            output.extend(found)
            candidates.extend(possible)
            if reason:
                reasons.append(reason)
    if not controllers or not themes:
        reasons.append("role_has_no_supported_entity")
    return controllers, themes, sorted(set(reasons)), candidates


def _scope(sentence, tokens, trigger):
    # Trigger clause boundary, not an endpoint-distance window.
    start, end = sentence["start"], sentence["end"]
    for match in _CLAUSE_BREAK.finditer(sentence["text"]):
        absolute = sentence["start"] + match.start()
        if absolute < trigger["start"]:
            start = sentence["start"] + match.end()
        elif absolute >= trigger["end"]:
            end = absolute
            break
    for other in tokens:
        if other["i"] == trigger["i"]:
            continue
        if other["dep"] in {"conj", "advcl", "ccomp", "parataxis"} and other.get("pos") in {"VERB", "AUX"}:
            children = _descendants(tokens, other["i"], {"conj", "advcl", "ccomp"})
            left = min(tokens[i]["start"] for i in children)
            right = max(tokens[i]["end"] for i in children)
            if left > trigger["end"]:
                end = min(end, left)
            elif right < trigger["start"]:
                start = max(start, right)
    while start < end and sentence["text"][start - sentence["start"]].isspace():
        start += 1
    return {"start": start, "end": end}


def _modifiers(sentence, tokens, trigger, scope, kind):
    line = sentence["text"][scope["start"] - sentence["start"] : scope["end"] - sentence["start"]]
    direct = _children(tokens, trigger["i"])
    negation = any(token["dep"] == "neg" for token in direct)
    if trigger["dep"] == "conj" and not any(token["dep"] in {"aux", "auxpass"} for token in direct):
        # Shared auxiliary negation in "did not increase or reduce" belongs to
        # both coordinated predicates; contrast clauses terminate inheritance.
        parent = tokens[trigger["head"]]
        bridge = sentence["text"][parent["end"] - sentence["start"] : trigger["start"] - sentence["start"]]
        if not re.search(r"\b(?:but|whereas|however)\b|;", bridge, re.I):
            negation |= any(token["dep"] == "neg" for token in _children(tokens, parent["i"]))
    nonsignificant = bool(_NONSIG.search(line))
    # "not significantly reduce" negates significance; preserve the stated
    # direction without declaring the biological effect absent.
    if nonsignificant and not re.search(r"\bnever\b", line, re.I):
        negation = False
    significance = "not_significant" if nonsignificant else "significant" if _SIG.search(line) else "unknown"
    speculation = any(token["text"].casefold() in {"may", "might", "could"} for token in direct) or bool(
        _SPECULATION.search(line)
    )
    direction = (
        "change"
        if kind == "no_effect" and nonsignificant
        else "no_effect"
        if kind == "no_effect"
        else kind
        if kind in {"increase", "decrease", "association"}
        else "change"
    )
    if (
        kind == "association"
        and trigger.get("pos") == "NOUN"
        and any(child["text"].casefold() == "no" for child in direct)
        and not nonsignificant
    ):
        direction = "no_effect"
    # No-effect nominal carries absence, not a negative assertion of the
    # existence of an effect. No significant effect retains uncertainty.
    if kind == "no_effect" and not nonsignificant:
        negation = False
    return direction, negation, speculation, significance


def _outcome_type(theme, line):
    if theme["kind"] == "outcome":
        return "health_outcome"
    if re.search(r"\b(?:expression|mRNA|transcript\w*)\b", line, re.I):
        return "expression"
    if re.search(r"\bmethylat\w*\b", line, re.I):
        return "methylation"
    if re.search(r"\b(?:activity|activities|activation)\b", line, re.I):
        return "activity"
    if re.search(r"\b(?:concentration|levels?)\b", line, re.I):
        return "concentration"
    return "unknown"


def _association_direction(tokens, trigger, kind):
    if kind != "association":
        return "unspecified"
    qualifiers = {
        child["text"].casefold()
        for child in _children(tokens, trigger["i"])
        if child["dep"] in {"advmod", "amod"}
    }
    positive = bool(qualifiers & {"positive", "positively"})
    negative = bool(qualifiers & {"negative", "negatively", "inverse", "inversely"})
    return (
        "positive"
        if positive and not negative
        else "negative"
        if negative and not positive
        else "unspecified"
    )


def _abstention(sentence, trigger, reasons, candidates=None):
    return {
        "status": "abstained",
        "trigger": {key: trigger[key] for key in ("text", "start", "end", "lemma")},
        "quote": sentence["text"],
        "start": sentence["start"],
        "end": sentence["end"],
        "abstention_reasons": sorted(set(reasons)),
        "candidate_endpoints": candidates or [],
    }


def _benchmark_scopes(sentence, tokens):
    result = []
    for token in tokens:
        kind = (
            "negation"
            if token["dep"] == "neg" or token["text"].casefold() in {"no", "neither", "without"}
            else "speculation"
            if token["text"].casefold() in {"may", "might", "could", "possible", "possibly", "perhaps"}
            else None
        )
        if not kind:
            continue
        head = tokens[token["head"]]
        scope = _scope(sentence, tokens, head)
        result.append(
            {
                "kind": kind,
                "cue_start": token["start"],
                "cue_end": token["end"],
                **scope,
                "text": sentence["text"][
                    scope["start"] - sentence["start"] : scope["end"] - sentence["start"]
                ],
            }
        )
    return result


def _group_key(event):
    conditions = event["conditions"]
    unresolved_conditions = not conditions["arm_id"] or not conditions["timepoint"]
    # Unknown conditions are not evidence that two experiments share a setting.
    # Repeated identical statements may group; distinct unknown-arm statements
    # stay separate until a reviewer resolves the study context.
    unresolved_key = event["evidence_quotes"][0]["quote"] if unresolved_conditions else None
    return (
        event["subject"],
        event["object"],
        event["outcome_type"],
        event["direction"],
        event.get("association_direction", "unspecified"),
        event["negation"],
        event["speculation"],
        event["significance"],
        event["species"],
        event["study_model"],
        conditions["arm_id"],
        conditions["comparator"]["value"] if conditions["comparator"] else None,
        conditions["timepoint"]["value"] if conditions["timepoint"] else None,
        tuple((q["quantity_kind"], q["value"], q["owner_id"]) for q in conditions["quantities"]),
        unresolved_key,
    )


def extract(text, sections, dictionaries, metadata=None):
    # Compatibility with callers retaining the v1 positional order.
    if isinstance(dictionaries, dict) and hasattr(metadata, "gene_trie"):
        dictionaries, metadata = metadata, dictionaries
    metadata = metadata or {}
    ablation_stage = metadata.get("_ablation_stage")
    if ablation_stage not in {None, "syntax_without_context"}:
        raise ValueError("Unknown v2 ablation stage")
    result_rules_version = RULES_VERSION + ("|ablation:" + ablation_stage if ablation_stage else "")
    sentences = sentence_spans(text, sections)
    mentions, definitions = _mentions(text, sentences, dictionaries, metadata)
    studies, context = studies_context(sentences, mentions, metadata)
    parser = parser_for(metadata)
    events, abstentions, scopes = [], [], []
    blocked = False
    publication_types = metadata.get("publication_types", [])
    if isinstance(publication_types, str):
        publication_types = [publication_types]
    review = any(_REVIEW.search(str(value)) for value in publication_types) or bool(
        re.search(
            r"\b(?:systematic review|meta-analysis|a review|scoping review)\b",
            metadata.get("title", ""),
            re.I,
        )
    )
    for sentence in sentences:
        local_mentions = [
            m for m in mentions if sentence["start"] <= m["start"] and m["end"] <= sentence["end"]
        ]
        try:
            tokens = validate_tokens(parser.parse(sentence), sentence)
        except (ParserUnavailable, ImportError, OSError) as error:
            blocked = True
            abstentions.append(
                {
                    "status": "abstained",
                    "quote": sentence["text"],
                    "start": sentence["start"],
                    "end": sentence["end"],
                    "abstention_reasons": ["parser_unavailable"],
                    "error": str(error),
                    "candidate_endpoints": local_mentions,
                }
            )
            continue
        scopes.extend(_benchmark_scopes(sentence, tokens))
        background = review or _EXCLUDED.search(sentence["label"]) or _PRIOR.search(sentence["text"])
        for token in tokens:
            kind = _TRIGGERS.get(token["lemma"])
            if (
                token["text"].casefold() in {"association", "correlation", "relationship"}
                and token.get("pos") == "NOUN"
            ):
                kind = "association"
            if token["text"].casefold() in {"effect", "change", "difference"} and any(
                child["text"].casefold() == "no" for child in _children(tokens, token["i"])
            ):
                kind = "no_effect"
            if not kind or (
                token.get("pos") not in {"VERB", "AUX"} and kind not in {"no_effect", "association"}
            ):
                continue
            controllers, themes, reasons, candidates = _roles(tokens, token, kind, local_mentions)
            if reasons:
                if local_mentions:
                    abstentions.append(_abstention(sentence, token, reasons, candidates or local_mentions))
                continue
            scope = _scope(sentence, tokens, token)
            scope_line = text[scope["start"] : scope["end"]]
            if ablation_stage == "syntax_without_context":
                # Development-only intervention on the actual rule component.
                # Never invoke contextual flags or event-condition binding.
                direction = kind if kind in {"increase", "decrease", "association"} else "change"
                negation, speculation, significance = False, False, "unknown"
            else:
                direction, negation, speculation, significance = _modifiers(
                    sentence, tokens, token, scope, kind
                )
            for controller in controllers:
                for theme in themes:
                    failures = []
                    if controller.get("payload", {}).get("ambiguous") or theme.get("payload", {}).get(
                        "ambiguous"
                    ):
                        failures.append("ambiguous_entity_identity")
                    if _endpoint(controller) == _endpoint(theme):
                        failures.append("identical_endpoints")
                    if controller["kind"] == "outcome" and theme["kind"] == "outcome":
                        failures.append("unsupported_outcome_to_outcome_relation")
                    if failures:
                        abstentions.append(_abstention(sentence, token, failures, [controller, theme]))
                        continue
                    species, model = organism_model(scope_line)
                    if species == "unknown" and context["species"] in {"human", "animal"}:
                        species = context["species"]
                    if model == "unknown" and context["study_model"] in {"clinical", "cell", "animal"}:
                        model = context["study_model"]
                    conditions = (
                        {
                            "arm_id": None,
                            "comparator": None,
                            "timepoint": None,
                            "quantities": [],
                            "statistics": [],
                        }
                        if ablation_stage
                        else conditions_for(sentence, controller, scope, studies)
                    )
                    risks = []
                    if species == "unknown":
                        risks.append("species_unknown")
                    if model == "unknown":
                        risks.append("study_model_unknown")
                    if any(m["kind"] == "gene" and not m.get("normalized_id") for m in (controller, theme)):
                        risks.append("gene_identity_unresolved")
                    if any(
                        m.get("payload", {}).get("identity_status")
                        in {"stereochemistry_unspecified", "processing_unspecified", "form_unspecified"}
                        for m in (controller, theme)
                    ):
                        risks.append("chemical_or_processing_subtype_unspecified")
                    if significance == "unknown":
                        risks.append("significance_unknown")
                    if (
                        re.search(r"\b(?:compared|versus|vs)\b", scope_line, re.I)
                        and not conditions["comparator"]
                    ):
                        risks.append("comparator_unresolved")
                    if speculation:
                        risks.append("speculative_statement")
                    status = "background" if background else "candidate"
                    event = {
                        "subject": _endpoint(controller),
                        "object": _endpoint(theme),
                        "controller": controller,
                        "theme": theme,
                        "trigger": {key: token[key] for key in ("text", "start", "end", "lemma")},
                        "relation_type": "binding"
                        if kind == "binding"
                        else "association"
                        if kind == "association"
                        else "effect",
                        "outcome_type": _outcome_type(theme, scope_line),
                        "direction": direction,
                        "association_direction": _association_direction(tokens, token, kind),
                        "negation": negation,
                        "speculation": speculation,
                        "speculative": speculation,
                        "significance": significance,
                        "statistical_significance": significance,
                        "polarity": "null"
                        if direction == "no_effect"
                        else "negated"
                        if negation
                        else "positive",
                        "species": species,
                        "organism": species,
                        "study_model": model,
                        "conditions": conditions,
                        "arm_id": conditions["arm_id"],
                        "comparator": conditions["comparator"],
                        "timepoint": conditions["timepoint"],
                        "scope": scope,
                        "status": status,
                        "triage_reasons": risks,
                        "abstention_reasons": ["background_or_prior_statement"] if background else [],
                        "evidence_quotes": [
                            {"quote": sentence["text"], "start": sentence["start"], "end": sentence["end"]}
                        ],
                        "parser_manifest": parser_manifest(parser),
                        "rule_id": "dependency-roles-v2",
                        "interpretation": "source statement candidate; not a confirmed dietary causal effect",
                    }
                    event["event_id"] = (
                        "event-sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                {
                                    "semantic_key": _group_key(event),
                                    "source_span": [sentence["start"], sentence["end"]],
                                    "rules": result_rules_version,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ).encode()
                        ).hexdigest()
                    )
                    events.append(event)
    grouped = {}
    for event in events:
        if event["status"] != "candidate":
            continue
        key = _group_key(event)
        if key in grouped:
            previous = grouped[key]
            for quote in event["evidence_quotes"]:
                if quote not in previous["evidence_quotes"]:
                    previous["evidence_quotes"].append(quote)
            previous.setdefault("grouped_event_ids", [previous["event_id"]]).append(event["event_id"])
        else:
            grouped[key] = dict(event)
            grouped[key]["evidence_quotes"] = list(event["evidence_quotes"])
    assertions = []
    for event in grouped.values():
        quote = event["evidence_quotes"][0]
        predicate = {"increase": "increases", "decrease": "decreases", "association": "associated_with"}.get(
            event["direction"], "binds" if event["relation_type"] == "binding" else "affects"
        )
        assertions.append(
            {
                "subject": event["subject"],
                "predicate": predicate,
                "object": event["object"],
                **quote,
                "rule_id": event["rule_id"],
                "payload": {
                    "event": event,
                    "explicit_relation": True,
                    "subject_mention": event["controller"],
                    "object_mention": event["theme"],
                    "cue": event["trigger"]["text"],
                    "cue_start": event["trigger"]["start"],
                    "cue_end": event["trigger"]["end"],
                    "polarity": event["polarity"],
                    "speculative": event["speculation"],
                    "organism": event["species"],
                    "rule_score": 1 + int(event["significance"] != "unknown"),
                    "triage_reasons": event["triage_reasons"],
                    "interpretation": event["interpretation"],
                },
            }
        )
    actual_parser = parser_manifest(parser)
    if not sentences:
        actual_parser.setdefault("available", False)
    annotated = [
        {
            "label": "interventions" if m["kind"] in {"nutrient", "food", "diet"} else "outcomes",
            "start": m["start"],
            "end": m["end"],
            "text": m["text"],
        }
        for m in mentions
        if m["kind"] in {"nutrient", "food", "diet", "outcome"}
    ]
    annotated += [
        {"label": "participants", "start": p["start"], "end": p["end"], "text": p["text"]}
        for s in studies
        for p in s["participants"]
    ]
    manifest = {
        "engine_version": ENGINE_VERSION,
        "rules_version": result_rules_version,
        "rule_source_hashes": _RULE_SOURCE_HASHES,
        "dictionary_version": dictionaries.version,
        "parser": actual_parser,
        "ontology": json.loads((_ROOT / "config" / "ontology-v2.manifest.json").read_text(encoding="utf-8")),
        "oracle": metadata.get("_gold_entities") is not None,
        "ablation_stage": ablation_stage,
        "offset_unit": "unicode_codepoints",
        "triage_scores_are_probabilities": False,
    }
    return {
        "engine_version": ENGINE_VERSION,
        "rules_version": result_rules_version,
        "dictionary_version": dictionaries.version,
        "dictionary_manifest": dictionaries.manifest,
        "summary": summarize(sentences, events, _RULES["mmr_lambda"]),
        "context": context,
        "mentions": mentions,
        "assertions": [] if blocked else assertions,
        "events": events,
        "abstentions": abstentions,
        "studies": studies,
        "abbreviations": definitions,
        "benchmark_scopes": scopes,
        "annotated_spans": annotated,
        "manifest": manifest,
        "status": "blocked" if blocked else "complete",
        "processing_status": "blocked_parser" if blocked else "complete",
        "duplicate_events_grouped": len([e for e in events if e["status"] == "candidate"]) - len(grouped),
    }
