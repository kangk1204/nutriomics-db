"""Read-only Korean food-name overlay, bound to exact source IDs and labels."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from .explorer_context import configured_path
from .settings import PROJECT_ROOT

CONFIG = 'NUTRIGENOMICS_FOOD_NAMES_FILE'
DEFAULT = PROJECT_ROOT / 'config/food_names_ko/catalog.json'
QUALIFIERS = {
    '무가당': 'unsweetened', '가당': 'sweetened', '무설탕': 'sugar_free', '슈거프리': 'sugar_free',
    '설탕무첨가': 'no_added_sugar', '무지방': 'nonfat', '저지방': 'lowfat', '생것': 'raw',
    '삶은': 'boiled', '찐것': 'steamed', '튀김': 'fried', '튀긴': 'fried', '구운': 'baked',
    '껍질제거': 'skinless', '뼈제거': 'boneless', '지방무첨가': 'no_added_fat', '지방첨가': 'fat_added',
    '무염': 'unsalted', '가염': 'salted', '볶은': 'roasted', '구이': 'baked', '다진': 'ground', '갈은': 'ground',
}
QUALIFIER_PATTERN = re.compile('|'.join(re.escape(k) for k in sorted(QUALIFIERS, key=lambda k: (-len(k), k))))


def normalize(value):
    return re.sub(r'[^가-힣a-z0-9]', '', unicodedata.normalize('NFC', value).casefold())


def paths():
    path = configured_path(CONFIG, DEFAULT).resolve()
    return path, path.with_suffix('.manifest.json')


def identity():
    return tuple((str(path), (path.stat().st_size, path.stat().st_mtime_ns) if path.is_file() else None) for path in paths())


@lru_cache(maxsize=4)
def _load(key):
    path = Path(key[0][0])
    if key[0][1] is None:
        return None
    manifest_path = Path(key[1][0])
    if not manifest_path.is_file():
        raise ValueError('Korean name catalog manifest missing')
    payload = path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if hashlib.sha256(payload).hexdigest() != manifest['catalog_sha256']:
        raise ValueError('Korean name catalog checksum mismatch')
    data = json.loads(payload)
    if data['count'] != len(data['items']) or len({r['id'] for r in data['items']}) != data['count']:
        raise ValueError('Korean name catalog has missing or duplicate records')
    for row in data['items']:
        if hashlib.sha256(row['source_label'].encode()).hexdigest() != row['source_label_sha256']:
            raise ValueError('Korean name source-label binding mismatch')
    for row in data['items']:
        row['_terms'] = {normalize(s) for s in [row['name_ko'], *row['aliases_ko']] if s}
        row['_primary'] = {normalize(s) for s in row['primary_aliases_ko'] if s}
        row['_text'] = normalize(row['name_ko'])
    return {**data, 'sha256': manifest['catalog_sha256'], 'by_id': {r['id']: r for r in data['items']}}


def catalog():
    try:
        return _load(identity())
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Imported only on failure, keeping the cache module's dependency acyclic.
        from .explorer_performance import QueryUnavailable
        raise QueryUnavailable('source_mismatch') from error


def meta():
    data = catalog()
    return {'available': bool(data), **({'version': data['version'], 'sha256': data['sha256'],
            'source_release_id': data['source_release_id'], 'records': data['count'],
            'purpose': data['purpose'], 'expert_review': 'not_performed'} if data else {})}


def annotate(node):
    if node.get('kind') != 'food':
        return node
    data = catalog()
    row = data['by_id'].get(node['id']) if data else None
    if row is None or row['source_label'] != node['label']:
        return node
    return {**node, 'display_name_ko': row['name_ko'], 'search_aliases_ko': row['aliases_ko'],
            'name_translation': {'version': data['version'], 'sha256': data['sha256'],
                'source_label_sha256': row['source_label_sha256'], 'status': row['translation_status'],
                'review_status': row['review_status'], 'remaining_source_terms': row['remaining_source_terms'],
                'purpose': 'display_and_search_only'}}


def _rank(row, query):
    if query in row['_primary']:
        return 0
    if query in row['_terms']:
        return 1
    return 2


def compound_match(word, terms):
    """Allow spacing-free combinations of recorded Korean food terms only."""
    if any(word in value for value in terms):
        return True
    if len(word) > 80:
        return False
    pieces = [value for value in terms if (len(value) >= 2 or value in {'닭', '밥', '콩', '무', '배', '밀', '차'}) and len(value) <= len(word)]
    reachable = {0}
    for index in range(len(word)):
        if index in reachable:
            reachable.update(index + len(piece) for piece in pieces if word.startswith(piece, index))
    return len(word) in reachable


@lru_cache(maxsize=128)
def _candidates(key, query):
    data = _load(key)
    if data is None:
        return ()
    compact = normalize(query)
    required = {QUALIFIERS[m[0]] for m in QUALIFIER_PATTERN.finditer(compact)}
    words = [part for word in unicodedata.normalize('NFC', query).split()
             for part in QUALIFIER_PATTERN.sub(' ', normalize(word)).split() if part]
    matches = []
    for row in data['items']:
        if compact == row['_text']:
            matches.append((row['id'], row['source_label'], 0))
            continue
        if not required.issubset(row['qualifiers']):
            continue
        if len(compact) == 1:
            found = compact in row['_terms']
        else:
            found = compact in row['_terms'] or compact in row['_text'] or all(
                compound_match(word, row['_terms']) for word in words)
        if found:
            matches.append((row['id'], row['source_label'], _rank(row, compact)))
    return tuple(matches)


def search_candidates(query):
    if not re.search('[가-힣ᄀ-ᇿ]', query) or not normalize(query):
        return ()
    try:
        return _candidates(identity(), query)
    except (OSError, ValueError, KeyError, TypeError) as error:
        from .explorer_performance import QueryUnavailable
        raise QueryUnavailable('source_mismatch') from error


def verified_matches(db, query):
    candidates = search_candidates(query)
    if not candidates:
        return ()
    expected = {identifier: (label, rank) for identifier, label, rank in candidates}
    # Check the current source, even if a prior release used the same food ID.
    matches = []
    for start in range(0, len(candidates), 500):
        block = candidates[start:start + 500]
        rows = db.execute("SELECT id,label FROM node WHERE kind='food' AND id IN (" + ','.join('?' for _ in block) + ')', [r[0] for r in block])
        for identifier, label in rows:
            if expected[identifier][0] == label:
                matches.append((identifier, expected[identifier][1]))
    return tuple(sorted(matches, key=lambda r: (r[1], r[0])))
