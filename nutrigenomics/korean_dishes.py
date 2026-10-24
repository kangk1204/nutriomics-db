"""Exact-source Korean dish selection and a separate local research derivative."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from scripts.import_kfind_foods import load_dataset

from .composition_policy import assess
from .explorer_build import canonical, sha256
from .explorer_performance_build import SCHEMA as GRAPH_SCHEMA
from .explorer_performance_build import add_graph_read_indexes
from .kfind import NUTRIENT_CROSSWALK, food_identity, parse_source_value
from .release_contract import (
    build_release_contract,
    install_release_contract,
    metadata_with_contract,
)


def selected_rows(raw_directory, selection):
    manifest, rows = load_dataset(Path(raw_directory))
    if manifest['dataset_id'] != selection['dataset_id']:
        raise ValueError('Dataset identity mismatch')
    index = {}
    for row in rows:
        index.setdefault(row['FOOD_CD'], []).append(row)
    chosen = []
    seen = set()
    for item in selection['foods']:
        key = item['food_code']
        matches = index.get(key, [])
        if key in seen or len(matches) != 1:
            raise ValueError('Duplicated or absent exact source food code: ' + key)
        seen.add(key)
        row = matches[0]
        if (row['FOOD_NM'] != item['source_name'] or row['NUT_CON_SRTR_QUA'] != '100g'
                or row.get('DATA_PROD_NM') != '분석' or row.get('SRC_NM') != '식품의약품안전처'
                or '간편조리세트' in row['FOOD_NM']):
            raise ValueError('Name, basis, source or preparation selection mismatch: ' + key)
        chosen.append((item, row))
    return manifest, chosen


def add_dishes(db, manifest, chosen, selection_sha):
    raw_hashes = manifest['sha256']
    source_hash = hashlib.sha256(canonical(raw_hashes).encode()).hexdigest()
    source_id = 'K-FIND-local:15100070:' + source_hash[:16]
    version = 'K-FIND-15100070-' + manifest['provider_reference_date_max']
    policy = {'source': '식품의약품안전처 / 전국통합식품영양성분정보(음식)표준데이터',
              'source_url': manifest['source_page'], 'release': version, 'version': version,
              'sha256': source_hash, 'raw_file_hashes': raw_hashes,
              'downloaded_utc': manifest['downloaded_utc'], 'export_allowed': False,
              'license': 'redistribution-unconfirmed', 'access_status': 'public_download',
              'restriction_reason': '연구실 조회용. 해당 표준데이터 release의 재배포 조건이 명시적으로 확인되기 전까지 값 내보내기 제외.',
              'policy_note': 'The separate 15127578 API license is not transferred to this 15100070 release.'}
    db.execute('INSERT INTO source VALUES(?,?)', (source_id, canonical(policy)))
    added = []
    for item, raw in chosen:
        food_id, _ = food_identity('15100070', raw['FOOD_CD'], raw['FOOD_NM'])
        payload_hash = hashlib.sha256(canonical(raw).encode()).hexdigest()
        info = {'public_food_id': food_id, 'calculation_food_id': food_id,
                'provider_food_code': raw['FOOD_CD'], 'provider_food_name': raw['FOOD_NM'],
                'source_record_sha256': payload_hash, 'source_identity_sha256': hashlib.sha256(raw['FOOD_NM'].encode()).hexdigest(),
                'preparation_state': '혼합 음식·조리 후',
                'edible_state': '조리 후 가식부', 'composition_mode': 'direct',
                'basis': 'per_100g_edible_portion', 'source_basis': raw['NUT_CON_SRTR_QUA'],
                'source_production_method': raw['DATA_PROD_NM'],
                'source_food_origin': raw['FOOD_ORIGIN_NM'],
                'reference_date': raw['CRTR_YMD'], 'data_created_date': raw.get('CRT_YMD'),
                'source_portion_reference': raw.get('FOOD_SIZE'),
                'source_portion_is_density': False, 'source_id': source_id,
                'selection_sha256': selection_sha, 'local_food_composition': True,
                'redistribution_status': 'unconfirmed', 'expert_review_status': 'pending',
                'interpretation': '선택한 원본 음식의 분석 조성. 동일 이름의 모든 조리법이나 중재 제품과 같은 음식으로 합치지 않음.'}
        aliases = sorted(set(item['aliases'] + [item['source_name']]))
        db.execute('INSERT INTO node VALUES(?,?,?,?,?,?,?,?)',
                   (food_id, 'food', item['label'], None, canonical(aliases), canonical(info), '[]', len(NUTRIENT_CROSSWALK)))
        db.execute('INSERT INTO node_kind VALUES(?,?)', (food_id, 'food'))
        for field, nutrient in NUTRIENT_CROSSWALK.items():
            component = 'FDCNutrient:' + nutrient['fdc_id']
            if db.execute('SELECT 1 FROM node WHERE id=?', (component,)).fetchone() is None:
                raise ValueError('Missing existing nutrient concept: ' + component)
            value = parse_source_value(raw.get(field))
            amount = float(value.amount) if value.amount is not None else None
            identifier = hashlib.sha256(canonical([source_id, food_id, field]).encode()).hexdigest()
            context = {'source_field': field, 'raw_value': value.raw_value, 'release': version,
                       'source_ref': manifest['source_page'] + '#' + raw['FOOD_CD'],
                       'source_record_sha256': payload_hash, 'source_basis': raw['NUT_CON_SRTR_QUA'],
                       'source_data_created_date': raw.get('CRT_YMD'),
                       'nutrient_mapping': 'nutrient_concept_crosswalk_not_single_molecule_identity',
                       'expert_review_status': 'pending'}
            db.execute('INSERT INTO measurement VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier, food_id, component, amount, nutrient['unit'], 'per_100g_edible_portion', value.status, source_id, canonical(context)))
            edge_context = {'measurement_id': identifier, 'source_status': value.status,
                            'interpretation': 'Food composition only; not a dietary intervention or health effect'}
            db.execute('INSERT INTO edge VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier, food_id, component, 'contains_nutrient', 'observed_composition', 'pending',
                        None, canonical(edge_context), canonical([source_id]), '[]', '[]'))
            assessment = assess(status=value.status, amount=amount, unit=nutrient['unit'],
                                basis='per_100g_edible_portion', metadata=context, food_metadata=info, source_id=source_id)
            db.execute('INSERT INTO composition_assessment VALUES(?,?,?,?)',
                       (identifier, int(assessment['eligible']), assessment['derivation_method'], canonical(assessment)))
        added.append({'id': food_id, 'code': raw['FOOD_CD'], 'source_name': raw['FOOD_NM'],
                      'label': item['label'], 'aliases': aliases, 'source_record_sha256': payload_hash})
    component_ids = ['FDCNutrient:' + n['fdc_id'] for n in NUTRIENT_CROSSWALK.values()]
    for component in component_ids:
        db.execute('UPDATE node SET degree=degree+? WHERE id=?', (len(chosen), component))
    return {'source_id': source_id, 'source': policy, 'foods': added,
            'food_records_added': len(added), 'measurements_added': len(added) * len(NUTRIENT_CROSSWALK),
            'biological_or_health_edges_added': 0, 'effect_rows_modified': 0, 'automatic_expert_approval': False}


def build(parent, raw_directory, selection_path, output, release_id):
    parent, output = Path(parent).resolve(), Path(output).resolve()
    staging = output.with_name(output.name + '.building')
    if output.exists() or staging.exists():
        raise FileExistsError('Existing releases are immutable')
    selection_path = Path(selection_path)
    selection = json.loads(selection_path.read_text(encoding='utf-8'))
    raw_manifest, chosen = selected_rows(raw_directory, selection)
    manifest = json.loads((parent / 'manifest.json').read_text(encoding='utf-8'))
    staging.mkdir(parents=True)
    print(json.dumps({'phase': 'copy_parent_database'}), flush=True)
    # Bounded, hash-checked byte copy, not a live/WAL file copy.
    from .composed_release import _copy_verified
    database = parent / 'nutriomics.sqlite3'
    _copy_verified(database, staging / 'nutriomics.sqlite3',
                   {'bytes': database.stat().st_size, 'sha256': manifest['database_sha256']})
    for path in parent.rglob('*'):
        if not path.is_file() or path.name in {'nutriomics.sqlite3', 'manifest.json', 'dataset-metadata.json'}:
            continue
        destination = staging / path.relative_to(parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Unchanged inherited attachments are immutable; own metadata is written separately.
        os.link(path, destination)
    with closing(sqlite3.connect(staging / 'nutriomics.sqlite3')) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA cache_size=-262144')
        protected = {'effect', 'effect_selector', 'effect_catalog', 'study_effect', 'science_record',
                     'curation_record_digest', 'scientific_review_decision', 'source_classification',
                     'scientific_review_dependency', 'workbench_evidence'}
        def guard(action, table, column, schema, trigger):
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE} and table in protected:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(guard)
        old = {k: json.loads(v) for k, v in db.execute('SELECT key,value_json FROM meta')}
        audit = add_dishes(db, raw_manifest, chosen, sha256(selection_path))
        db.commit()
        print(json.dumps({'phase': 'rebuild_graph_read_indexes', 'foods_added': len(chosen)}), flush=True)
        graph_schema = GRAPH_SCHEMA[GRAPH_SCHEMA.index('CREATE TABLE perf_exposure_match'):]
        for name in re.findall(r'CREATE INDEX (\w+)', graph_schema):
            db.execute('DROP INDEX IF EXISTS ' + name)
        for name in ['perf_search', 'perf_search_content', 'perf_adj', 'perf_exposure_match']:
            db.execute('DROP TABLE ' + name)
        graph = add_graph_read_indexes(db, release_id)
        quality = json.loads(canonical(old['quality']))
        quality['composition'].update(measurements=db.execute('SELECT count(*) FROM measurement').fetchone()[0],
            food_count=db.execute("SELECT count(*) FROM node WHERE kind='food'").fetchone()[0],
            eligible_foods=db.execute('SELECT count(DISTINCT m.food) FROM measurement m JOIN composition_assessment a ON a.measurement_id=m.id WHERE a.eligible=1').fetchone()[0],
            source_states=dict(db.execute('SELECT status,count(*) FROM measurement GROUP BY status')),
            methods=dict(db.execute('SELECT method,count(*) FROM composition_assessment GROUP BY method')))
        # New unquantified values are intentionally retained, never converted to zero.
        exclusions = Counter()
        for row in db.execute('SELECT assessment_json FROM composition_assessment WHERE eligible=0'):
            exclusions.update(json.loads(row[0])['exclusion_reasons'])
        quality['composition']['exclusion_reasons'] = dict(exclusions)
        db.execute("DELETE FROM meta WHERE key='public_counts'")
        current = {**old, 'release_id': release_id, 'dataset_release_id': release_id, 'service_build_id': release_id,
                   'scope': 'local_research', 'bulk_download_allowed': False,
                   'local_sources_note': '식약처 공식 조성 50종은 연구실 조회용이며 재배포 조건 확인 전 값 내보내기를 제외합니다.',
                   'quality': quality, 'graph_performance': graph, 'local_food_extension': audit,
                   'parent_release_id': manifest['release_id'], 'parent_database_sha256': manifest['database_sha256'],
                   'created_at': datetime.now(UTC).isoformat(), 'submission_ready': False}
        current.pop('public_counts', None)
        for k, v in current.items():
            if k not in {'release_contract', 'counts', 'public_counts'}:
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (k, canonical(v)))
        contract = build_release_contract(db, release_id=release_id, scope='local_research', previous_metadata=current,
                                          manifest={'parent_release_id': manifest['release_id'], 'parent_database_sha256': manifest['database_sha256']})
        contract.update(dataset_release_id=release_id, service_build_id=release_id,
                        composition=old.get('composition'), review_summary=old.get('review_summary'),
                        status=old['status'], submission_ready=False, expert_review=old.get('expert_review'))
        install_release_contract(db, contract)
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Derived release integrity check failed')
        metadata = metadata_with_contract(db, current)
        db.set_authorizer(None)
    (staging / 'dataset-metadata.json').write_text(canonical(metadata) + '\n', encoding='utf-8')
    (staging / 'local-food-extension.json').write_text(canonical(audit) + '\n', encoding='utf-8')
    (staging / 'local-food-selection.json').write_bytes(selection_path.read_bytes())
    digest = sha256(staging / 'nutriomics.sqlite3')
    # No mixed-rights SQLite or inherited stale table archive is exposed as public download.
    final_manifest = {**manifest, 'release_id': release_id, 'database_sha256': digest,
                      'parent_release_id': manifest['release_id'], 'parent_database_sha256': manifest['database_sha256'],
                      'scope': 'local_research', 'bulk_download_allowed': False, 'files': [],
                      'local_extension': {'selection_sha256': sha256(selection_path),
                                          'raw_manifest_sha256': sha256(Path(raw_directory) / 'manifest.json'),
                                          'foods': len(chosen), 'measurements': audit['measurements_added']}}
    (staging / 'manifest.json').write_text(canonical(final_manifest) + '\n', encoding='utf-8')
    staging.rename(output)
    print(json.dumps({'status': 'built', 'release_id': release_id, 'database_sha256': digest,
                      'foods_added': len(chosen), 'effect_rows_modified': 0}), flush=True)
    return metadata
