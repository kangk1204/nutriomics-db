import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_explorer_api import explorer as explorer  # noqa: PLC0414

from nutrigenomics.explorer_build import sha256
from nutrigenomics.kfind import NUTRIENT_CROSSWALK
from nutrigenomics.korean_dishes import add_dishes, build, selected_rows
from nutrigenomics.public_service import create_app


def raw_row():
    return {**{key: '1' for key in NUTRIENT_CROSSWALK}, 'FOOD_CD': 'D106-test',
            'FOOD_NM': '된장찌개', 'NUT_CON_SRTR_QUA': '100g', 'DATA_PROD_NM': '분석',
            'SRC_NM': '식품의약품안전처', 'FOOD_ORIGIN_NM': '가정식(분석함량)',
            'CRTR_YMD': '2026-09-29', 'CRT_YMD': '2012-02-28', 'FOOD_SIZE': '400g'}


def selection():
    return {'dataset_id': '15100070', 'foods': [{'food_code': 'D106-test', 'source_name': '된장찌개',
                                               'label': '된장찌개', 'aliases': ['된장 찌개']}]}


def write_raw(path, rows):
    path.mkdir()
    columns = {'tableVO': {'colNmList': list(raw_row())}}
    files = {'columns.json': json.dumps(columns).encode(), 'page-0001.json': json.dumps(rows).encode()}
    for name, body in files.items():
        (path / name).write_bytes(body)
    manifest = {'dataset_id': '15100070', 'page_count': 1, 'page_record_counts': [len(rows)],
                'total_count': len(rows), 'provider_reference_date_max': '2026-09-29',
                'downloaded_utc': '2026-10-23T00:00:00+00:00',
                'source_page': 'https://www.data.go.kr/data/15100070/standard.do',
                'sha256': {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}}
    (path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


@pytest.mark.parametrize('change', [
    {'FOOD_NM': '다른 조리법'}, {'NUT_CON_SRTR_QUA': '100ml'}, {'DATA_PROD_NM': '산출'},
    {'SRC_NM': '다른 제공기관'}, {'FOOD_NM': '된장찌개_간편조리세트'},
])
def test_source_name_basis_method_and_provider_must_match(tmp_path, change):
    write_raw(tmp_path / 'raw', [{**raw_row(), **change}])
    with pytest.raises(ValueError):
        selected_rows(tmp_path / 'raw', selection())


def test_duplicate_codes_and_changed_file_hash_fail_before_build(tmp_path):
    path = tmp_path / 'raw'
    write_raw(path, [raw_row(), raw_row()])
    with pytest.raises(ValueError, match='Duplicated'):
        selected_rows(path, selection())
    (path / 'page-0001.json').write_text('[]')
    with pytest.raises(ValueError, match='hash mismatch'):
        selected_rows(path, selection())


def test_native_food_values_zero_missing_and_existing_effects_are_preserved(tmp_path):
    row = {**raw_row(), 'ENERC': '46', 'NAT': '318', 'VITD': '', 'FE': '0.00'}
    manifest = write_raw(tmp_path / 'raw', [row])
    _, rows = selected_rows(tmp_path / 'raw', selection())
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE source(id TEXT PRIMARY KEY,metadata_json TEXT);
        CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT,label TEXT,label_en TEXT,aliases_json TEXT,metadata_json TEXT,domains_json TEXT,degree INTEGER);
        CREATE TABLE node_kind(id TEXT PRIMARY KEY,kind TEXT);
        CREATE TABLE measurement(id TEXT PRIMARY KEY,food TEXT,component TEXT,amount REAL,unit TEXT,basis TEXT,status TEXT,source_id TEXT,raw_json TEXT);
        CREATE TABLE edge(id TEXT PRIMARY KEY,subject TEXT,object TEXT,relation TEXT,tier TEXT,review_status TEXT,taxon TEXT,context_json TEXT,source_ids_json TEXT,pmids_json TEXT,domains_json TEXT);
        CREATE TABLE composition_assessment(measurement_id TEXT,eligible INTEGER,method TEXT,assessment_json TEXT);
        CREATE TABLE effect(key TEXT,value REAL); INSERT INTO effect VALUES('original-five-part-key',0.123456789);
    ''')
    for nutrient in NUTRIENT_CROSSWALK.values():
        db.execute("INSERT INTO node VALUES(?,'nutrient',?,NULL,'[]','{}','[]',0)",
                   ('FDCNutrient:' + nutrient['fdc_id'], nutrient['name_en']))
    old = [tuple(r) for r in db.execute('SELECT * FROM effect')]
    audit = add_dishes(db, manifest, rows, 'a' * 64)
    assert audit['measurements_added'] == 24 and audit['biological_or_health_edges_added'] == 0
    assert db.execute("SELECT amount FROM measurement WHERE component='FDCNutrient:1093'").fetchone()[0] == 318
    missing = db.execute("SELECT amount,status FROM measurement WHERE component='FDCNutrient:1114'").fetchone()
    assert tuple(missing) == (None, 'not_measured')
    zero = db.execute("SELECT amount,status FROM measurement WHERE component='FDCNutrient:1089'").fetchone()
    assert tuple(zero) == (0, 'measured_zero')
    assert json.loads(db.execute('SELECT metadata_json FROM source').fetchone()[0])['export_allowed'] is False
    assert json.loads(db.execute("SELECT metadata_json FROM node WHERE kind='food'").fetchone()[0])['data_created_date'] == '2012-02-28'
    assert [tuple(r) for r in db.execute('SELECT * FROM effect')] == old
    assert {r[0] for r in db.execute('SELECT DISTINCT relation FROM edge')} == {'contains_nutrient'}
    db.close()


def test_local_research_release_cannot_download_the_mixed_rights_database(explorer, tmp_path):
    folder = tmp_path / 'local'
    folder.mkdir()
    database = folder / 'nutriomics.sqlite3'
    with sqlite3.connect(explorer['db']) as source, sqlite3.connect(database) as target:
        source.backup(target)
        for key, value in {'scope': 'local_research', 'bulk_download_allowed': False}.items():
            target.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))
    (folder / 'dataset-metadata.json').write_text(json.dumps({'release_id': 'local', 'scope': 'local_research',
        'bulk_download_allowed': False, 'expert_review': 'pending'}))
    (folder / 'manifest.json').write_text(json.dumps({'database_sha256': sha256(database)}))
    with TestClient(create_app(folder, allow_candidate=True)) as client:
        health = client.get('/healthz')
        assert health.json()['scope'] == 'local_research'
        assert health.headers['X-NutriOmics-Scope'] == 'local_research'
        listing = client.get('/api/v1/public/files').json()
        assert listing['availability'] == 'withheld_for_local_source_rights'
        assert client.get('/api/v1/public/files/nutriomics.sqlite3').status_code == 403


def test_frozen_dish_selection_keeps_variant_labels_and_unique_source_codes():
    path = Path(__file__).resolve().parents[1] / 'config/korean_dishes_20261023.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    assert len(value['foods']) == len({r['food_code'] for r in value['foods']}) == 50
    tofu = next(r for r in value['foods'] if r['food_code'] == 'D106-291030000-0001')
    assert tofu['source_name'] == '순두부찌개_김치' and '김치' in tofu['label']


def test_builder_handles_existing_graph_indexes_without_changing_parent(explorer, tmp_path):
    from nutrigenomics.explorer_performance_build import add_graph_read_indexes

    parent = tmp_path / 'parent'
    parent.mkdir()
    source_path = parent / 'nutriomics.sqlite3'
    with sqlite3.connect(explorer['db']) as source, sqlite3.connect(source_path) as db:
        source.backup(db)
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE node_kind(id TEXT PRIMARY KEY,kind TEXT);
            CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT);
            CREATE TABLE composition_assessment(measurement_id TEXT,eligible INTEGER,method TEXT,assessment_json TEXT);
        ''')
        for nutrient in NUTRIENT_CROSSWALK.values():
            db.execute("INSERT OR IGNORE INTO node VALUES(?,'nutrient',?,NULL,'[]','{}','[]',0)",
                       ('FDCNutrient:' + nutrient['fdc_id'], nutrient['name_en']))
        for key, value in {'quality': {'composition': {}}, 'status': 'candidate_pending_expert_review'}.items():
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))
        add_graph_read_indexes(db, 'parent')
    original_hash = sha256(source_path)
    (parent / 'manifest.json').write_text(json.dumps({'release_id': 'parent', 'database_sha256': original_hash}))
    (parent / 'dataset-metadata.json').write_text('{}')
    raw = tmp_path / 'raw'
    write_raw(raw, [raw_row()])
    selected = tmp_path / 'selection.json'
    selected.write_text(json.dumps(selection()), encoding='utf-8')
    result = build(parent, raw, selected, tmp_path / 'derived', 'local-derived')
    assert sha256(source_path) == original_hash
    assert result['scope'] == 'local_research' and result['bulk_download_allowed'] is False
    with sqlite3.connect(tmp_path / 'derived/nutriomics.sqlite3') as db:
        assert db.execute("SELECT count(*) FROM perf_adj WHERE subject LIKE 'K-FIND:%'").fetchone()[0] == 24
