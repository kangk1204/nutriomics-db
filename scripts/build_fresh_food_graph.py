"""Build a source graph from freshly ingested FDC records, without a parent DB."""
import argparse
import csv
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from nutrigenomics.explorer_build import canonical, read_only, sha256, verify_closed_sqlite
from nutrigenomics.source_updates import atomic_json

SCHEMA = """
CREATE TABLE source(id TEXT PRIMARY KEY,metadata_json TEXT NOT NULL);
CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT,name TEXT,metadata_json TEXT);
CREATE TABLE edge(id TEXT PRIMARY KEY,subject TEXT,object TEXT,relation TEXT,tier TEXT,taxon TEXT,context_json TEXT);
CREATE TABLE edge_source(edge_id TEXT,source_id TEXT);
CREATE TABLE citation(edge_id TEXT,pmid TEXT);
CREATE TABLE measurement(id TEXT PRIMARY KEY,food TEXT,nutrient TEXT,amount REAL,unit TEXT,basis TEXT,status TEXT,source_id TEXT,raw_json TEXT);
"""


def add_observed_genes(src, dst, hgnc):
    """Exact approved identifier matches only; no inferred chemical/gene edges."""
    index = {}
    with Path(hgnc).open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream, delimiter='\t'):
            if row['status'] != 'Approved':
                continue
            for key in (row['symbol'], row.get('ensembl_gene_id')):
                if key:
                    index.setdefault(key, {})[row['hgnc_id']] = row
    selected, unknown = {}, 0
    for series, feature_type, native in src.execute("SELECT DISTINCT series_accession,feature_type,feature_id FROM study_effect WHERE feature_type IN ('gene','gene_expression','expression','transcript')"):
        candidates = index.get(native, {})
        if len(candidates) != 1:
            unknown += 1
            continue
        row = next(iter(candidates.values()))
        target = ('NCBIGene:' + row['entrez_id']) if row.get('entrez_id', '').isdigit() else row['hgnc_id']
        value = selected.setdefault(row['hgnc_id'], {'reference': row, 'features': set(), 'mappings': []})
        value['features'].add(native)
        value['mappings'].append({'feature_id': native, 'series_accession': series,
            'feature_type': feature_type, 'target_id': target, 'taxon': '9606',
            'target_taxon': '9606', 'namespace': 'Ensembl' if native.startswith('ENSG') else 'HGNCsymbol',
            'verified': True})
    digest = sha256(Path(hgnc))
    source_id = 'HGNC:' + digest[:16]
    dst.execute('INSERT INTO source VALUES(?,?)', (source_id, canonical({
        'source': 'HGNC approved gene identifiers', 'sha256': digest, 'version': digest, 'license': 'CC0-1.0',
        'source_url': 'https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt',
        'policy_url': 'https://www.genenames.org/about/license/', 'export_allowed': True,
        'scope': 'Identifier cross references only; no regulatory or dietary causality'})))
    for value in selected.values():
        row = value['reference']
        target = ('NCBIGene:' + row['entrez_id']) if row.get('entrez_id', '').isdigit() else row['hgnc_id']
        for mapping in value['mappings']:
            mapping.update(source_id=source_id, source_version=digest)
        dst.execute('INSERT INTO node VALUES(?,?,?,?)', (target, 'gene', row['symbol'],
            canonical({'hgnc_id': row['hgnc_id'], 'hgnc_symbol': row['symbol'], 'entrez_id': row['entrez_id'],
                       'ensembl_gene_id': row.get('ensembl_gene_id'),
                       'verified_feature_ids': sorted(value['features']), 'source_ids': [source_id],
                       'feature_identifier_mappings': value['mappings'],
                       'identifier_aliases': [i for i in (row['hgnc_id'], row.get('ensembl_gene_id'), target) if i],
                       'gene_identity_basis': 'exact approved HGNC symbol or Ensembl gene ID',
                       'gene_identity_verified': True})))
    return {'mapped_gene_nodes': len(selected), 'unresolved_native_feature_labels': unknown,
            'hgnc_sha256': digest, 'unresolved_policy': 'No alias guessing or invented identity'}


