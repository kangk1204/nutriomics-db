"""Independent naming fixtures: identity, ambiguity and measured intervention links."""
import csv
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from test_explorer_api import explorer as explorer
from test_explorer_scientific_api import _science
from nutrigenomics import gene_names as names


@pytest.fixture
def named(explorer, tmp_path, monkeypatch):
    _science(explorer)
    raw = tmp_path / 'hgnc.tsv'
    rows = [
        dict(hgnc_id='HGNC:1', symbol='ENTPD1', name='ectonucleoside triphosphate diphosphohydrolase 1', status='Approved', entrez_id='41', ensembl_gene_id='ENSG_FIXTURE', alias_symbol='CD39|SHARED', prev_symbol='OLD39'),
        dict(hgnc_id='HGNC:2', symbol='OTHER', name='other gene', status='Approved', entrez_id='99', ensembl_gene_id='ENSG_OTHER', alias_symbol='SHARED', prev_symbol=''),
    ]
    with raw.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter='\t')
        writer.writeheader(); writer.writerows(rows)
    inventory = tmp_path / 'inventory.json'
    with sqlite3.connect(explorer['db']) as db:
        items = [dict(id=r[0], label=r[1], metadata=json.loads(r[2]), metadata_sha256=hashlib.sha256(names.canonical(json.loads(r[2])).encode()).hexdigest()) for r in db.execute("SELECT id,label,metadata_json FROM node WHERE kind='gene'")]
    inventory.write_text(json.dumps(dict(items=items, release_id='fixture-release-20261003', database_sha256=names.digest(explorer['db']))))
    korean = tmp_path / 'korean.json'
    korean.write_text(json.dumps(dict(items=[dict(hgnc_id='HGNC:1', symbol=rows[0]['symbol'], approved_name=rows[0]['name'], name_ko='뉴클레오타이드 분해 효소', aliases=['한글 별칭'])])))
    config = tmp_path / 'config'; config.mkdir()
    producer = tmp_path / 'source.R'; producer.write_text('# fixture producer')
    contracts = config / 'contracts.json'
    contracts.write_text(json.dumps(dict(contracts=[dict(series_accession='GSE_FIXTURE', analysis_version='v1', feature_type='gene_expression', namespace='HGNCsymbol', source_file='source.R', script_sha256=names.digest(producer))])))
    output = tmp_path / 'names.sqlite'
    names.build(raw, inventory, korean, contracts, output)
    monkeypatch.setenv(names.CONFIG, str(output))
    return dict(**explorer, catalog=output, raw=raw, inventory=inventory, korean=korean, contracts=contracts, producer=producer)


@pytest.mark.parametrize('query', ['ENTPD1', 'entpd1', 'CD39', 'OLD39', 'ectonucleoside triphosphate diphosphohydrolase 1', 'HGNC:1', 'ENSG_FIXTURE', '한글별칭', '뉴클레오타이드분해효소'])
def test_names_preserve_native_identity_and_private_metadata(named, query):
    result = named['client'].get('/api/v1/explorer/entities', params=dict(kind='gene', q=query))
    assert result.status_code == 200, result.text
    found = next(r for r in result.json()['items'] if r['id'] == 'NCBIGene:41')
    assert found['label'] == 'ENTPD1'
    assert found['gene_names']['hgnc_id'] == 'HGNC:1'
    assert found['gene_names']['expert_review_status'] == 'pending'
    assert '/home/research' not in result.text


def test_ambiguous_alias_does_not_merge_or_choose_identity(named):
    result = named['client'].get('/api/v1/explorer/entities', params=dict(kind='gene', q='SHARED', include_extracted=True)).json()
    assert result['gene_search_context']['ambiguous'] is True
    assert result['gene_search_context']['exact_hgnc_candidates'] == 2
    assert {r['id'] for r in result['items']} == {'NCBIGene:41', 'NCBIGene:99'}
    assert names.normalize('PPAR-γ') == names.normalize('ppar gamma')


