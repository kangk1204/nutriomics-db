"""Independent original-output numeric preservation and primary-source food/API checks."""
import argparse
import csv
import gzip
import hashlib
import json
import math
import sqlite3
import time
import urllib.request
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from nutrigenomics.explorer_build import read_only, sha256
from nutrigenomics.kfind import NUTRIENT_CROSSWALK
from nutrigenomics.source_updates import atomic_json

KEY = ('feature_type', 'feature_id', 'contrast')
NUMERIC = ('effect_estimate', 'standard_error', 'p_value', 'q_value', 'n_people', 'n_samples')


def number(value, integer=False):
    if value is None or str(value) in {'', 'NA', 'NaN'}:
        return None
    result = int(value) if integer else float(value)
    if not math.isfinite(result):
        return None
    return 0 if result == 0 else result


def row_hash(row):
    values = [row[k] for k in KEY] + [number(row[k], k.startswith('n_')) for k in NUMERIC]
    return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).digest()


def numeric_outputs(work, core, output):
    work, core, output = Path(work), Path(core), Path(output)
    plan = json.loads((work / 'analysis-rebuild-plan.json').read_text())
    plan_sha = sha256(work / 'analysis-rebuild-plan.json')
    core_sha = sha256(core)
    wal = Path(str(core) + '-wal')
    if wal.exists() and wal.stat().st_size > 32:
        raise ValueError('Numeric audit requires a frozen snapshot without pending WAL frames')
    reports = []
    with read_only(core) as db:
        db.execute('PRAGMA cache_size=-524288')
        for item in plan['outcomes']:
            folder = work / 'outputs' / item['folder']
            metadata = json.loads((folder / 'analysis_metadata.json').read_text())
            ready = json.loads((folder / 'DONE.json').read_text())
            files = metadata.get('result_files', ['gene_effects.tsv.gz', 'pathway_effects.tsv.gz'])
            if isinstance(files, str):
                files = [files]
            hashes = {r['path']: r['sha256'] for r in ready['files']}
            expected = []
            for name in files:
                if sha256(folder / name) != hashes[name]:
                    raise ValueError('Executed result hash changed')
                with gzip.open(folder / name, 'rt', newline='') as stream:
                    expected.extend(row_hash(row) for row in csv.DictReader(stream, delimiter='\t'))
            actual = [row_hash(row) for row in db.execute(
                'SELECT ' + ','.join((*KEY, *NUMERIC)) + ' FROM study_effect WHERE series_accession=? AND analysis_version=?',
                (item['series'], item['analysis_version']))]
            expected.sort()
            actual.sort()
            match = expected == actual
            reports.append({'series': item['series'], 'analysis_version': item['analysis_version'],
                'source_rows': len(expected), 'core_rows': len(actual), 'values_match': match,
                'completion_sha256': sha256(folder / 'DONE.json'),
                'digest': hashlib.sha256(b''.join(expected)).hexdigest()})
            atomic_json(output, {'status': 'in_progress', 'analyses': reports,
                                'core_snapshot_sha256': core_sha, 'analysis_plan_sha256': plan_sha})
            if not match:
                raise ValueError('Original executed numeric outputs differ from core')
    if sha256(core) != core_sha or sha256(work / 'analysis-rebuild-plan.json') != plan_sha or (wal.exists() and wal.stat().st_size > 32):
        raise ValueError('Frozen numeric audit inputs changed during validation')
    result = {'status': 'passed', 'analyses': reports, 'rows': sum(r['source_rows'] for r in reports),
              'core_snapshot_sha256': core_sha, 'analysis_plan_sha256': plan_sha,
              'fields': list((*KEY, *NUMERIC)), 'scope': 'All executed result values; biological interpretation remains subject to expert review'}
    atomic_json(output, result)
    return result


