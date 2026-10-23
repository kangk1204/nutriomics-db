"""Source-bound gene names. Naming evidence never implies a dietary effect."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from .explorer_context import configured_path
from .explorer_performance import QueryUnavailable

CONFIG = 'NUTRIGENOMICS_GENE_NAMES_FILE'
VERSION = 'gene-names-20261023-v1'
SCHEMA = '''
CREATE TABLE header(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE record(hgnc_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE binding(node_id TEXT PRIMARY KEY,hgnc_id TEXT NOT NULL,source_label TEXT,metadata_sha256 TEXT);
CREATE INDEX binding_gene ON binding(hgnc_id,node_id);
CREATE TABLE term(id INTEGER PRIMARY KEY,hgnc_id TEXT NOT NULL,name TEXT NOT NULL,norm TEXT NOT NULL,kind TEXT NOT NULL);
CREATE INDEX term_exact ON term(norm,hgnc_id);
CREATE INDEX term_gene ON term(hgnc_id);
CREATE VIRTUAL TABLE term_fts USING fts5(norm,content='term',content_rowid='id',tokenize='trigram');
'''


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def normalize(value):
    text = unicodedata.normalize('NFKC', str(value)).casefold()
    for letter, word in {'α': 'alpha', 'β': 'beta', 'γ': 'gamma', 'δ': 'delta', 'κ': 'kappa'}.items():
        text = text.replace(letter, word)
    return ''.join(c for c in text if c.isalnum())


def build(raw, inventory, korean, contracts, output):
    """Produce an immutable catalog; never updates the source graph or effects."""
    raw, inventory, korean, contracts, output = map(Path, (raw, inventory, korean, contracts, output))
    temporary = output.with_suffix(output.suffix + '.building')
    if output.exists() or temporary.exists():
        raise FileExistsError('Name catalogs are immutable; choose a new output')
    foods = json.loads(inventory.read_text(encoding='utf-8'))
    local = json.loads(korean.read_text(encoding='utf-8'))
    namespaces = json.loads(contracts.read_text(encoding='utf-8'))
    source_root = contracts.resolve().parent.parent
    for item in namespaces['contracts']:
        files = [(item.get('source_file'), item['script_sha256'])]
        files.extend((r['source_file'], r['sha256']) for r in item.get('namespace_evidence', []))
        for name, expected in files:
            path = (source_root / str(name)).resolve()
            if not path.is_relative_to(source_root) or not path.is_file() or digest(path) != expected:
                raise ValueError('Gene feature namespace producer code mismatch')
    with raw.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        required = {'hgnc_id', 'symbol', 'name', 'status', 'entrez_id', 'ensembl_gene_id'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('HGNC schema changed')
        records = [r for r in reader if r['status'] == 'Approved']
    if len({r['hgnc_id'] for r in records}) != len(records):
        raise ValueError('Duplicated HGNC IDs')
    owners = defaultdict(set)
    for r in records:
        for value in [r['symbol'], *r.get('prev_symbol', '').split('|')]:
            if value:
                owners[value].add(r['hgnc_id'])
    ko = {r['hgnc_id']: r for r in local['items']}
    if len(ko) != len(local['items']):
        raise ValueError('Duplicated authored Korean gene identity')
    by_native = defaultdict(set)
    by_hgnc = {}
    terms = set()
    for r in records:
        hid = r['hgnc_id']
        for key, prefix in [('hgnc_id', ''), ('entrez_id', 'NCBIGene:'), ('ensembl_gene_id', 'Ensembl:')]:
            if r.get(key):
                by_native[prefix + r[key]].add(hid)
        k = ko.get(hid)
        if k and (k['symbol'] != r['symbol'] or k['approved_name'] != r['name']):
            raise ValueError('Authored Korean terminology is stale: ' + hid)
        values = [('approved_symbol', r['symbol']), ('approved_name', r['name']), ('identifier', hid)]
        for key in ['alias_symbol', 'alias_name', 'prev_symbol', 'prev_name']:
            values.extend((key, name) for name in r.get(key, '').split('|') if name)
        for key, prefix in [('entrez_id', 'NCBIGene:'), ('ensembl_gene_id', 'Ensembl:')]:
            if r.get(key):
                values.extend([('identifier', prefix + r[key]), ('identifier', r[key])])
        if k:
            values.extend([('authored_korean_name', k['name_ko']), *(('authored_search_alias', s) for s in k['aliases'])])
        by_hgnc[hid] = {
            'hgnc_id': hid, 'approved_symbol': r['symbol'], 'approved_name': r['name'],
            'entrez_id': r.get('entrez_id') or None, 'ensembl_gene_id': r.get('ensembl_gene_id') or None,
            'symbol_features': sorted(s for s in [r['symbol'], *r.get('prev_symbol', '').split('|')]
                                      if s and owners[s] == {hid}),
            'name_ko': k['name_ko'] if k else None,
            'korean_status': 'authored_search_terminology_not_official_translation' if k else 'not_available',
            'expert_review_status': 'pending' if k else 'not_performed',
            'terms': [{'name': name, 'kind': kind} for kind, name in values],
            'source_url': 'https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/' + hid,
            'source_record_sha256': hashlib.sha256(canonical(r).encode()).hexdigest(),
        }
        terms.update((hid, name, normalize(name), kind) for kind, name in values if normalize(name))
    unknown_ko = set(ko) - set(by_hgnc)
    if unknown_ko:
        raise ValueError('Korean entries without approved HGNC identity')
    bindings, excluded = [], []
    for node in foods['items']:
        candidates = by_native.get(node['id'], set())
        declared = node.get('metadata', {}).get('hgnc_id')
        if len(candidates) != 1 or (declared and declared not in candidates):
            excluded.append({'id': node['id'], 'reason': 'no_unambiguous_current_official_identifier'})
            continue
        hid = next(iter(candidates))
        bindings.append((node['id'], hid, node['label'], node['metadata_sha256']))
    header = {'version': VERSION, 'dataset_release_id': foods['release_id'],
              'database_sha256': foods['database_sha256'], 'hgnc_sha256': digest(raw),
              'inventory_sha256': digest(inventory), 'korean_sha256': digest(korean),
              'namespace_contract_sha256': digest(contracts), 'namespace_contracts': namespaces['contracts'],
              'source_url': 'https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt',
              'terms_url': 'https://www.genenames.org/about/license/', 'license': 'CC0-1.0',
              'approved_hgnc_records': len(records), 'bound_native_gene_records': len(bindings),
              'native_gene_records': len(foods['items']), 'unbound_native_gene_records': len(excluded),
              'authored_korean_genes': len(ko), 'terms': len(terms),
              'scope': 'Official human gene names and exact native identifiers; Korean terminology is authored for search only',
              'excluded_bindings': excluded}
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(temporary)) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO header VALUES(?,?)', ('catalog', canonical(header)))
        db.executemany('INSERT INTO record VALUES(?,?)', [(k, canonical(v)) for k, v in by_hgnc.items()])
        db.executemany('INSERT INTO binding VALUES(?,?,?,?)', bindings)
        db.executemany('INSERT INTO term(hgnc_id,name,norm,kind) VALUES(?,?,?,?)', sorted(terms))
        db.execute("INSERT INTO term_fts(term_fts) VALUES('rebuild')")
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Catalog integrity failure')
    temporary.replace(output)
    receipt = {**header, 'catalog_sha256': digest(output), 'catalog_bytes': output.stat().st_size}
    output.with_suffix('.manifest.json').write_text(canonical(receipt), encoding='utf-8')
    return receipt


def default_path():
    return Path(__file__).resolve().parents[1] / 'config/gene_names/catalog.sqlite3'


def paths():
    path = configured_path(CONFIG, default_path())
    return path, path.with_suffix('.manifest.json')


def identity():
    return tuple((str(p.resolve()), (p.stat().st_size, p.stat().st_mtime_ns) if p.is_file() else None) for p in paths())


@lru_cache(maxsize=2)
def _load(key):
    if key[0][1] is None:
        return None
    path = Path(key[0][0])
    manifest = json.loads(Path(key[1][0]).read_text(encoding='utf-8'))
    if digest(path) != manifest['catalog_sha256']:
        raise ValueError('Gene catalog checksum mismatch')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        header = json.loads(db.execute("SELECT value FROM header WHERE key='catalog'").fetchone()[0])
        if any(header.get(k) != manifest.get(k) for k in ['version', 'database_sha256', 'hgnc_sha256', 'dataset_release_id']):
            raise ValueError('Gene catalog manifest/header mismatch')
        records = {k: json.loads(v) for k, v in db.execute('SELECT * FROM record')}
        bindings = {r[0]: r[1:] for r in db.execute('SELECT * FROM binding')}
    return {**header, 'catalog_sha256': manifest['catalog_sha256'], 'records': records, 'bindings': bindings,
            'path': str(path), 'identity': key}


def catalog():
    try:
        key = identity()
        if key[0][1] is None and Path(key[0][0]) != default_path().resolve() and Path(key[0][0]).name != 'gene-names-unconfigured.sqlite3':
            raise QueryUnavailable('source_unavailable')
        return _load(key)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        raise QueryUnavailable('source_mismatch') from error


def meta():
    data = catalog()
    return {'available': bool(data), **({k: data[k] for k in ['version', 'catalog_sha256', 'dataset_release_id',
        'hgnc_sha256', 'source_url', 'license', 'bound_native_gene_records', 'native_gene_records',
        'unbound_native_gene_records', 'authored_korean_genes', 'terms', 'scope']} if data else {})}


def annotate(node, query=None, source_metadata=None):
    if node.get('kind') != 'gene':
        return node
    data = catalog()
    binding = data['bindings'].get(node['id']) if data else None
    if binding is None:
        return node
    source_hash = hashlib.sha256(canonical(node.get('metadata', {}) if source_metadata is None else source_metadata).encode()).hexdigest()
    if (node.get('label'), source_hash) != (binding[1], binding[2]):
        raise QueryUnavailable('source_mismatch')
    record = data['records'][binding[0]]
    exact = [r for r in record['terms'] if query and normalize(r['name']) == normalize(query)]
    names = {**record, 'version': data['version'], 'catalog_sha256': data['catalog_sha256'],
             'hgnc_sha256': data['hgnc_sha256'], 'matched_names': exact,
             'identity_status': 'exact_official_identifier', 'taxon': '9606',
             'interpretation': 'Names and identifier correspondence only; not a dietary effect or expert review'}
    return {**node, 'gene_names': names}


def attach(db):
    data = catalog()
    if data is None:
        return False
    release = db.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()
    if release is None or json.loads(release[0]) != data['dataset_release_id']:
        raise QueryUnavailable('source_mismatch')
    names = {r[1] for r in db.execute('PRAGMA database_list')}
    if 'gene_names_catalog' not in names:
        db.execute('ATTACH DATABASE ? AS gene_names_catalog', (Path(data['path']).resolve().as_uri() + '?mode=ro',))
    return True


def search_sql(query):
    key = normalize(query)
    if not key:
        return 'SELECT NULL WHERE 0', []
    where, args = 'instr(t.norm,?)>0', [key]
    if len(key) >= 3:
        where += ' AND t.id IN (SELECT rowid FROM gene_names_catalog.term_fts WHERE term_fts MATCH ?)'
        args.append('"' + key.replace('"', '""') + '"')
    return ('SELECT b.node_id FROM gene_names_catalog.binding b JOIN gene_names_catalog.term t '
            'ON t.hgnc_id=b.hgnc_id WHERE ' + where), args


def priority_sql(query):
    # Exact symbols/IDs/names precede aliases, then original substring matches.
    return ("CASE WHEN EXISTS(SELECT 1 FROM gene_names_catalog.binding b JOIN gene_names_catalog.term t ON t.hgnc_id=b.hgnc_id WHERE b.node_id=n.id AND t.norm=? AND t.kind IN ('approved_symbol','approved_name','identifier','authored_korean_name')) THEN 0 "
            "WHEN EXISTS(SELECT 1 FROM gene_names_catalog.binding b JOIN gene_names_catalog.term t ON t.hgnc_id=b.hgnc_id WHERE b.node_id=n.id AND t.norm=?) THEN 1 ELSE 2 END,"), [normalize(query)] * 2


def search_context(db, query):
    ids = [r[0] for r in db.execute('SELECT DISTINCT hgnc_id FROM gene_names_catalog.term WHERE norm=?', (normalize(query),))]
    return {'query': query, 'normalization': 'NFKC, casefold, punctuation and spacing, spelled Greek letters',
            'exact_hgnc_candidates': len(ids), 'ambiguous': len(ids) > 1,
            'note': '공통 별칭은 여러 유전자 후보로 표시합니다. 한글명은 검색용 자체 용어이며 공식 한글명이나 전문가 승인을 뜻하지 않습니다.'}


def feature_contract(contrast):
    data = catalog()
    if not data:
        return None
    return next((r for r in data['namespace_contracts'] if all(r.get(k) == contrast.get(k)
                 for k in ['series_accession', 'analysis_version'])), None)


def feature_identifiers(node, contrast=None):
    names = node.get('gene_names')
    if not names or names.get('identity_status') != 'exact_official_identifier':
        return None
    values = {'HGNCsymbol': names['symbol_features'], 'NCBIGene': [names.get('entrez_id')],
              'Ensembl': [names.get('ensembl_gene_id')]}
    contract = feature_contract(contrast) if contrast else None
    selected = values.get(contract['namespace'], []) if contract else [v for block in values.values() for v in block]
    return list(dict.fromkeys(v for v in selected if v))


def feature_identity(node, effect):
    contract = feature_contract(effect)
    names = node.get('gene_names')
    if not contract or not names or effect.get('feature_type') != contract['feature_type']:
        return None
    if effect.get('feature_id') not in (feature_identifiers(node, effect) or []):
        return None
    return {'status': 'source_identifier_match', 'label': '공식 유전자 ID·분석 namespace 대응',
            'proof': {'hgnc_id': names['hgnc_id'], 'source_id': 'HGNC:' + names['hgnc_sha256'][:16],
                      'source_version': names['hgnc_sha256'], 'catalog_sha256': names['catalog_sha256'],
                      'namespace': contract['namespace'], 'taxon': '9606', 'verified': True,
                      'analysis_version': effect['analysis_version'], 'producer_script_sha256': contract['script_sha256']},
            'note': '명칭·측정 식별자 대응입니다. 식이 효과의 승인이나 인과성 검증이 아닙니다.'}