def test_gene_to_intervention_uses_native_key_and_preserves_nonsignificant_values(named):
    response = named['client'].post('/api/v1/explorer/match', json={'seed_id': 'NCBIGene:41'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['reference_context']['intake_g'] is None
    assert result['gene_query']['eligible_contrasts'] == 1
    card, = result['observed_evidence']
    assert card['causal_inference'] is False
    assert card['mapping_identity']['status'] == 'measured_gene'
    contrast, = card['contrasts']
    effect, = contrast['effects']
    assert (effect['feature_type'], effect['feature_id'], effect['analysis_version']) == ('gene_expression', 'ENTPD1', 'v1')
    assert effect['effect_estimate'] == -.25 and effect['q_value'] == .72
    assert effect['feature_mapping']['status'] == 'source_identifier_match'
    assert not any(r['dimension'] == 'dose' for r in card['applicability'])
    other = named['client'].post('/api/v1/explorer/match', json={'seed_id': 'NCBIGene:99'}).json()
    assert other['gene_query']['status'] == 'available_no_results'
    assert other['observed_evidence'] == []


def test_source_corruption_is_not_reported_as_zero(named):
    named['catalog'].with_suffix('.manifest.json').write_text('{}')
    response = named['client'].get('/api/v1/explorer/entities', params=dict(kind='gene', q='CD39'))
    assert response.status_code == 503


def test_changed_native_binding_is_rejected(named):
    with sqlite3.connect(named['db']) as db:
        db.execute("UPDATE node SET label='changed' WHERE id='NCBIGene:41'")
    response = named['client'].get('/api/v1/explorer/entities', params=dict(kind='gene', q='CD39'))
    assert response.status_code == 503


def test_namespace_code_hash_must_match(named, tmp_path):
    named['producer'].write_text('# changed producer')
    with pytest.raises(ValueError, match='producer code mismatch'):
        names.build(named['raw'], named['inventory'], named['korean'], named['contracts'], tmp_path / 'second.sqlite')


def test_detail_does_not_leak_other_namespace_or_analysis_version(named):
    result = named['client'].get('/api/v1/explorer/nodes/NCBIGene:41')
    assert result.status_code == 200, result.text
    effects = result.json()['effects']
    assert len(effects) == 1
    assert effects[0]['analysis_version'] == 'v1'
    assert effects[0]['feature_mapping']['status'] == 'source_identifier_match'


def test_inventory_requires_closed_hash_bound_release(explorer, tmp_path):
    import shutil
    from scripts.export_gene_inventory import export
    release = tmp_path / 'release'; release.mkdir()
    database = release / 'explorer.sqlite'; shutil.copyfile(explorer['db'], database)
    (release / 'manifest.json').write_text(json.dumps(dict(release_id='fixture-release-20261003', database_sha256=names.digest(database))))
    output = tmp_path / 'exported-inventory.json'
    receipt = export(release, output)
    assert receipt['records'] == 2
    with pytest.raises(FileExistsError):
        export(release, output)
    Path(str(database) + '-wal').write_bytes(b'nonempty')
    with pytest.raises(ValueError, match='immutable snapshot'):
        export(release, tmp_path / 'denied.json')


def test_gene_snapshot_retains_search_names_without_unpermitted_stats(named):
    response = named['client'].post('/api/v1/explorer/export', json=dict(format='json',release_id='fixture-release-20261003',node_ids=['NCBIGene:41'],edge_ids=[],filters={'scientific_context': {'seed_id':'NCBIGene:41','reference_context':{}}}))
    assert response.status_code == 200, response.text
    result=response.json()
    assert result['nodes'][0]['gene_names']['approved_symbol']=='ENTPD1'
    assert result['nodes'][0]['gene_names']['name_ko']=='뉴클레오타이드 분해 효소'
    assert result['node_details']['NCBIGene:41']['effects']==[]
    assert '/home/research' not in response.text


def test_removed_configured_catalog_is_not_silent_missing_evidence(named):
    named['catalog'].unlink()
    response=named['client'].get('/api/v1/explorer/entities',params={'kind':'gene','q':'CD39'})
    assert response.status_code==503
