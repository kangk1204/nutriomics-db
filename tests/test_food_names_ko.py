"""Record binding, Korean search semantics, qualifier scope and cache updates."""
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

import pytest
from test_explorer_api import explorer as explorer  # noqa: PLC0414

from nutrigenomics import food_names
from scripts.build_food_names_ko import lexicon, translate

ROOT = Path(__file__).resolve().parents[1]
LEXICON = ROOT / 'config/food_names_ko/lexicon.txt'


def test_complete_catalog_keeps_exact_ids_and_original_name_hashes():
    data = food_names.catalog()
    assert data['count'] == len(data['by_id']) == 5794
    for record in data['items']:
        assert re.search('[가-힣]', record['name_ko'])
        assert hashlib.sha256(record['source_label'].encode()).hexdigest() == record['source_label_sha256']
        assert record['review_status'] == 'not_expert_reviewed'
        assert not {'amount', 'unit', 'nutrient', 'effect_estimate', 'expert_approved'} & record.keys()


@pytest.mark.parametrize('query,expected', [
    ('사과', 'Apple, raw'), ('바나나', 'Banana, raw'), ('닭가슴살', 'Chicken, breast, boneless, skinless, raw'),
    ('계란', 'Egg, whole, raw'), ('달걀', 'Egg, whole, raw'), ('흰쌀밥', 'Rice, white, cooked, no added fat'),
    ('현미밥', 'Rice, brown, cooked, no added fat'), ('무가당 아몬드', 'Almond milk, unsweetened'),
    ('아몬드 무가당', 'Almond milk, unsweetened'), ('고구마', 'Sweet potato'), ('두부', 'Tofu'),
    ('통밀빵', 'Bread, French or Vienna, whole wheat'),
    ('귤', 'Mandarin, seedless, peeled, raw'), ('탄산수', 'Water, carbonated, flavored'),
    ('볶음밥', 'Rice, fried'), ('녹차', 'Tea, hot, leaf, green'), ('홍차', 'Tea, hot, leaf, black'),
    ('김치', 'Kimchi'), ('브로콜리', 'Broccoli, raw'), ('호두', 'Walnuts'), ('참기름', 'Sesame oil'),
])
def test_common_food_queries_include_actual_source_records(query, expected):
    records = food_names.search_candidates(query)
    assert records and any(expected.casefold() in name.casefold() for _, name, _ in records)


@pytest.mark.parametrize('query,required', [('가당', 'sweetened'), ('무가당', 'unsweetened'),
    ('저지방우유', 'lowfat'), ('무지방우유', 'nonfat'), ('닭가슴살 생것', 'raw'), ('삶은 계란', 'boiled')])
def test_qualifier_search_does_not_infer_opposite_or_unknown_state(query, required):
    records = food_names.search_candidates(query)
    assert records
    for identifier, _, _ in records:
        assert required in food_names.catalog()['by_id'][identifier]['qualifiers']


def test_cooked_from_raw_is_not_a_raw_chicken_candidate():
    record = next(row for row in food_names.catalog()['items'] if row['source_label'] == 'Chicken breast, baked, broiled, or roasted, skin not eaten, from raw')
    assert 'raw' not in record['qualifiers']
    assert record['id'] not in {row[0] for row in food_names.search_candidates('닭가슴살 생것')}
    assert record['id'] in {row[0] for row in food_names.search_candidates(record['name_ko'])}


def test_raw_rice_is_not_discovered_as_bap_and_pear_is_not_cabbage():
    assert 'USDA-FDC:2710825' not in {row[0] for row in food_names.search_candidates('밥')}
    pears = food_names.search_candidates('배')
    assert pears and not any('cabbage' in label.casefold() or 'bass' in label.casefold() for _, label, _ in pears)
    whole = food_names.search_candidates('전란')
    assert whole and not any('egg, white' in label.casefold() for _, label, _ in whole)


