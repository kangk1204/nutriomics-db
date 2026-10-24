"""Source-anchored study/arm/quantity candidates; no document-wide dose guess."""

import re
from .extraction import UNKNOWN, _PRIOR

_HUMAN = re.compile(r"\b(?:humans?|patients?|participants?|volunteers?|adults?|men|women)\b", re.I)
_ANIMAL = re.compile(r"\b(?:mice|mouse|murine|rats?|rodents?|rabbits?|zebrafish|drosophila|animals?)\b", re.I)
_CELL = re.compile(r"\b(?:cells?|cellular|cell lines?|in vitro|cultured|HEK293|HUVEC)\b", re.I)
_DESIGN = re.compile(
    r"\b(?:randomi[sz]ed(?:\s+controlled)?\s+trial|cross[- ]sectional(?:\s+study)?|prospective\s+cohort|retrospective\s+cohort|cohort\s+study|case[- ]control(?:\s+study)?|systematic\s+review|meta-analysis)\b",
    re.I,
)
_QUANTITY = re.compile(
    r"(?<!\w)(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>mg|μg|µg|mcg|g|IU|mIU|ng|nmol|mmol)(?:\s*/\s*(?P<denominator>100\s*g|kg(?:\s*/\s*(?:day|d))?|day|d|mL|dL|L))?\b",
    re.I,
)
_TIME = re.compile(
    r"\b(?:for|after|at|over|during)\s+\d+(?:\.\d+)?\s*(?:hours?|days?|weeks?|months?|years?)\b", re.I
)
_COMPARATOR = re.compile(
    r"\b(?:compared (?:with|to)|versus|vs\.?)\s+(?:a\s+|the\s+)?(?:placebo(?:\s+group)?|control(?:\s+(?:diet|group))?|usual care|baseline)\b",
    re.I,
)
_ARM = re.compile(
    r"\b(?:[A-Za-z][\w-]*\s+)?(?:treatment|intervention|placebo|control|high[- ]dose|low[- ]dose)\s+(?:arm|group)\b|\b(?:arm|group)\s+[A-Z1-9]\b",
    re.I,
)
_STAT = re.compile(
    r"\bp\s*[<=>≤≥]\s*(?:0?\.\d+|\d+(?:\.\d+)?(?:\s*[×x]\s*10[-−]\d+)?)|\b(?:95|90|99)\s*%\s*(?:CI|confidence interval)\b",
    re.I,
)


def organism_model(line, fallback=None):
    human, animal, cell = bool(_HUMAN.search(line)), bool(_ANIMAL.search(line)), bool(_CELL.search(line))
    species = "mixed" if human and animal else "animal" if animal else "human" if human else "unknown"
    model = (
        "mixed"
        if cell
        and re.search(r"\b(?:patients?|participants?|volunteers?|adults?|in vivo|animal model)\b", line, re.I)
        else "cell"
        if cell
        else "animal"
        if animal
        else "clinical"
        if human
        else "unknown"
    )
    if fallback and species == "unknown" and model == "unknown":
        species, model = fallback
    return species, model


def anchored(sentence, match, **extra):
    return {
        "value": match.group(),
        "text": match.group(),
        "quote": match.group(),
        "start": sentence["start"] + match.start(),
        "end": sentence["start"] + match.end(),
        "source_quote": sentence["text"],
        "quote_start": sentence["start"],
        "quote_end": sentence["end"],
        **extra,
    }


