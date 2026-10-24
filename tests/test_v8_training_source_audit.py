import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/audit_v8_training_sources.py'
spec=importlib.util.spec_from_file_location('audit_v8_training_sources',SCRIPT)
audit=importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def put(root,name,value):
    p=root/name;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(value,ensure_ascii=False),encoding='utf8')
    return hashlib.sha256(p.read_bytes()).hexdigest()


def source(name,kind,path,**values):
    return {'name':name,'dataset':name,'split':'train','kind':kind,'path':path,**values}


def test_merges_protection_train_native_and_manifest_without_using_labels(tmp_path):
    put(tmp_path,'prior.json',{'labels_read':False,'pmids':['1','2'],'pmcids':['PMC1'],
                             'text_hashes':[audit.normalized_text_hash('old abstract')]})
    put(tmp_path,'train.json',{'split':'train','test_labels_read':False,'documents':[
        {'pmid':'2','pmcid':'PMC2','text':'β FULL  ABSTRACT','entities':{'private_label':'not exported'}},
        {'pmid':'3','text':'β full abstract','relations':['private-gold']}]})
    put(tmp_path,'native.json',{'heldout_labels_read':False,'rows':[
        {'pmid':'4','task':'cg','text':'native source','label':'NEVER_EXPORT'},
        {'pmid':'5','task':'drugprot','text':'other task','label':'IGNORE'}]})
    put(tmp_path,'clinical.json',{'training_split':'train','test_used_for_selection':False,
                                'training_pmids':{'rct':['6','7'],'ebm':['8']}})
    cfg={'source_root':str(tmp_path),'sources':[
        source('prior','protection','prior.json',split='protected'),
        source('NLM-Gene','training_documents','train.json'),
        source('CG','native_rows','native.json',task='cg'),
        source('PubMed-RCT','training_manifest','clinical.json',training_key='rct')]}
    out=audit.build_exclusions(cfg)
    assert out['pmids']==['1','2','3','4','6','7']
    assert out['pmcids']==['PMC1','PMC2']
    assert len(out['normalized_text_hashes'])==3
    assert out['not_a_supervised_training_allowlist'] and not out['all_public_gold_excluded']
    assert out['source_only'] is True
    assert out['status']=='waiting' and out['sealed_annotation_files_opened'] is False
    serialized=json.dumps(out)
    assert 'private-gold' not in serialized and 'NEVER_EXPORT' not in serialized
    assert str(tmp_path) not in serialized and all(not r['labels_used_for_exclusion'] for r in out['public_gold_sources'])
    assert next(r for r in out['public_gold_sources'] if r['name']=='PubMed-RCT')['rows_missing_text']==2


def test_export_is_actual_corpus_protection_compatible_without_annotation_payload(tmp_path):
    """Exercise the consumer contract rather than a second copy of its checks."""
    pytest.importorskip('defusedxml',reason='Requires the literature_service runtime dependencies')
    corpus_path=SCRIPT.parents[1]/'literature_service/nutri_literature/corpus_v8.py'
    corpus_spec=importlib.util.spec_from_file_location('corpus_v8_audit_compat',corpus_path)
    corpus=importlib.util.module_from_spec(corpus_spec)
    corpus_spec.loader.exec_module(corpus)
    put(tmp_path,'train.json',{'split':'train','documents':[
        {'pmid':'123','pmcid':'PMC123','text':'source text',
         'entities':[{'label':'SECRET_ENTITY'}],
         'relations':[{'type':'SECRET_RELATION'}],
         'comparisons':[{'effect':'SECRET_EFFECT'}]}]})
    put(tmp_path,'prior.json',{'pmids':['PMC456','journal-source-id']})
    output=audit.build_exclusions({'source_root':str(tmp_path),'sources':[
        source('NLM-Gene','training_documents','train.json'),
        source('prior','protection','prior.json')]})
    path=tmp_path/'exclusion.json'
    path.write_text(audit.canonical(output),encoding='utf8')
    protection=corpus._protection(path)
    assert protection['pmids']=={'123'} and protection['pmcids']=={'PMC123','PMC456'}
    assert protection['hashes']=={audit.normalized_text_hash('source text')}
    assert protection['payload']['source_only'] is True
    assert protection['ready'] is False and output['all_public_gold_excluded'] is False
    # Neither actual annotations nor raw source text is copied into this artifact.
    serialized=path.read_text(encoding='utf8')
    assert all(value not in serialized for value in ('SECRET_ENTITY','SECRET_RELATION','SECRET_EFFECT','source text'))
    forbidden={'annotations','entities','relations','labels','predictions','gold','comparisons','targets'}
    def verify_metadata_only(value):
        if isinstance(value,dict):
            assert not forbidden.intersection(value)
            for item in value.values():verify_metadata_only(item)
        elif isinstance(value,list):
            for item in value:verify_metadata_only(item)
    verify_metadata_only(output)


def test_all_required_public_inventories_need_complete_pinned_metadata(tmp_path):
    cfg={'source_root':str(tmp_path),'sources':[]}
    for i,dataset in enumerate(audit.REQUIRED):
        path=f'{i}.json';sha=put(tmp_path,path,{'split':'all','coverage_complete':True,'expected_documents':1,'pmids':[str(i+1)],'pmcids':[]})
        cfg['sources'].append(source(dataset,'id_manifest',path,split='all',sha256=sha))
    out=audit.build_exclusions(cfg)
    assert out['all_public_gold_excluded'] and out['status']=='completed' and not out['missing_sources']
    assert out['pmcid_coverage']=='incomplete_or_unestablished'
    cfg['sources'][0].pop('sha256')
    incomplete=audit.build_exclusions(cfg)
    assert not incomplete['all_public_gold_excluded']
    assert any(r['dataset']==audit.REQUIRED[0] for r in incomplete['missing_sources'])