@pytest.mark.parametrize('source,contains,absent', [
    ('Yogurt, NS as to type of milk, plain', '우유 종류 미상', '구성:'),
    ('Yogurt, NS as to type of milk or flavor', '우유 종류·맛 미상', '구성:'),
    ('Yogurt, Greek, low fat milk, flavors other than fruit', '과일 이외 향미', '기타 보다'),
    ('Beans, kidney, light red, canned', '강낭콩', '신장'),
    ('Chicken wings with hot sauce', '매운 소스', '뜨거운 소스'),
    ('Rice, brown, cooked, no added fat', '지방 무첨가', '지방 첨가'),
    ('Almond milk, unsweetened', '무가당', '우유'),
    ('Yogurt, Greek, nonfat', '그릭 요구르트', '저지방'),
])
def test_context_sensitive_phrases_and_negations(source, contains, absent):
    terms, pattern = lexicon(LEXICON)
    rendered, _ = translate(source, terms, pattern)
    assert contains in rendered and absent not in rendered


def test_nfd_whitespace_and_direct_foods_precede_ingredient_mentions():
    assert food_names.search_candidates('닭 가슴살') == food_names.search_candidates(unicodedata.normalize('NFD', '닭 가슴살'))
    matches = food_names.search_candidates('사과')
    raw = next(rank for _, label, rank in matches if label == 'Apple, raw')
    juice = next(rank for _, label, rank in matches if label == 'Apple juice, 100%')
    assert raw < juice


def write_catalog(path, rows):
    data = {'version': 'test-food-names', 'count': len(rows), 'source_release_id': 'test', 'purpose': 'display_and_search_only', 'items': rows}
    payload = json.dumps(data, ensure_ascii=False).encode()
    path.write_bytes(payload)
    path.with_suffix('.manifest.json').write_text(json.dumps({'catalog_sha256': hashlib.sha256(payload).hexdigest()}))


def example_record(label='쌀밥'):
    return {'id': 'food:rice', 'source_label': 'Rice, cooked', 'source_label_sha256': hashlib.sha256(b'Rice, cooked').hexdigest(),
        'name_ko': label, 'aliases_ko': [label], 'primary_aliases_ko': [label], 'qualifiers': [],
        'translation_status': 'composed_search_name', 'review_status': 'not_expert_reviewed', 'remaining_source_terms': []}


def test_source_label_mismatch_abstains_and_changed_catalog_invalidates_cached_response(explorer, tmp_path, monkeypatch):
    path = tmp_path / 'names.json'
    monkeypatch.setenv(food_names.CONFIG, str(path))
    record = example_record('검색명초기값')
    with sqlite3.connect(explorer['db']) as db:
        original = db.execute("SELECT label FROM node WHERE id='food:rice'").fetchone()[0]
    record['source_label'] = original
    record['source_label_sha256'] = hashlib.sha256(original.encode()).hexdigest()
    write_catalog(path, [record])
    client = explorer['client']
    route = '/api/v1/explorer/entities'
    before = client.get(route, params={'kind': 'food_record', 'q': '검색명초기값'}).json()
    assert before['total'] == 1 and before['items'][0]['label'] == original
    assert before['items'][0]['display_name_ko'] == '검색명초기값'
    record['name_ko'] = '검사새이름'
    record['aliases_ko'] = ['검사새이름']
    record['primary_aliases_ko'] = ['검사새이름']
    write_catalog(path, [record])
    assert client.get(route, params={'kind': 'food_record', 'q': '검색명초기값'}).json()['total'] == 0
    assert client.get(route, params={'kind': 'food_record', 'q': '검사새이름'}).json()['total'] == 1
    record['source_label'] = 'Different preparation'
    record['source_label_sha256'] = hashlib.sha256(record['source_label'].encode()).hexdigest()
    write_catalog(path, [record])
    assert client.get(route, params={'kind': 'food_record', 'q': '검사새이름'}).json()['total'] == 0


def test_catalog_corruption_is_a_failure_not_zero_results(explorer, tmp_path, monkeypatch):
    path = tmp_path / 'corrupt.json'
    monkeypatch.setenv(food_names.CONFIG, str(path))
    write_catalog(path, [example_record()])
    path.write_text('{}')
    response = explorer['client'].get('/api/v1/explorer/entities', params={'kind': 'food_record', 'q': '사과'})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'source_mismatch'


def test_missing_optional_catalog_does_not_change_english_search(explorer, tmp_path, monkeypatch):
    client = explorer['client']
    before = client.get('/api/v1/explorer/entities', params={'kind': 'food_record', 'q': 'rice'}).json()
    monkeypatch.setenv(food_names.CONFIG, str(tmp_path / 'absent.json'))
    after = client.get('/api/v1/explorer/entities', params={'kind': 'food_record', 'q': 'rice'}).json()
    assert before == after
