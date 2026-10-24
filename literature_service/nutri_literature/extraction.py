"""Conservative, deterministic extraction of auditable literature candidates.

No statement becomes a confirmed scientific finding here. Rule scores order
source sentences; they are not calibrated probabilities or confidence values.
"""

from bisect import bisect_right
import hashlib
from pathlib import Path
import re
from .text import lexical_tokens, sentence_spans

ENGINE_VERSION = "heuristic-v1"
# Hash source files in a fixed, named order so rule changes invalidate reuse,
# while dictionary updates retain their independent provenance version.
_MODULE_DIRECTORY = Path(__file__).resolve().parent
RULES_VERSION = (
    "rules-sha256:"
    + hashlib.sha256(
        b"extraction.py\0"
        + (_MODULE_DIRECTORY / "extraction.py").read_bytes()
        + b"\0text.py\0"
        + (_MODULE_DIRECTORY / "text.py").read_bytes()
    ).hexdigest()
)
UNKNOWN = "미확인"
_HUMAN = re.compile(
    r"\b(?:human|humans|patients?|participants?|volunteers?|adults?|men|women|subjects)\b", re.I
)
_ANIMAL = re.compile(
    r"\b(?:mice|mouse|murine|rats?|rodents?|rabbits?|zebrafish|drosophila|animal(?:s| model)?)\b", re.I
)
_CELL = re.compile(r"\b(?:cell(?:s|ular)?|cell lines?|in vitro|cultured|HEK293|HUVEC)\b", re.I)
_GENE_CONTEXT = re.compile(
    r"\b(?:genes?|genetic|genotype|alleles?|polymorphisms?|variants?|expression|expressed|mRNA|transcript|protein|enzyme|promoter|methylation|catalase|receptor|synthase)\b",
    re.I,
)
_PRIOR = re.compile(
    r"\b(?:previous(?:ly)?|prior|earlier|published|other|preceding)\s+(?:stud(?:y|ies)|reports?|research|work)|\b(?:previously (?:reported|shown|demonstrated)|has been (?:reported|shown)|according to|et al\.)",
    re.I,
)
_EXCLUDED_SECTION = re.compile(
    r"introduction|background|references?|bibliograph|related work|methods?|materials?", re.I
)
_REVIEW = re.compile(r"\b(?:review|meta-analysis|systematic review)\b", re.I)
_SPECULATIVE = re.compile(
    r"\b(?:may|might|could|potentially|hypothesi[sz]e\w*|suggest\w*|propos\w*|possible|potential)\b", re.I
)
_NULL = re.compile(
    r"\b(?:not statistically significant|not significant|not\s+(?:statistically\s+)?significantly|non[- ]significant|no significant|no (?:difference|effect|association|change|relationship)|did not differ|unchanged|similar (?:levels|values))\b",
    re.I,
)
_NEGATION = re.compile(r"\b(?:not|never|neither|didn't|doesn't|wasn't|weren't|without)\b", re.I)
_CUE = re.compile(
    r"\b(?P<increase>increas\w*|up[- ]?regulat\w*|elevat\w*|enhanc\w*|activat\w*|induc\w*)\b|"
    r"\b(?P<decrease>reduc\w*|decreas\w*|down[- ]?regulat\w*|suppress\w*|inhibit\w*|lower\w*)\b|"
    r"\b(?P<association>associat\w*|correlat\w*|linked)\s+(?:positively |negatively |significantly )?(?:with|to)\b|"
    r"\b(?P<effect>affect\w*|alter\w*|modulat\w*|no (?:effect|association|relationship) (?:on|with|between))\b",
    re.I,
)
_STAT = re.compile(
    r"\b(?:p\s*[<=>]\s*0?\.\d+|(?:95|90|99)\s*%\s*(?:CI|confidence interval)|(?:odds|hazard|risk) ratio|(?:OR|HR|RR)\s*[=:]\s*\d)",
    re.I,
)
_LIMIT = re.compile(
    r"\b(?:limitation|limited by|small sample|residual confounding|further (?:research|studies)|cannot (?:infer|establish)|should be interpreted|caution|underpowered)\b",
    re.I,
)
_METHOD = re.compile(
    r"\b(?:randomi[sz]|trial|cohort|cross[- ]sectional|case[- ]control|observational|participants?|patients?|mice|rats?|cell|recruit\w*|enroll\w*)",
    re.I,
)