def _owner(sentence, match, mentions):
    before = [
        m
        for m in mentions
        if m["kind"] in {"nutrient", "food", "diet"}
        and sentence["start"] <= m["start"] < m["end"] <= sentence["start"] + match.start()
    ]
    after = [
        m
        for m in mentions
        if m["kind"] in {"nutrient", "food", "diet"}
        and sentence["start"] + match.end() <= m["start"] < m["end"] <= sentence["end"]
    ]
    owners = []
    if before:
        candidate = max(before, key=lambda m: m["end"])
        gap = sentence["text"][candidate["end"] - sentence["start"] : match.start()]
        if re.fullmatch(r"\s*(?:at\s+|of\s+|a dose of\s+|\(\s*|was\s+|:|,)?\s*", gap, re.I):
            owners.append(candidate)
    if after:
        candidate = min(after, key=lambda m: m["start"])
        gap = sentence["text"][match.end() : candidate["start"] - sentence["start"]]
        if re.fullmatch(r"\s*(?:of\s+)?", gap, re.I):
            owners.append(candidate)
    unique = {(m["start"], m["end"]): m for m in owners}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _clause_interval(sentence, position):
    left, right = 0, len(sentence["text"])
    # Explicit independent-clause separators keep context from later clauses
    # from changing the kind of an earlier quantity. Simple coordination of
    # ingredients is deliberately not split here.
    for match in re.finditer(
        r";|\b(?:but|whereas|while)\b|\band(?=\s+(?:the\s+)?(?:group|arm)\s+[A-Z1-9])", sentence["text"], re.I
    ):
        if match.end() <= position:
            left = match.end()
        elif match.start() > position:
            right = match.start()
            break
    return left, right


def _linked_arm(sentence, match, arms):
    left, right = _clause_interval(sentence, match.start())
    local = [
        arm
        for arm in arms
        if sentence["start"] + left <= arm["start"] and arm["end"] <= sentence["start"] + right
    ]
    return local[0]["arm_id"] if len(local) == 1 else None


