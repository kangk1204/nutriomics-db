import gzip
import hashlib
import json
import sqlite3
from collections import Counter

import pytest
from nutrigenomics.candidate_layers import graphreg_rows, integrate, graphreg_rights, validate_projection,add_exact_gene_bridges
from nutrigenomics.explorer_build import SCHEMA
from scripts.reacquire_candidate_layers import parse_pubmed

def test_graphreg_exact_rows_method_dedup_and_native_numeric(tmp_path):
    row='EH38E1\tENSG00000000001\tGENE\tprotein_coding\tGraphRegLR\tchr1_2_3\tENCFF431TFM\tK562\t1\n'
    p=tmp_path/'test.gz'
    with gzip.open(p,'wt') as h:h.write(row+row+row.replace('GraphRegLR','other'))
    digest=hashlib.sha256(p.read_bytes()).hexdigest();counts=Counter()
    rows=list(graphreg_rows(p,digest,counts))
    assert counts=={'source_rows':3,'admitted':1,'exact_duplicate_rows':1,'excluded_other_method':1}
    assert rows[0]['fields'][-1]=='1'
    assert rows[0]['object']=='Ensembl:ENSG00000000001'
    with pytest.raises(ValueError,match='hash'):list(graphreg_rows(p,'0'*64))

def test_graphreg_nonfinite_and_schema_fail_closed(tmp_path):
    for line in ('bad\tshape','EH38E1\tENSG00000000001\tGENE\tx\tGraphRegLR\tchr1_2_3\tENCFF431TFM\tK562\tnan'):
        p=tmp_path/'test.gz'
        with gzip.open(p,'wt') as h:h.write(line)
        with pytest.raises(ValueError):list(graphreg_rows(p,hashlib.sha256(p.read_bytes()).hexdigest()))

