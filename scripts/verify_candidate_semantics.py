"""Verify all official ID links and live candidate labels/rights, not expert meaning."""
import argparse
import csv
import json
import urllib.request
from collections import defaultdict
from pathlib import Path
from nutrigenomics.explorer_build import read_only,decode,sha256
from nutrigenomics.source_updates import atomic_json

def verify(release,hgnc,base,output,public=False):
    release,hgnc=map(Path,(release,hgnc));records={};owners=defaultdict(set);hgnc_sha=sha256(hgnc)
    with hgnc.open(encoding='utf-8-sig') as handle:
        for row in csv.DictReader(handle,delimiter='\t'):
            if row['status']!='Approved':continue
            records[row['hgnc_id']]=row
            for key,value in [('HGNC',row['hgnc_id']),('Ensembl',row.get('ensembl_gene_id')),('NCBIGene',row.get('entrez_id'))]:
                if value:owners[value if key=='HGNC' else key+':'+value].add(row['hgnc_id'])
    errors=[];checked=0
    manifest=json.loads((release/'manifest.json').read_text(encoding='utf-8'))
    with read_only(release/('nutriomics.sqlite3' if public else 'explorer.sqlite')) as db:
        for row in db.execute("SELECT * FROM edge WHERE id>='HGNC-ID:' AND id<'HGNC-ID;'"):
            context=decode(row['context_json']);h=context['hgnc_id'];source=records.get(h)
            for identifier in (row['subject'],row['object']):
                if identifier.startswith('Literature:HGNC:'):
                    valid=identifier=='Literature:'+h
                else:valid=owners[identifier]=={h}
                if not valid:errors.append({'id':row['id'],'reason':'official_identifier_not_unique_or_mismatch'})
            if not source or context['source_sha256']!=hgnc_sha or row['tier']!='regulatory_identity' or row['taxon'] is not None or context['effect_transfer'] is not False:
                errors.append({'id':row['id'],'reason':'identity_context_or_effect_transfer'})
            checked+=1
        prediction=db.execute("SELECT id FROM edge WHERE tier='predicted' ORDER BY id LIMIT 1").fetchone()[0]
        literal=db.execute("SELECT id FROM edge WHERE tier='extracted' ORDER BY id LIMIT 1").fetchone()
        literal=literal[0] if literal else None
        expected_docs=db.execute('SELECT count(*) FROM candidate_document').fetchone()[0]
        expected_candidates=db.execute('SELECT sum(candidate_count) FROM candidate_document').fetchone()[0]
    atomic_json(output,{'status':'in_progress','phase':'official_identifier_links_checked',
        'official_identifier_links_checked':checked,'errors':errors,'release_id':manifest['release_id']})
    def get(path):
        with urllib.request.urlopen(base+'/api/v1/explorer/'+path,timeout=30) as response:
            if response.status!=200:raise ValueError('Live query failed')
            return json.load(response)
    pred=get('edges/'+prediction)
    if pred['tier']!='predicted' or pred['scientific']['study_evidence']['classification']!='computational_prediction' or pred['context']['experimental_validation'] is not False:
        errors.append({'reason':'prediction_misclassified_in_live_API'})
    wb=get('evidence/'+prediction)
    if wb['layer']!='regulatory_predicted' or any(wb[k] is not None for k in ('effect','p_value','q_value','n_people')) or wb['expert_review_status']!='pending':
        errors.append({'reason':'prediction_statistics_or_review_promoted'})
    docs=[]
    for page in range(1,(expected_docs+199)//200+1):docs+=get(f'candidate-documents?page={page}&page_size=200')['items']
    if len(docs)!=expected_docs or len({d['pmid'] for d in docs})!=expected_docs or sum(d['candidate_count'] for d in docs)!=expected_candidates:
        errors.append({'reason':'live_document_registry_scope'})
    if public:
        data=get('evidence?layer=literature_candidate')
        if data['status']!='withheld' or data['withheld_candidate_count']!=expected_candidates or data['items']!=[]:
            errors.append({'reason':'withheld_candidates_presented_as_absent_or_open'})
    elif literal:
        data=get('edges/'+literal)
        if data['tier']!='extracted' or data['taxon'] is not None or data['scientific']['study_evidence']['classification']!='automated_candidate':
            errors.append({'reason':'literature_candidate_promoted_in_live_API'})
        payload={'release_id':manifest['release_id'],'node_ids':[data['subject'],data['object']],'edge_ids':[data['id']],'format':'json','filters':{}}
        req=urllib.request.Request(base+'/api/v1/explorer/export',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=45) as response:export=json.load(response)
        if any(e['tier']!='restricted' or 'quote' in e.get('context',{}) for e in export['edges']):
            errors.append({'reason':'restricted_abstract_claim_export_leak'})
    result={'status':'passed' if not errors else 'failed','release_id':manifest['release_id'],
        'database_sha256':manifest['database_sha256'],'official_identifier_links_checked':checked,
        'documents_checked_via_HTTP':len(docs),'automatic_candidates_registered':expected_candidates,
        'rights_filtered_public':public,'errors':errors,'expert_semantic_accuracy':'not_evaluated',
        'source_implementation_hash':sha256(Path(__file__))}
    atomic_json(output,result);return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('release','hgnc','output'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--base',required=True);p.add_argument('--public',action='store_true');a=p.parse_args()
    r=verify(a.release,a.hgnc,a.base,a.output,a.public);print(json.dumps(r));raise SystemExit(0 if r['status']=='passed' else 1)