def _organism(text: str) -> str:
    human, animal, cell = bool(_HUMAN.search(text)), bool(_ANIMAL.search(text)), bool(_CELL.search(text))
    if human and animal:
        return "mixed"
    if animal:
        return "animal"
    if human:
        return "human"
    if cell:
        return "cell"
    return "unknown"


def _trie_matches(text, trie):
    tokens = lexical_tokens(text)
    matches = []
    for index, (_, start, _) in enumerate(tokens):
        node, found = trie, None
        previous_end = start
        for cursor in range(index, len(tokens)):
            token, token_start, end = tokens[cursor]
            # Do not let a multiword dictionary name cross a sentence boundary.
            if cursor > index and re.search(r"[.,!?;:\n]", text[previous_end:token_start]):
                break
            if token not in node:
                break
            node = node[token]
            previous_end = end
            if "_entries" in node:
                found = (start, end, node["_entries"])
        if found:
            matches.append(found)
    # Prefer the longest dictionary phrase and avoid nested/duplicate matches.
    selected = []
    for match in sorted(matches, key=lambda m: (m[0], -(m[1] - m[0]))):
        if selected and match[0] < selected[-1][1]:
            continue
        selected.append(match)
    return selected


def _entity_mentions(text, dictionaries):
    result = []
    for start, end, entries in _trie_matches(text, dictionaries.entity_trie):
        entry = entries[0]
        surface = text[start:end]
        # Short diet/nutrient acronyms are case-sensitive, not ordinary words.
        if len(entry["term"]) <= 4 and entry["term"].isupper() and surface != entry["term"]:
            continue
        result.append(
            {
                "kind": entry["kind"],
                "text": surface,
                "normalized_id": entry["id"],
                "start": start,
                "end": end,
                "status": "candidate",
                "payload": {"label": entry["label"], "dictionary_term": entry["term"]},
            }
        )
    return result


def _gene_mentions(text, sentences, organism, dictionaries):
    result = []
    sentence_starts = [s["start"] for s in sentences]
    for start, end, entries in _trie_matches(text, dictionaries.gene_trie):
        sentence_index = bisect_right(sentence_starts, start) - 1
        if sentence_index < 0 or end > sentences[sentence_index]["end"]:
            continue
        sentence = sentences[sentence_index]
        surface, line = text[start:end], sentence["text"]
        species = _organism(line)
        if species == "unknown":
            species = organism if organism in {"human", "animal"} else "unknown"
        candidates = []
        nearby = text[max(sentence["start"], start - 80) : min(sentence["end"], end + 80)]
        has_gene_context = bool(_GENE_CONTEXT.search(nearby))
        for entry in entries:
            # Full official names are case-insensitive. Symbols remain exact,
            # except conventional animal capitalization with gene context.
            if entry["form_kind"] != "name" and len(entry["term"].split()) == 1:
                if surface != entry["term"] and not (species == "animal" and has_gene_context):
                    continue
            candidates.append(entry)
        if not candidates:
            continue
        if surface.upper() == "CAT" and re.search(r"\bCAT\s+(?:scans?|scanning|imaging)\b", line):
            continue
        # Exact approved symbols take precedence over an alias of another gene.
        exact = [e for e in candidates if e["form_kind"] == "symbol" and surface == e["symbol"]]
        if exact:
            candidates = exact
        unique = {e["symbol"]: e for e in candidates}
        entry = next(iter(unique.values()))
        ambiguous = len(unique) > 1 or (len(entry["term"]) <= 3 and not has_gene_context)
        normalizable = len(unique) == 1 and not ambiguous and species == "human" and entry["id"] is not None
        status = (
            "needs_review" if ambiguous else ("candidate" if normalizable else "candidate_species_unresolved")
        )
        result.append(
            {
                "kind": "gene",
                "text": surface,
                "normalized_id": entry["id"] if normalizable else None,
                "start": start,
                "end": end,
                "status": status,
                "payload": {
                    "symbol": entry["symbol"],
                    "organism": species,
                    "model": "cell" if _CELL.search(line) else None,
                    "candidate_symbols": sorted(unique),
                    "candidate_hgnc_ids": sorted({e["id"] for e in unique.values() if e["id"]}),
                    "ambiguous": ambiguous,
                    "hgnc_verified": entry["id"] is not None,
                    "dictionary_term": entry["term"],
                },
            }
        )
    return result


