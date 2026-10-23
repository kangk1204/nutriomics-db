"""Small, versioned Korean search vocabulary; never a food identity crosswalk.

Source labels and nutrition records remain unchanged. Product-family keywords
return separate candidates and must not establish trial-product equivalence.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata

VERSION = "food-search-ko-20261023-v1"
VOCABULARY = (
    {"terms": ("김치",), "mode": "source_record", "english": "Kimchi",
     "food_id": "USDA-FDC:2710077", "expected_label": "Kimchi",
     "source_url": "https://fdc.nal.usda.gov/food-details/2710077/nutrients"},
    {"terms": ("요구르트", "요거트"), "mode": "food_family_keyword", "english": "Yogurt"},
)
SHA256 = hashlib.sha256(json.dumps(VOCABULARY, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def context(query: str) -> dict | None:
    term = unicodedata.normalize("NFC", query.strip())
    record = next((row for row in VOCABULARY if term in row["terms"]), None)
    if record is None:
        return None
    return {"version": VERSION, "sha256": SHA256, "query": query,
            "matched_term": term, "mode": record["mode"], "english": record["english"],
            **{key: record[key] for key in ("food_id", "expected_label", "source_url") if key in record},
            "purpose": "search_only", "scientific_review": "not_assessed",
            "note": "검색용 명칭입니다. 같은 식품·중재 제품이라는 판정이나 과학적 검수를 뜻하지 않습니다."}


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_clause(query: str, *, indexed: bool = False, korean_foods: bool = True) -> tuple[str, list]:
    """Original literal substring semantics OR narrowly scoped Korean matches.

    FTS is a candidate filter inside each branch; applying the Korean FTS
    phrase to the English-name branch would incorrectly discard every match.
    Domain, evidence, kind, order and pagination remain caller constraints.
    """
    search = query.strip()
    term = _like(search)
    original = "(n.label LIKE ? ESCAPE '\\' OR n.label_en LIKE ? ESCAPE '\\' OR n.id LIKE ? ESCAPE '\\' OR n.aliases_json LIKE ? ESCAPE '\\')"
    args = [term] * 4
    if indexed and len(search) >= 3:
        original += " AND n.id IN (SELECT c.node_id FROM perf_search f JOIN perf_search_content c ON c.rowid=f.rowid WHERE perf_search MATCH ?)"
        args.append('"' + search.replace('"', '""') + '"')
    mapping = context(query) if korean_foods else None
    if mapping is None:
        return "(" + original + ")", args
    if mapping["mode"] == "source_record":
        extra = "(n.kind='food' AND n.id=? AND n.label=?)"
        args.extend([mapping["food_id"], mapping["expected_label"]])
    else:
        extra = "(n.kind='food' AND (n.label LIKE ? ESCAPE '\\' OR n.label_en LIKE ? ESCAPE '\\'))"
        args.extend([_like(mapping["english"])] * 2)
    return "((" + original + ") OR " + extra + ")", args