def add_kfind(src, dst, foods, components):
    """Local research projection; unresolved redistribution permission stays false."""
    resource = src.execute("SELECT * FROM resource WHERE id='R02'").fetchone()
    if resource is None:
        return 0
    releases = json.loads(resource['metadata_json'])['kfind_dataset_releases']
    sources = {}
    for dataset, record in releases.items():
        digest = hashlib.sha256(canonical(record['sha256']).encode()).hexdigest()
        identifier = 'K-FIND-source:' + dataset + ':' + digest[:16]
        sources[record['release']] = identifier
        dst.execute('INSERT INTO source VALUES(?,?)', (identifier, canonical({
            'source': 'MFDS / DATA.GO.KR food composition', 'release': record['release'],
            'source_url': record['source_page'], 'sha256': digest, 'raw_file_hashes': record['sha256'],
            'export_allowed': False, 'license': 'redistribution-unconfirmed',
            'restriction_reason': 'Attribution retained for local research; bulk redistribution permission not confirmed'})))
    for row in src.execute("SELECT * FROM food WHERE source_id='R02' ORDER BY id"):
        info = json.loads(row['metadata_json'] or '{}')
        if not row['source_code'].startswith('K-FIND:') or not info.get('provider_food_code'):
            raise ValueError('Missing exact K-FIND food identity')
        foods[row['id']] = row['source_code']
        info.update(core_food_id=row['id'], core_metadata=json.loads(row['metadata_json'] or '{}'),
                    food_code=info['provider_food_code'], source_name=info['provider_food_name'],
                    core_source_id='R02', original_source_code=row['source_code'],
                    edible_state=row['edible_state'], preparation_state=row['preparation_state'])
        dst.execute('INSERT INTO node VALUES(?,?,?,?)', (row['source_code'], 'food', row['name'], canonical(info)))
    count = 0
    for row in src.execute("SELECT * FROM composition_measurement WHERE source_id='R02' ORDER BY id"):
        source, food, component = sources[row['release']], foods[row['food_id']], components[row['component_id']]
        identifier = hashlib.sha256(canonical([source, row['source_ref'], food, component]).encode()).hexdigest()
        metadata = json.loads(row['metadata_json'] or '{}')
        metadata.update(source_measurement_id=row['id'], source_ref=row['source_ref'], release=row['release'])
        dst.execute('INSERT INTO measurement VALUES(?,?,?,?,?,?,?,?,?)', (identifier, food, component,
            row['amount_per_100g'], row['unit'], row['basis'], row['status'], source, canonical(metadata)))
        dst.execute('INSERT INTO edge VALUES(?,?,?,?,?,?,?)', (identifier, food, component,
            'contains_nutrient', 'observed_composition', None, canonical({'measurement_id': identifier, 'source_status': row['status']})))
        dst.execute('INSERT INTO edge_source VALUES(?,?)', (identifier, source))
        count += 1
    return count