def _value(sentence, match, value=None):
    return {
        "value": value if value is not None else match.group(),
        "quote": sentence["text"],
        "start": sentence["start"] + match.start(),
        "end": sentence["start"] + match.end(),
    }


def _unique_value(values):
    unique = {str(value["value"]): value for value in values}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        return {
            "value": UNKNOWN,
            "reason": "multiple explicit values; manual interpretation required",
            "candidates": values,
        }
    return UNKNOWN


def _context(sentences, metadata):
    text = " ".join(s["text"] for s in sentences)
    patterns = {
        "study_design": r"\b(?:randomi[sz]ed(?:\s+controlled)?\s+trial|cross[- ]sectional(?:\s+study)?|prospective\s+cohort|retrospective\s+cohort|cohort\s+study|case[- ]control(?:\s+study)?|systematic\s+review|meta-analysis)\b",
        "tissue": r"\b(?:whole blood|peripheral blood|blood leukocytes|PBMCs?|liver|adipose tissue|skeletal muscle|kidney|vascular tissue|endothelial cells?|aortic tissue)\b",
        "participants": r"\b(?:[1-9]\d{0,5}\s+(?:participants?|patients?|volunteers?|adults?|subjects|men|women)|(?:participants?|patients?|volunteers?|adults?|subjects)\s*\(\s*n\s*=\s*[1-9]\d{0,5}\s*\))(?=\W|$)",
        "dose": r"\b\d+(?:\.\d+)?\s*(?:mg|μg|µg|mcg|g|IU|mIU)(?:\s*/\s*(?:day|d|kg)(?:\s*/\s*day)?)?(?:\s+(?:daily|per day))?\b",
        "response_time": r"\b(?:for|after|at|over|during)\s+\d+(?:\.\d+)?\s*(?:hours?|days?|weeks?|months?|years?)\b",
        "comparator": r"\b(?:compared (?:with|to)|versus|vs\.?)\s+(?:a\s+|the\s+)?(?:placebo(?:\s+group)?|control(?:\s+(?:diet|group))?|usual care|baseline)\b",
    }
    context = {"organism": _organism(text)}
    for key, pattern in patterns.items():
        values = []
        for sentence in sentences:
            if _PRIOR.search(sentence["text"]) or re.search(
                r"references?|bibliography", sentence["label"], re.I
            ):
                continue
            for match in re.finditer(pattern, sentence["text"], re.I):
                if key == "dose":
                    # Blood concentrations, measured assay values and food
                    # composition are not automatically intervention doses.
                    near = sentence["text"][
                        max(0, match.start() - 100) : min(len(sentence["text"]), match.end() + 100)
                    ]
                    if not re.search(
                        r"\b(?:dose|dosage|administer\w*|supplement\w*|receiv\w*|intake|given|consum\w*|treat\w*)\b",
                        near,
                        re.I,
                    ):
                        continue
                    if re.match(r"\s*/\s*(?:mL|L|dL)\b", sentence["text"][match.end() :], re.I):
                        continue
                if key == "response_time" and not re.search(
                    r"\b(?:intervention|treat\w*|supplement\w*|receiv\w*|given|consum\w*|follow[- ]?up|trial|after|measur\w*|blood pressure|expression)\b",
                    sentence["text"],
                    re.I,
                ):
                    continue
                values.append(_value(sentence, match))
        context[key] = _unique_value(values)
    return context


