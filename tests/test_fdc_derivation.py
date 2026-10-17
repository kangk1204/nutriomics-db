import hashlib
import json
import sqlite3
import zipfile

from scripts.build_fresh_food_graph import SCHEMA, add_observed_genes, build
from scripts.import_fdc_json import DATASETS, import_fdc


def test_fresh_import_and_graph_preserve_analytical_vs_reference_values(tmp_path):
    folder = tmp_path / 'data/raw/fdc'
    folder.mkdir(parents=True)
    records = []
    for k, dataset in enumerate(DATASETS):
        path = folder / dataset['name']
        food = {'fdcId': 10 + k, 'description': 'source food',
                'dataType': 'Foundation' if k == 0 else 'Survey (FNDDS)',
                'foodNutrients': [{'nutrient': {'id': 1003, 'name': 'Protein', 'unitName': 'g'},
                                  'amount': 0 if k == 0 else 3.3,
                                  'foodNutrientDerivation': {'code': 'A', 'description': 'Analytical'}}]}
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('food.json', json.dumps({dataset['root_keys'][0]: [food]}))
        records.append({**dataset, 'url': 'https://fdc.nal.usda.gov/fdc-datasets/' + path.name,
                        'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    (folder / 'download_manifest.json').write_text(json.dumps({'files': records}))
    db = tmp_path / 'core.sqlite3'
    import_fdc(tmp_path / 'data', 'sqlite:///' + db.as_posix())
    with sqlite3.connect(db) as connection:
        rows = connection.execute('SELECT status,metadata_json FROM composition_measurement ORDER BY id').fetchall()
    assert [r[0] for r in rows] == ['measured_zero', 'derived']
    assert all(json.loads(r[1])['foodNutrientDerivation']['code'] == 'A' for r in rows)
    graph = tmp_path / 'graph.sqlite3'
    result = build(db, graph)
    assert result['foods'] == 2 and result['measurements'] == 2
    with sqlite3.connect(graph) as connection:
        assert dict(connection.execute('SELECT status,count(*) FROM measurement GROUP BY status')) == {'derived': 1, 'measured_zero': 1}


def test_gene_identity_uses_exact_approved_ids_without_cpg_regulation_claim(tmp_path):
    hgnc = tmp_path / 'hgnc.tsv'
    hgnc.write_text('hgnc_id\tsymbol\tstatus\tentrez_id\tensembl_gene_id\n'
                    'HGNC:613\tAPOE\tApproved\t348\tENSG00000130203\n')
    src, dst = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    src.execute('CREATE TABLE study_effect(feature_id,feature_label,feature_type,series_accession)')
    src.executemany('INSERT INTO study_effect VALUES(?,?,?,?)', [
        ('APOE', 'APOE', 'gene', 'GSE1'), ('ENSG00000130203', 'APOE', 'gene', 'GSE1'),
        ('APO-E', 'APOE', 'gene', 'GSE1'), ('cg000001', 'APOE', 'cpg', 'GSE1')])
    dst.executescript(SCHEMA)
    report = add_observed_genes(src, dst, hgnc)
    assert report['mapped_gene_nodes'] == 1 and report['unresolved_native_feature_labels'] == 1
    identifier, metadata = dst.execute('SELECT id,metadata_json FROM node').fetchone()
    assert identifier == 'NCBIGene:348'
    assert json.loads(metadata)['verified_feature_ids'] == ['APOE', 'ENSG00000130203']
    from nutrigenomics.explorer_scientific import feature_match_identity, gene_feature_identifiers
    node = {'id': identifier, 'label': 'APOE', 'metadata': json.loads(metadata)}
    assert '613' not in gene_feature_identifiers({'id': 'HGNC:613', 'label': 'APOE', 'metadata': {}})
    assert '348' in gene_feature_identifiers(node)
    assert 'ENSG00000130203' in gene_feature_identifiers(node)
    assert feature_match_identity(node, {'series_accession': 'GSE1', 'feature_type': 'gene',
        'feature_id': 'ENSG00000130203'})['status'] == 'source_identifier_match'
    assert dst.execute('SELECT count(*) FROM edge').fetchone()[0] == 0
    src.close()
    dst.close()
