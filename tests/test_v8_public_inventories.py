import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('build_v8_public_inventories',SCRIPTS/'build_v8_public_inventories.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


def test_pubtator_reads_only_bibliographic_headers_and_never_decodes_labels():
    data=b'123|t|Title\n123|a|Abstract\n123\t1\t2\t\xff\tGene\n123\t999\t\xfe\tRELATION\n\n'
    assert builder.pubtator_headers(io.BytesIO(data))==[{'pmid':'123','text':'Title\nAbstract'}]
    with pytest.raises(ValueError,match='Duplicate'):builder.pubtator_headers(io.BytesIO(b'1|t|T\n1|t|T2\n'))


def test_rct_reader_discards_role_and_sentence_annotation_columns():
    assert builder.rct_headers(io.BytesIO(b'###123\nRESULTS\t\xff\n\n###456\nMETHODS\ttext\n'))==[
        {'pmid':'123'},{'pmid':'456'}]


def test_bioc_header_handler_ignores_annotation_subtrees():
    raw=b'''<collection><document><id>999</id><passage><infon key="article-id_pmid">123</infon>
    <text>Original source.</text><annotation id="A1"><infon key="article-id_pmid">WRONG</infon>
    <text>DO NOT RETAIN ANNOTATION</text></annotation></passage></document></collection>'''
    result=builder.bioc_source_headers(raw)
    assert result==[{'id':'999','pmid':'123','passages':['Original source.']}]
    assert 'ANNOTATION' not in str(result)


def test_plain_archive_reader_opens_only_source_text_members():
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w:gz') as archive:
        for name,value in [('consensus/123.txt',b'Source'),('consensus/123.ann',b'private labels'),('other/456.txt',b'not selected')]:
            info=tarfile.TarInfo(name);info.size=len(value);archive.addfile(info,io.BytesIO(value))
    assert builder.plain_tar(out.getvalue(),'consensus/',1)==[{'pmid':'123','text':'Source'}]
    with pytest.raises(ValueError,match='count differs'):builder.plain_tar(out.getvalue(),'consensus/',2)


def test_inventory_records_source_counts_without_changing_old_inventory(tmp_path):
    result=builder.write_inventory('example',[{'pmid':'1','text':'Original'}],{'train':1},
                                   [{'url':'https://official.example/source','sha256':'a'*64}],output=tmp_path)
    assert result['coverage_complete'] and result['documents']==1
    payload=json.loads(Path(result['path']).read_text(encoding='utf8'))
    assert payload['coverage_complete'] and payload['expected_documents']==1
    assert payload['annotation_members_extracted'] is False and payload['labels_read'] is False
    assert payload['split_counts']=={'train':1}
    with pytest.raises(FileExistsError):builder.write_inventory('example',[{'pmid':'2'}],{'test':1},[],output=tmp_path)


def test_medtag_reader_decodes_bibliographic_excerpts_but_not_annotation_records():
    raw=b"insert into annotation values (\xff);\ninsert into excerpt (corpus_cd, excerpt_id, source, subcorpus_cd, text)\nvalues ('GENETAG','S1','PMID: 000123 SO: Journal','train','Source');\n"
    assert builder.medtag_source_excerpts(io.BytesIO(raw))==[{'pmid':'123'}]


def test_only_explicit_official_hosts_are_accepted():
    with pytest.raises(ValueError,match='official public'):builder.fetch('https://untrusted.invalid/file.zip')


def test_pmc_mapping_uses_current_official_bibliographic_api_only(monkeypatch):
    import requests
    calls=[]
    class Response:
        status_code=200
        content=b'bibliographic-response'
        def raise_for_status(self):pass
        def json(self):return {'status':'ok','records':[
            {'requested-id':'PMC123','pmcid':'PMC123','pmid':456},
            {'requested-id':'PMC333','pmcid':'PMC333'},
            {'requested-id':'PMC999','errmsg':'Identifier not found'}]}
    def get(url,params,timeout):
        calls.append((url,params));return Response()
    monkeypatch.setattr(requests,'get',get)
    monkeypatch.setattr(builder.time,'sleep',lambda _:None)
    mapping,receipts=builder.official_pmc_map({'123','333','999'})
    assert mapping=={'123':'456'}
    assert calls==[('https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/',{
        'ids':'PMC123,PMC333,PMC999','idtype':'pmcid','format':'json','tool':'NutriOmicsSourceAudit'})]
    assert receipts[0]['labels_read'] is False
    assert receipts[0]['confirmed_no_pmid_pmcids']==['PMC333']
    assert receipts[0]['unresolved_pmcids']==['PMC999']
    with pytest.raises(ValueError,match='numeric identifiers'):builder.official_pmc_map({'bad-id'})