def _endpoint(mention):
    if mention.get("normalized_id"):
        return mention["normalized_id"]
    if mention["kind"] == "gene":
        payload = mention.get("payload", {})
        return (
            f"unresolved-gene:{payload.get('organism', 'unknown')}:{payload.get('symbol', mention['text'])}"
        )
    return mention["text"]


def _relations(sentences, mentions, metadata):
    publication_types = metadata.get("publication_types", [])
    if isinstance(publication_types, str):
        publication_types = [publication_types]
    is_review = any(_REVIEW.search(str(t)) for t in publication_types)
    # Titles identify review-only reports even if publication indexing is late.
    is_review = is_review or bool(
        re.search(
            r"\b(?:systematic review|meta-analysis|a review|scoping review)\b",
            metadata.get("title", ""),
            re.I,
        )
    )
    result, seen = [], set()
    if is_review:
        return result
    for sentence in sentences:
        line, offset = sentence["text"], sentence["start"]
        if _EXCLUDED_SECTION.search(sentence["label"]) or _PRIOR.search(line):
            continue
        entities = [
            m
            for m in mentions
            if offset <= m["start"]
            and m["end"] <= sentence["end"]
            and not m.get("payload", {}).get("ambiguous")
        ]
        between = re.search(
            r"\b(?:(?:no|non[- ]significant|no significant)\s+)?(?:association|relationship|correlation|effect)\b.{0,65}?\bbetween\b",
            line,
            re.I,
        )
        cues = list(_CUE.finditer(line))
        if between:
            cues.insert(0, between)
        # Clauses limit endpoints: separate verbs never create cross-products.
        for cue in cues:
            cue_start, cue_end = offset + cue.start(), offset + cue.end()
            left_boundary = (
                offset + max(line.rfind(";", 0, cue.start()), line.rfind(" but ", 0, cue.start())) + 1
            )
            right_candidates = [
                p for p in [line.find(";", cue.end()), line.find(" but ", cue.end())] if p >= 0
            ]
            right_boundary = offset + (min(right_candidates) if right_candidates else len(line))
            local = [m for m in entities if left_boundary <= m["start"] and m["end"] <= right_boundary]
            before = [m for m in local if m["end"] <= cue_start]
            after = [m for m in local if m["start"] >= cue_end]
            if cue is between:
                # Explicit "association between A and B" names both endpoints.
                if len(after) < 2:
                    continue
                subject, object_ = after[0], after[1]
                connector = line[subject["end"] - offset : object_["start"] - offset]
                if not re.search(r"\band\b", connector, re.I):
                    continue
            else:
                if not before or not after:
                    continue
                # Nearest explicit endpoints only. Coordination stays reviewable
                # rather than expanding one sentence into inferred pairings.
                subject = max(before, key=lambda m: m["end"])
                object_ = min(after, key=lambda m: m["start"])
                suffix = line[cue.end() : object_["start"] - offset]
                if re.search(r"\b(?:by|after|following|with)\s*$", suffix, re.I) and cue.lastgroup in {
                    "increase",
                    "decrease",
                    "effect",
                }:
                    # "BP was reduced by caffeine" preserves semantic direction.
                    subject, object_ = object_, subject
            if subject["kind"] == "outcome" and object_["kind"] == "outcome":
                continue
            if _endpoint(subject) == _endpoint(object_):
                continue
            relation_type = "association" if cue is between else cue.lastgroup
            if relation_type in {"increase", "decrease"}:
                predicate = "increases" if relation_type == "increase" else "decreases"
            else:
                predicate = "associated_with" if relation_type == "association" else "affects"
            clause = line[max(0, left_boundary - offset) : right_boundary - offset]
            # Modifiers must belong to this endpoint pair, not to an unrelated
            # outcome later in the same source sentence.
            frame_start = min(subject["start"], object_["start"], cue_start) - offset
            frame_end = max(subject["end"], object_["end"], cue_end) - offset
            frame = line[frame_start:frame_end]
            near = line[max(frame_start, cue.start() - 55) : min(frame_end, cue.end() + 30)]
            polarity = (
                "null" if _NULL.search(frame) else ("negated" if _NEGATION.search(near) else "positive")
            )
            speculative = bool(_SPECULATIVE.search(frame))
            fingerprint = (subject["start"], object_["start"], predicate, polarity, speculative)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            payload = {
                "explicit_relation": True,
                "relation_type": relation_type,
                "polarity": polarity,
                "speculative": speculative,
                "organism": _organism(line),
                "section": sentence["label"],
                "subject_mention": subject,
                "object_mention": object_,
                "cue": cue.group(),
                "cue_start": cue_start,
                "cue_end": cue_end,
                "rule_score": 1 + int(bool(_STAT.search(clause))),
                "interpretation": "source statement candidate; not a causal or confirmed scientific claim",
            }
            result.append(
                {
                    "subject": _endpoint(subject),
                    "predicate": predicate,
                    "object": _endpoint(object_),
                    "quote": line,
                    "start": offset,
                    "end": sentence["end"],
                    "rule_id": "explicit-nearest-endpoints-v1",
                    "payload": payload,
                }
            )
    return result