def test_pubmed_sections_native_text_offsets_and_retraction():
    raw=b'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
        <ArticleTitle>A <i>gene</i> title.</ArticleTitle><Abstract><AbstractText NlmCategory="RESULTS">Omega-3 decreased TG.</AbstractText></Abstract>
        <PublicationTypeList><PublicationType>Retracted Publication</PublicationType></PublicationTypeList>
        </Article></MedlineCitation></PubmedArticle></PubmedArticleSet>'''
    row=parse_pubmed(raw)[0]
    assert row['retracted'] is True
    assert row['text'][row['sections'][1]['start']:row['sections'][1]['end']]=='Omega-3 decreased TG.'
    assert row['text'].startswith('A gene title.')

def test_rights_requires_actual_policy_and_native_file_metadata(tmp_path):
    (tmp_path/'rights').mkdir()
    (tmp_path/'rights/manifest.json').write_text('[]')
    with pytest.raises((KeyError,ValueError)):graphreg_rights(tmp_path)

def test_prediction_and_text_never_become_human_intervention():
    from nutrigenomics.explorer_scientific import edge_scientific
    for tier,classification in [('predicted','computational_prediction'),('extracted','automated_candidate')]:
        result=edge_scientific({'tier':tier,'relation':'increases','taxon':None,'context':{}})
        assert result['study_evidence']['classification']==classification
        assert result['study_evidence']['component_causal_effect'] is False

def test_exact_hgnc_bridge_joins_ids_without_merging_homonyms(tmp_path):
    from nutrigenomics.workbench_build import WORKBENCH_SCHEMA
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;db.executescript(SCHEMA+WORKBENCH_SCHEMA)
    for identifier in ('Ensembl:ENSG00000000001','NCBIGene:1','NCBIGene:2','Literature:HGNC:1'):
        db.execute('INSERT INTO node VALUES(?,?,?,?,?,?,?,0)',(identifier,'gene','SAME','SAME','[]','{}','[]'))
    hgnc=tmp_path/'hgnc.tsv'
    hgnc.write_text('status\thgnc_id\tensembl_gene_id\tentrez_id\nApproved\tHGNC:1\tENSG00000000001\t1\nApproved\tHGNC:2\tENSG00000000002\t2\n')
    result=add_exact_gene_bridges(db,hgnc)
    assert result['added_exact_gene_identity_links']==2
    assert db.execute('SELECT count(*) FROM node').fetchone()[0]==4
    assert not db.execute("SELECT 1 FROM edge WHERE object='NCBIGene:2'").fetchone()
    assert add_exact_gene_bridges(db,hgnc)['added_exact_gene_identity_links']==0

@pytest.mark.parametrize('public',[False,True])
def test_projection_keeps_prediction_candidate_and_public_rights_separate(tmp_path,public,monkeypatch):
    def write(name,value):
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(value),encoding='utf-8');return p
    raw=tmp_path/'source.gz'
    with gzip.open(raw,'wt') as h:
        h.write('EH38E1\tENSG00000000001\tGENE\tx\tGraphRegLR\tchr1_2_3\tENCFF431TFM\tK562\t1\n')
    digest=hashlib.sha256(raw.read_bytes()).hexdigest()
    write('graphreg-source.json',{'object_path':str(raw),'sha256':digest,'url':'https://downloads.wenglab.org/native','retrieved_at':'2026-10-17'})
    receipts=[]
    for name in ('encode-policy','screen-about','screen-downloads','ENCFF431TFM','ENCFF140XYG'):
        ext='.json' if name.startswith('ENCFF') else '.html'
        p=tmp_path/'rights'/(name+ext);p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps({'status':'released','assembly':'GRCh38','submitted_file_name':'GraphRegLR'})
                     if ext=='.json' else 'unrestricted use immediately upon release',encoding='utf-8')
        receipts.append({'name':name,'status':200,'url':'https://www.encodeproject.org/'+name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    write('rights/manifest.json',receipts)
    hgnc=tmp_path/'hgnc.tsv';hgnc.write_text('status\tensembl_gene_id\thgnc_id\nApproved\tENSG00000000001\tHGNC:1\n')
    quote='DHA decreased GENE expression.'
    assertion={'subject':'local:DHA','object':'HGNC:1','predicate':'decreases','quote':quote,'start':0,'end':len(quote),
        'payload':{'subject_mention':{'kind':'compound','text':'DHA'},'object_mention':{'kind':'gene','text':'GENE'},'organism':'animal','polarity':'positive'}}
    write('literature-candidates.json',{'extractor':'test-v1','documents':[{'pmid':'123','source_snapshot_sha256':'a'*64,
        'source_url':'https://pubmed.ncbi.nlm.nih.gov/123/','publication_types':['Journal Article'],'retracted':False,
        'text':quote,'extraction':{'assertions':[assertion],'dictionary_version':'test-dictionary'}}]})
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    db.execute("INSERT INTO node VALUES('HGNC:1','gene','GENE','GENE','[]','{}','[]',0)")
    summary=integrate(db,tmp_path,hgnc,public)
    audit=validate_projection(db,tmp_path,summary,public)
    assert audit['mismatches']==0 and audit['expert_approved']==0
    row=db.execute("SELECT tier,review_status,context_json FROM edge WHERE id LIKE 'GRL:%'").fetchone()
    assert row['tier']=='predicted' and row['review_status']=='candidate'
    assert json.loads(row['context_json'])['experimental_validation'] is False
    if public:
        assert db.execute("SELECT count(*) FROM edge WHERE tier='extracted'").fetchone()[0]==0
        assert 'DHA decreased' not in db.execute('SELECT metadata_json FROM candidate_document').fetchone()[0]
    else:
        row=db.execute("SELECT taxon,context_json FROM edge WHERE tier='extracted'").fetchone()
        assert row['taxon'] is None
        assert json.loads(row['context_json'])['payload']['organism']=='animal'
        assert json.loads(row['context_json'])['organism'] is None
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from nutrigenomics.explorer import attach_explorer
    db.execute("INSERT INTO meta VALUES('release_id','\"candidate-test\"')");db.commit()
    disk=tmp_path/'explorer.sqlite'
    with sqlite3.connect(disk) as target:db.backup(target)
    monkeypatch.setenv('NUTRIGENOMICS_EXPLORER_DATABASE',str(disk))
    app=FastAPI();attach_explorer(app)
    with TestClient(app) as client:
        result=client.get('/api/v1/explorer/evidence',params={'layer':'literature_candidate'}).json()
        assert result['status']==('withheld' if public else 'indexed')
        assert result['withheld_candidate_count']==(1 if public else 0)
        if public:
            assert result['withheld_count_scope']=='release_wide_not_seed_relevance'
            assert result['items']==[]
        registry=client.get('/api/v1/explorer/candidate-documents').json()
        assert registry['total']==1 and registry['expert_approved']==0
        assert 'DHA decreased' not in json.dumps(registry)