def food_api(work, source, core, base, output):
    work, source, core, output = map(Path, (work, source, core, output))
    selection = json.loads((source / 'config/kfind_food_selection.json').read_text())['foods']
    primary = {}
    for dataset in {str(item['dataset_id']) for item in selection}:
        rows = []
        folder = work / 'data/raw/kfind' / dataset
        manifest = json.loads((folder / 'manifest.json').read_text())
        for name, digest in manifest['sha256'].items():
            if sha256(folder / name) != digest:
                raise ValueError('Primary K-FIND source hash changed')
            if name.startswith('page-'):
                rows.extend(json.loads((folder / name).read_text()))
        primary[dataset] = rows
    differences, inputs, totals = [], [], defaultdict(float)
    rows_checked = 0
    with read_only(core) as db:
        for index, food in enumerate(selection):
            candidates = [r for r in primary[str(food['dataset_id'])] if r['FOOD_CD'] == food['food_code'] and r['FOOD_NM'] == food['food_name']]
            if len(candidates) != 1:
                raise ValueError('Primary food identity is ambiguous')
            raw = candidates[0]
            if str(raw['NUT_CON_SRTR_QUA']).strip().lower().replace(' ', '') not in {'100g', '100.0g'}:
                raise ValueError('Selected source does not report 100 g mass basis')
            found = [dict(r) for r in db.execute("SELECT * FROM food WHERE source_id='R02'")
                     if json.loads(r['metadata_json'])['provider_food_code'] == food['food_code']
                     and json.loads(r['metadata_json'])['provider_food_name'] == food['food_name']]
            if len(found) != 1:
                raise ValueError('Core food identity differs from primary source')
            grams = 80 + 3 * index
            inputs.append({'food_id': found[0]['id'], 'amount_g': grams})
            for field, definition in NUTRIENT_CROSSWALK.items():
                token = '' if raw.get(field) is None else str(raw[field]).strip()
                if token in {'', '-', 'NA', 'N/A'}:
                    expected, status = None, 'not_measured'
                else:
                    expected = float(Decimal(token.replace(',', '')))
                    status = 'measured_zero' if expected == 0 else 'measured'
                key = 'FDC-NUTRIENT:' + str(definition['fdc_id'])
                row = db.execute('SELECT cm.* FROM composition_measurement cm JOIN component c ON c.id=cm.component_id WHERE cm.food_id=? AND c.component_key=? AND cm.release=?',
                    (found[0]['id'], key, found[0]['active_release'])).fetchone()
                rows_checked += 1
                if row is None or row['amount_per_100g'] != expected or row['status'] != status or row['basis'] != 'source_100g' or row['unit'] != definition['unit']:
                    differences.append({'food': food['food_code'], 'component': key, 'reason': 'source_value_unit_basis_or_status'})
                if expected is not None:
                    totals[(key, definition['unit'])] += expected * grams / 100
    atomic_json(output, {'status': 'in_progress', 'phase': 'primary_source_core',
                        'food_count': len(inputs), 'composition_count': rows_checked, 'differences': differences})
    batches = []
    for name, batch in [('single', inputs[:1]), ('meal', inputs[:5]), ('all50', inputs)]:
        req = urllib.request.Request(base + '/api/v1/meals/analyze', data=json.dumps({'items': batch}).encode(),
                                     headers={'Content-Type': 'application/json'})
        start = time.monotonic()
        with urllib.request.urlopen(req, timeout=60) as response:
            meal = json.load(response)
            batches.append({'name': name, 'items': len(batch), 'seconds': time.monotonic() - start,
                            'http_status': response.status,
                            'payload_sha256': hashlib.sha256(req.data).hexdigest()})
        if name == 'all50':
            actual = {(r['component_key'], r['unit']): r['amount'] for r in meal['components']}
            for key, value in totals.items():
                if key not in actual or not math.isclose(actual[key], value, rel_tol=1e-10, abs_tol=1e-8):
                    differences.append({'component': key, 'reason': 'API_weighted_sum'})
            if len(meal['items']) != 50 or set(actual) != set(totals):
                differences.append({'reason': 'API_item_or_component_scope'})
        atomic_json(output, {'status': 'in_progress', 'phase': name, 'food_count': 50,
                            'composition_count': rows_checked, 'differences': differences})
    result = {'status': 'passed' if not differences else 'failed', 'food_count': 50,
              'composition_count': rows_checked, 'differences': differences,
              'api_batches': batches, 'core_snapshot_sha256': sha256(core),
              'food_selection_sha256': sha256(source / 'config/kfind_food_selection.json'),
              'scope': 'Fresh DATA.GO.KR original rows to core and weighted API calculation; not edible-state biological validation'}
    atomic_json(output, result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['numeric', 'food'])
    for name in ('work', 'source', 'core', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--base', default='http://127.0.0.1:8031')
    a = p.parse_args()
    result = numeric_outputs(a.work, a.core, a.output) if a.mode == 'numeric' else food_api(a.work, a.source, a.core, a.base, a.output)
    raise SystemExit(0 if result['status'] == 'passed' else 1)