def _summary(sentences, assertions):
    usable = [
        s
        for s in sentences
        if not re.search(r"references?|bibliography", s["label"], re.I) and not _PRIOR.search(s["text"])
    ]
    methods = [
        s for s in usable if re.search(r"methods?|materials?", s["label"], re.I) or _METHOD.search(s["text"])
    ]
    limits = [s for s in usable if _LIMIT.search(s["text"])]
    relation_spans = {(a["start"], a["end"]) for a in assertions}
    candidates = []
    for sentence in usable:
        if re.search(r"introduction|background|methods?|materials?", sentence["label"], re.I):
            continue
        score = (
            3 * int(bool(re.search(r"results?|conclusions?|findings", sentence["label"], re.I)))
            + 2 * int((sentence["start"], sentence["end"]) in relation_spans)
            + 2 * int(bool(_CUE.search(sentence["text"]) or _NULL.search(sentence["text"])))
            + int(bool(_STAT.search(sentence["text"])))
        )
        if score >= 2 and not _LIMIT.search(sentence["text"]):
            candidates.append({**sentence, "rule_score": score})
    selected = sorted(
        sorted(candidates, key=lambda s: (-s["rule_score"], s["start"]))[:4], key=lambda s: s["start"]
    )
    return {
        "labels": {"method": "연구설계", "results": "주요 결과", "limitations": "한계"},
        "method": methods[0] if methods else UNKNOWN,
        "results": selected,
        "limitations": limits[0] if limits else UNKNOWN,
        "status": "extractive_candidate",
        "missing_results": not bool(selected),
        "notice": "자동 추출 후보입니다. 원문·연구설계·통계 해석을 검수해야 합니다.",
    }


def extract(text: str, sections: list[dict], metadata: dict, dictionaries) -> dict:
    sentences = sentence_spans(text, sections)
    context = _context(sentences, metadata)
    mentions = _entity_mentions(text, dictionaries)
    mentions += _gene_mentions(text, sentences, context["organism"], dictionaries)
    mentions.sort(key=lambda m: (m["start"], m["end"], m["kind"]))
    assertions = _relations(sentences, mentions, metadata)
    return {
        "engine_version": ENGINE_VERSION,
        "rules_version": RULES_VERSION,
        "dictionary_version": dictionaries.version,
        "dictionary_manifest": dictionaries.manifest,
        "summary": _summary(sentences, assertions),
        "context": context,
        "mentions": mentions,
        "assertions": assertions,
    }