def test_missing_or_partial_public_gold_inventory_never_permits_dapt(tmp_path):
    put(tmp_path,'train.json',{'split':'train','documents':[{'pmid':'1','text':'known'}]})
    out=audit.build_exclusions({'source_root':str(tmp_path),'sources':[
        source('NLM-Gene','training_documents','train.json'),
        source('BC5CDR','source_only_documents','missing-inputs.json',split='test')]})
    assert out['counts']['pmids']==1 and out['status']=='waiting'
    assert {r['dataset'] for r in out['missing_sources']}==set(audit.REQUIRED)
    assert any(r['reason']=='explicit_source_file_missing' for r in out['missing_sources'])


def test_explicit_source_only_rejects_annotation_fields(tmp_path):
    put(tmp_path,'inputs.json',{'documents':[{'pmid':'1','text':'source','entities':[]}]})
    with pytest.raises(ValueError,match='annotation fields'):
        audit.build_exclusions({'source_root':str(tmp_path),'sources':[
            source('BioRED','source_only_documents','inputs.json',split='test')]})


@pytest.mark.parametrize('name',['gold.json','sealed/inputs.json','test-gold.json'])
def test_sealed_annotation_paths_are_refused_before_opening(tmp_path,name,monkeypatch):
    put(tmp_path,name,{'documents':[]})
    original=Path.read_bytes
    def forbid(p):
        if p==tmp_path/name:raise AssertionError('sealed annotation file was read')
        return original(p)
    monkeypatch.setattr(Path,'read_bytes',forbid)
    with pytest.raises(ValueError,match='Sealed/gold'):
        audit.build_exclusions({'source_root':str(tmp_path),'sources':[
            source('BioRED','source_only_documents',name,split='test')]})


@pytest.mark.parametrize('document',[{'split':'test','documents':[]},
                                   {'split':'train','test_labels_read':True,'documents':[]}])
def test_annotated_nontrain_input_is_not_imported(tmp_path,document):
    put(tmp_path,'data.json',document)
    with pytest.raises(ValueError,match='allowed train'):
        audit.build_exclusions({'source_root':str(tmp_path),'sources':[
            source('NLM-Gene','training_documents','data.json')]})


def test_declared_source_hash_is_checked(tmp_path):
    put(tmp_path,'prior.json',{'pmids':['1']})
    with pytest.raises(ValueError,match='SHA-256 differs'):
        audit.build_exclusions({'source_root':str(tmp_path),'sources':[
            source('prior','protection','prior.json',sha256='0'*64)]})


def test_normalized_text_matches_v8_nfkc_casefold_and_whitespace():
    assert audit.normalized_text_hash('Ａ  β\nStraße')==audit.normalized_text_hash('a β STRASSE')


def test_complete_inventory_count_mismatch_does_not_qualify(tmp_path):
    sha=put(tmp_path,'meta.json',{'split':'all','coverage_complete':True,'expected_documents':100,'pmids':['1']})
    out=audit.build_exclusions({'source_root':str(tmp_path),'sources':[
        source('NLM-Gene','id_manifest','meta.json',split='all',sha256=sha)]})
    assert not out['all_public_gold_excluded']
    assert not out['public_gold_sources'][0]['all_release_coverage_verified']


def test_legacy_nonpubmed_document_ids_are_not_reported_as_numeric_pmids(tmp_path):
    put(tmp_path,'meta.json',{'pmids':['123','PMC100','1471-2105-8-249']})
    out=audit.build_exclusions({'source_root':str(tmp_path),'sources':[source('prior','protection','meta.json')]})
    assert out['pmids']==['123'] and len(out['source_document_ids'])==3
    assert out['legacy_source_document_ids']==['1471-2105-8-249','PMC100']
    assert out['pmcids']==['PMC100']
    assert out['counts']['pmids']==1 and out['counts']['source_document_ids']==3
    assert out['identifier_notes']['numeric_pubmed_ids']==1
    assert out['identifier_notes']['non_numeric_source_document_ids']==2


def test_dapt_also_excludes_supervised_examples_previously_removed_from_training(tmp_path):
    put(tmp_path,'clinical.json',{'training_split':'train','test_used_for_selection':False,
        'training_pmids':{'ebm':['1','2']},'excluded_pmids':{'ebm':['3']}})
    out=audit.build_exclusions({'source_root':str(tmp_path),'sources':[
        source('EBM-NLP','training_manifest','clinical.json',training_key='ebm')]})
    assert out['pmids']==['1','2','3']
    assert out['public_gold_sources'][0]['supervised_train_manifest_counts']=={
        'allowed_train_identifiers':2,'excluded_train_identifiers':1}


def test_cli_needs_explicit_output_and_does_not_overwrite(tmp_path,capsys):
    with pytest.raises(SystemExit):audit.main([])
    p=tmp_path/'source.json';put(tmp_path,p.name,{'pmids':['1']})
    config=tmp_path/'config.json';config.write_text(json.dumps({'sources':[
        source('prior','protection',p.name)]}),encoding='utf8')
    out=tmp_path/'new-exclusion.json'
    assert audit.main(['--config',str(config),'--output',str(out)])==0
    value=json.loads(out.read_text());assert value['pmids']==['1']
    before=out.read_bytes()
    with pytest.raises(FileExistsError):audit.main(['--config',str(config),'--output',str(out)])
    assert out.read_bytes()==before
    with pytest.raises(ValueError,match='replace an audit input'):
        audit.main(['--config',str(config),'--output',str(p)])
    assert json.loads(p.read_text())=={'pmids':['1']}