def build(core, output, hgnc=None):
    core, output = Path(core).resolve(), Path(output).resolve()
    if output.exists() or output.with_suffix('.building').exists():
        raise FileExistsError('An existing graph is immutable')
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix('.building')
    source_hash = sha256(core)
    with read_only(core) as src, closing(sqlite3.connect(temp)) as dst:
        dst.executescript(SCHEMA)
        dst.execute('PRAGMA cache_size=-262144')
        resource = src.execute("SELECT * FROM resource WHERE id='R03'").fetchone()
        if not resource:
            raise ValueError('Verified FDC resource is missing')
        archives = json.loads(resource['metadata_json'])['local_archives']
        sources = {}
        for release, entry in archives.items():
            identifier = 'USDA-FDC:' + hashlib.sha256(release.encode()).hexdigest()[:16]
            sources[release] = identifier
            dst.execute('INSERT INTO source VALUES(?,?)', (identifier, canonical({
                'source': 'USDA FoodData Central', 'release': release, 'sha256': entry['sha256'],
                'source_url': entry['url'], 'license': 'USDA-public-domain',
                'export_allowed': True, 'policy_url': 'https://fdc.nal.usda.gov/faq/',
                'rights_scope': 'USDA public composition records; no third-party scientific effect claim'})))
        foods, components = {}, {}
        for row in src.execute("SELECT * FROM food WHERE source_id='R03' ORDER BY id"):
            info = json.loads(row['metadata_json'] or '{}')
            fdc_id = info.get('fdc_id')
            if not fdc_id or row['source_code'] != 'FDC:' + str(fdc_id):
                raise ValueError('FDC source identifier mismatch')
            identifier = 'USDA-FDC:' + str(fdc_id)
            foods[row['id']] = identifier
            info.update(core_food_id=row['id'], core_metadata=json.loads(row['metadata_json'] or '{}'),
                        core_source_id='R03', original_source_code=row['source_code'],
                        edible_state=row['edible_state'], preparation_state=row['preparation_state'],
                        composition_mode=row['composition_mode'])
            dst.execute('INSERT INTO node VALUES(?,?,?,?)', (identifier, 'food', row['name'], canonical(info)))
        for row in src.execute("SELECT * FROM component WHERE component_key LIKE 'FDC-NUTRIENT:%' ORDER BY id"):
            identifier = row['component_key'].replace('FDC-NUTRIENT:', 'FDCNutrient:')
            components[row['id']] = identifier
            dst.execute('INSERT INTO node VALUES(?,?,?,?)', (identifier, 'nutrient', row['name'], canonical({
                'component_key': row['component_key'], 'canonical_unit': row['canonical_unit'],
                'mixture_or_nutrient_group': True, 'single_molecule_identity_verified': False})))
        gene_report = add_observed_genes(src, dst, hgnc) if hgnc else {'status': 'not_requested'}
        count = 0
        for row in src.execute("SELECT * FROM composition_measurement WHERE source_id='R03' ORDER BY id"):
            food, component = foods[row['food_id']], components[row['component_id']]
            source_id = sources[row['release']]
            identifier = hashlib.sha256(canonical([source_id, row['source_ref'], food, component]).encode()).hexdigest()
            metadata = json.loads(row['metadata_json'] or '{}')
            metadata.update(source_measurement_id=row['id'], source_ref=row['source_ref'], release=row['release'])
            dst.execute('INSERT INTO measurement VALUES(?,?,?,?,?,?,?,?,?)', (identifier, food, component,
                row['amount_per_100g'], row['unit'], row['basis'], row['status'], source_id, canonical(metadata)))
            dst.execute('INSERT INTO edge VALUES(?,?,?,?,?,?,?)', (identifier, food, component, 'contains_nutrient',
                'observed_composition', None, canonical({'measurement_id': identifier, 'source_status': row['status'],
                'interpretation': 'Composition reference; not a measured biological or health effect'})))
            dst.execute('INSERT INTO edge_source VALUES(?,?)', (identifier, source_id))
            count += 1
        kfind_count = add_kfind(src, dst, foods, components)
        count += kfind_count
        dst.commit()
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Graph integrity failed')
    verify_closed_sqlite(temp)
    if sha256(core) != source_hash:
        raise ValueError('Core changed during graph construction')
    temp.rename(output)
    report = {'core_sha256': source_hash, 'graph_sha256': sha256(output), 'foods': len(foods),
              'components': len(components), 'measurements': count, 'former_server_dependency': False,
              'local_kfind_measurements': kfind_count,
              'gene_identifiers': gene_report}
    atomic_json(output.with_suffix('.json'), report)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--core', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--hgnc', type=Path)
    args = p.parse_args()
    print(json.dumps(build(args.core, args.output, args.hgnc)))