def studies_context(sentences, mentions, metadata=None):
    joined = " ".join(s["text"] for s in sentences if not _PRIOR.search(s["text"]))
    species, model = organism_model(joined)
    studies, all_quantities = [], []
    for sentence in sentences:
        if _PRIOR.search(sentence["text"]) or re.search(r"references?|bibliography", sentence["label"], re.I):
            continue
        designs = [anchored(sentence, m) for m in _DESIGN.finditer(sentence["text"])]
        arms = [
            anchored(
                sentence,
                m,
                arm_id="arm:"
                + re.sub(r"\W+", "-", re.sub(r"^(?:the|a|an)\s+", "", m.group().casefold())).strip("-"),
            )
            for m in _ARM.finditer(sentence["text"])
        ]
        participants = []
        for m in re.finditer(
            r"\b(?:\d+\s+(?:participants?|patients?|volunteers?|adults?|subjects|men|women)|n\s*=\s*\d+)\b",
            sentence["text"],
            re.I,
        ):
            arm_id = _linked_arm(sentence, m, arms)
            role = (
                "total"
                if re.search(r"\b(?:total|enroll\w*|recruit\w*)\b", sentence["text"], re.I) and not arms
                else "arm"
                if arm_id
                else "unknown"
            )
            participants.append(anchored(sentence, m, role=role, arm_id=arm_id))
        quantities = []
        for m in _QUANTITY.finditer(sentence["text"]):
            owner = _owner(sentence, m, mentions)
            denominator = (m.group("denominator") or "").casefold().replace(" ", "")
            clause_left, clause_right = _clause_interval(sentence, m.start())
            prefix = sentence["text"][clause_left : m.start()]
            if denominator in {"ml", "l", "dl"} or re.search(
                r"\b(?:serum|plasma|blood)\s+(?:\w+\s+){0,3}(?:concentrations?|levels?)\b",
                prefix,
                re.I,
            ):
                kind = "concentration"
            elif denominator == "100g" or re.search(r"\b(?:contain\w*|content|composition)\b", prefix, re.I):
                kind = "food_content"
            elif re.search(
                r"\b(?:dose|dosage|administer\w*|supplement\w*|receiv\w*|intake|given|consum\w*|treat\w*)\b",
                prefix,
                re.I,
            ):
                kind = "dose"
            else:
                kind = "unknown"
            if (
                kind == "food_content"
                and owner
                and owner["kind"] == "food"
                and any(
                    mention["kind"] == "nutrient"
                    and sentence["start"] + clause_left
                    <= mention["start"]
                    < mention["end"]
                    <= sentence["start"] + clause_right
                    for mention in mentions
                )
            ):
                owner = None
            value = float(m.group("number"))
            unit = m.group("unit")
            normalized = None
            # Mass conversions preserve dimensional denominator. IU, molarity,
            # and mass-to-molar conversions remain literal source quantities.
            scale = {"g": 1000.0, "mg": 1.0, "mcg": 0.001, "μg": 0.001, "µg": 0.001, "ng": 0.000001}
            if owner and kind != "unknown" and unit.casefold() in scale:
                normalized = {
                    "value": value * scale[unit.casefold()],
                    "unit": "mg",
                    "denominator": denominator or None,
                }
            quantities.append(
                anchored(
                    sentence,
                    m,
                    quantity_kind=kind,
                    number=value,
                    unit=unit,
                    denominator=denominator or None,
                    owner=owner,
                    owner_id=owner.get("normalized_id") if owner else None,
                    arm_id=_linked_arm(sentence, m, arms),
                    normalized=normalized,
                    status="candidate" if owner and kind != "unknown" else "needs_review",
                )
            )
        timepoints = [anchored(sentence, m) for m in _TIME.finditer(sentence["text"])]
        comparators = [anchored(sentence, m) for m in _COMPARATOR.finditer(sentence["text"])]
        statistics = [anchored(sentence, m) for m in _STAT.finditer(sentence["text"])]
        local_species, local_model = organism_model(sentence["text"])
        if designs or arms or participants or quantities or timepoints or comparators or statistics:
            studies.append(
                {
                    "study_id": f"study-span:{sentence['start']}:{sentence['end']}",
                    "start": sentence["start"],
                    "end": sentence["end"],
                    "quote": sentence["text"],
                    "designs": designs,
                    "arms": arms,
                    "participants": participants,
                    "quantities": quantities,
                    "timepoints": timepoints,
                    "comparators": comparators,
                    "statistics": statistics,
                    "species": local_species,
                    "study_model": local_model,
                }
            )
            all_quantities.extend(quantities)

    def one(values):
        unique = {v["value"]: v for v in values}
        return (
            next(iter(unique.values()))
            if len(unique) == 1
            else {
                "value": UNKNOWN,
                "candidates": values,
                "reason": "multiple explicit values; use event-linked conditions",
            }
            if unique
            else UNKNOWN
        )

    context = {
        "organism": species,
        "species": species,
        "study_model": model,
        "study_design": one([v for s in studies for v in s["designs"]]),
        "participants": one([v for s in studies for v in s["participants"]]),
        "dose": one([v for v in all_quantities if v["quantity_kind"] == "dose" and v["owner"]]),
        "response_time": one([v for s in studies for v in s["timepoints"]]),
        "comparator": one([v for s in studies for v in s["comparators"]]),
        "tissue": UNKNOWN,
    }
    return studies, context


def conditions_for(sentence, controller, scope, studies):
    local = [s for s in studies if s["start"] == sentence["start"] and s["end"] == sentence["end"]]
    fields = {"arm_id": None, "comparator": None, "timepoint": None, "quantities": [], "statistics": []}
    for study in local:

        def in_scope(value):
            return scope["start"] <= value["start"] and value["end"] <= scope["end"]

        arms = [v for v in study["arms"] if in_scope(v)]
        fields["arm_id"] = arms[0]["arm_id"] if len(arms) == 1 else None
        for source_key, target_key in [("comparators", "comparator"), ("timepoints", "timepoint")]:
            values = [v for v in study[source_key] if in_scope(v)]
            fields[target_key] = values[0] if len(values) == 1 else None
        fields["quantities"] += [
            v
            for v in study["quantities"]
            if in_scope(v)
            and v["owner"]
            and v["owner"]["start"] == controller["start"]
            and v["owner"]["end"] == controller["end"]
            and v["status"] == "candidate"
        ]
        fields["statistics"] += [v for v in study["statistics"] if in_scope(v)]
    return fields
