"""Build a record-bound Korean search catalog without modifying source data."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

VERSION = "food-names-ko-20261023-v2"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def lexicon(path):
    text = ";".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#"))
    terms = {}
    for item in text.split(";"):
        if not item.strip():
            continue
        keys, value = item.strip().split("=", 1)
        for key in keys.split("|"):
            key = unicodedata.normalize("NFC", key.strip()).casefold()
            if key in terms and terms[key] != value.strip():
                raise ValueError("Conflicting term: " + key)
            terms[key] = value.strip()
    pattern = re.compile(r"(?<![A-Za-z0-9'\-])(?:" + "|".join(re.escape(k) for k in sorted(terms, key=lambda k: (-len(k), k))) + r")(?![A-Za-z0-9'\-])", re.IGNORECASE)
    return terms, pattern


def translate(text, terms, pattern):
    rendered = pattern.sub(lambda match: terms[match[0].casefold()], text)
    if re.match(r'^tea,', text, re.IGNORECASE):
        if re.search(r'\bgreen\b', text, re.IGNORECASE):
            rendered = rendered.replace('녹색', '녹차')
        if re.search(r'\bblack\b', text, re.IGNORECASE):
            rendered = rendered.replace('검정', '홍차')
    rendered = re.sub(r"\s+", " ", rendered).strip(" ,")
    remaining = re.findall(r"[A-Za-z][A-Za-z'-]*", rendered)
    return rendered, remaining


def aliases(food, terms, pattern, synonym_groups):
    """Aliases broaden discovery only, while preparation qualifiers stay exact."""
    label = food['label']
    replacements = {terms[m[0].casefold()] for m in pattern.finditer(label)
                    if not (m[0].casefold() == 'raw' and re.search(r'from\s+$', label[:m.start()], re.IGNORECASE))}
    root = translate(label.split(',')[0], terms, pattern)[0]
    first = next(pattern.finditer(label), None)
    primary = {root}
    if first is not None and first.start() == 0 and first.end() >= len(label.split(',')[0]):
        primary.add(terms[first[0].casefold()])
    names = {v for v in replacements | primary if re.search('[가-힣]', v)}
    # Groups are directed: "전란" can be found with "계란", but searching
    # "전란" must not turn an explicitly white-only record into a whole egg.
    expansions = {group[0]: group[1:] for group in synonym_groups}
    for target in (names, primary):
        for value in list(target):
            target.update(expansions.get(value, ()))
    lower = label.casefold()
    category = food.get('metadata', {}).get('food_category', '')
    if 'rice' in lower and (', cooked' in lower or category == 'Rice mixed dishes'):
        names.add('밥')
        if lower.startswith('rice, white,'):
            names.update(('쌀밥', '흰쌀밥', '백미밥'))
        if lower.startswith('rice, brown,'):
            names.add('현미밥')
    if re.search(r'(?:^|,\s*)raw(?:,|$)', lower):
        if lower.startswith(('chicken breast', 'chicken, breast')):
            names.update(('생닭가슴살', '닭가슴살 생것'))
        if lower.startswith(('egg, whole', 'egg, raw')):
            names.update(('날달걀', '날계란'))
    if lower.startswith('egg, whole, fried'):
        names.update(('계란프라이', '계란후라이', '달걀프라이'))
    if lower.startswith('bread,') and re.search(r'\bwhole wheat\b', lower):
        names.add('통밀빵')
    if lower.startswith('water, carbonated'):
        names.add('탄산수')
    if lower.startswith('tea,'):
        if re.search(r'\bgreen\b', lower):
            names.add('녹차')
        if re.search(r'\bblack\b', lower):
            names.add('홍차')
    return sorted(names), sorted(primary)


def qualifiers(label):
    text = re.sub(r'\bfrom raw\b', '', label.casefold())
    result = set()
    rules = {
        'raw': r'\braw\b', 'sweetened': r'\bsweetened\b', 'unsweetened': r'\bunsweetened\b',
        'sugar_free': r'\bsugar[ -]free\b', 'no_added_sugar': r'\bno (?:added sugar|sugar added)\b',
        'nonfat': r'\b(?:nonfat|fat[ -]free)\b', 'lowfat': r'\blow[ -]?fat\b',
        'boiled': r'\bboiled\b', 'steamed': r'\bsteamed\b',
        'fried': r'\bfried\b', 'baked': r'\b(?:baked|broiled|roasted|grilled|toasted)\b',
        'skinless': r'\bskinless\b', 'boneless': r'\bboneless\b',
        'no_added_fat': r'\bno added fat\b', 'fat_added': r'\bfat added\b',
        'unsalted': r'\bunsalted\b', 'salted': r'\bsalted\b', 'roasted': r'\broasted\b', 'ground': r'\bground\b',
    }
    for key, expression in rules.items():
        if re.search(expression, text):
            result.add(key)
    return sorted(result)


def build(inventory, vocabulary, output, synonyms=None):
    original = inventory.read_bytes()
    data = json.loads(original)
    terms, pattern = lexicon(vocabulary)
    synonym_data = json.loads(synonyms.read_text(encoding='utf-8')) if synonyms else {'groups': []}
    missing = Counter()
    records = []
    for food in data["items"]:
        name, remaining = translate(food["label"], terms, pattern)
        names, primary = aliases(food, terms, pattern, synonym_data['groups'])
        missing.update(token.casefold() for token in remaining)
        records.append({"id": food["id"], "source_label": food["label"], "source_label_sha256": sha(food["label"].encode()),
                        "name_ko": name, "aliases_ko": names, "primary_aliases_ko": primary,
                        "primary_name_ko": translate(food['label'].split(',')[0], terms, pattern)[0],
                        "qualifiers": qualifiers(food['label']),
                        "remaining_source_terms": sorted(set(remaining)),
                        "translation_status": "composed_search_name", "review_status": "not_expert_reviewed"})
    catalog = {"version": VERSION, "source_release_id": data["release_id"], "source_inventory_sha256": sha(original),
               "lexicon_sha256": sha(vocabulary.read_bytes()), "purpose": "display_and_search_only",
               "synonyms_sha256": sha(synonyms.read_bytes()) if synonyms else None,
               "license": "CC-BY-4.0 for original Korean vocabulary and search-name curation; source names retain source terms",
               "count": len(records), "items": records}
    payload = canonical(catalog)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    manifest = {"version": VERSION, "catalog_sha256": sha(payload), "source_release_id": data["release_id"],
                "count": len(records), "has_hangul": sum(bool(re.search('[가-힣]', r['name_ko'])) for r in records),
                "rows_with_remaining_latin_terms": sum(bool(r['remaining_source_terms']) for r in records),
                "remaining_terms": missing.most_common(), "expert_review": "not_performed"}
    output.with_suffix('.manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in manifest.items() if k != 'remaining_terms'}, ensure_ascii=False))
    print(json.dumps(missing.most_common(), ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--lexicon', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--synonyms', type=Path)
    args = parser.parse_args()
    build(args.inventory, args.lexicon, args.output, args.synonyms)
