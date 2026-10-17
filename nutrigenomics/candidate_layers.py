"""Typed, immutable projection of freshly acquired prediction and text candidates.

No source score is an effect/P/FDR. A verified identity is not a verified claim.
"""
from __future__ import annotations
import csv
import gzip
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .explorer_build import canonical, decode, sha256
from .workbench_build import WORKBENCH_SCHEMA

def graphreg_rows(path, digest, counts=None):
    counts = counts if counts is not None else Counter()
    if sha256(Path(path)) != digest:
        raise ValueError('GraphReg source hash mismatch')
    seen = set()
    with gzip.open(path,'rt',encoding='utf-8') as handle:
        expanded=0
        for number,line in enumerate(handle,1):
            expanded+=len(line.encode())
            if expanded>2_000_000_000:raise ValueError('Expanded source byte budget')
            if not line.strip():continue
            counts['source_rows']+=1
            fields=line.rstrip('\r\n').split('\t')
            if len(fields)!=9:raise ValueError('GraphReg column count changed')
            ccre,target,symbol,biotype,method,interval,accession,cell,numeric=fields
            if method!='GraphRegLR':
                counts['excluded_other_method']+=1;continue
            if not re.fullmatch(r'EH38E\d+',ccre) or not target.strip() or not math.isfinite(float(numeric)):
                raise ValueError('Invalid native prediction record')
            key=hashlib.sha256(canonical(fields).encode()).hexdigest()
            if key in seen:
                counts['exact_duplicate_rows']+=1;continue
            seen.add(key);counts['admitted']+=1
            yield {'id':f'GRL:{digest[:16]}:{key}','fields':fields,'line_number':number,
                'subject':'ENCODE-cCRE:'+ccre,
                'object':('Ensembl:' if re.fullmatch(r'ENSG\d{11}(?:\.\d+)?',target) else 'SCREEN-native-gene:')+target}

def hgnc_mappings(path):
    result=defaultdict(set)
    with Path(path).open(encoding='utf-8-sig') as handle:
        for row in csv.DictReader(handle,delimiter='\t'):
            if row['status']=='Approved' and row.get('ensembl_gene_id'):
                result[row['ensembl_gene_id']].add(row['hgnc_id'])
    return {k:next(iter(v)) for k,v in result.items() if len(v)==1}

def add_exact_gene_bridges(db,hgnc):
    """Official exact native identifier links; neither label merging nor effects transfer."""
    known={r[0] for r in db.execute("SELECT id FROM node WHERE kind='gene'")}
    sha=sha256(Path(hgnc));sid='HGNC:'+sha[:16]
    db.execute('INSERT OR IGNORE INTO source VALUES(?,?)',(sid,canonical({'id':sid,'source':'HGNC official identifiers',
        'url':'https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt',
        'version':sha,'sha256':sha,'license':'CC0-1.0','export_allowed':True})))
    native=defaultdict(set);records=[]
    with Path(hgnc).open(encoding='utf-8-sig') as handle:
        for row in csv.DictReader(handle,delimiter='\t'):
            if row['status']!='Approved':continue
            ids=[row['hgnc_id']]
            if re.fullmatch(r'ENSG\d{11}',row.get('ensembl_gene_id','')):ids.append('Ensembl:'+row['ensembl_gene_id'])
            if re.fullmatch(r'\d+',row.get('entrez_id','')):ids.append('NCBIGene:'+row['entrez_id'])
            for identifier in ids:native[identifier].add(row['hgnc_id'])
            records.append((row,ids))
    count=0
    for row,ids in records:
        available=[i for i in ids if i in known and len(native[i])==1]
        mention='Literature:'+row['hgnc_id']
        if mention in known:available.append(mention)
        if len(available)<2:continue
        anchor=next((i for i in available if i.startswith('Ensembl:')),available[0])
        for other in available:
            if anchor==other:continue
            eid='HGNC-ID:'+hashlib.sha256(canonical([sha,anchor,other]).encode()).hexdigest()
            if db.execute('SELECT 1 FROM edge WHERE id=?',(eid,)).fetchone():continue
            context={'evidence_layer':'molecular_identity','gene_identity_verified':True,
                'mapping_type':'exact_unambiguous_official_native_identifiers','hgnc_id':row['hgnc_id'],
                'native_ensembl_id':row.get('ensembl_gene_id'),'native_entrez_id':row.get('entrez_id'),
                'source_ref':'HGNC complete set record '+row['hgnc_id'],'source_sha256':sha,
                'quantity_transfer_allowed':False,'effect_transfer':False,'export_allowed':True,
                'expert_review_status':'pending','mention_recognition_remains_candidate':other.startswith('Literature:')}
            add_edge(db,eid,anchor,other,'source_identifier_cross_reference','regulatory_identity',context,[sid],review='source_recorded')
            add_workbench(db,eid,anchor,other,'verified_gene_identifier_cross_reference','molecular_identity',sid,context,exported=True)
            count+=1
    return {'added_exact_gene_identity_links':count,'source_sha256':sha,'label_matching_used':False,
        'effect_or_taxon_transfer':False,'nodes_merged':0}

def add_node(db,known,identifier,kind,label,metadata,aliases=()):
    if identifier in known:return
    db.execute('INSERT INTO node VALUES(?,?,?,?,?,?,?,0)',
        (identifier,kind,label,label,canonical(list(aliases)),canonical(metadata),'[]'))
    known.add(identifier)

def add_edge(db,eid,subject,obj,relation,tier,context,sources,pmids=(),taxon=None,review='candidate'):
    db.execute('INSERT INTO edge VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (eid,subject,obj,relation,tier,review,taxon,canonical(context),canonical(sources),canonical(list(pmids)),'[]'))

def add_workbench(db,eid,subject,obj,relation,layer,source,context,tissue=None,organism=None,exported=False):
    # Reuse the edge ID throughout API/graph/exports. No effect values are copied.
    data={'id':eid,'subject_id':subject,'object_id':obj,'relation':relation,
        'context':context,'source_ref':context.get('source_ref'),
        'effect':None,'p_value':None,'q_value':None,'expert_review_status':'pending'}
    db.execute('INSERT INTO workbench_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (eid,subject,obj,relation,layer,None,source,tissue,organism,'candidate',int(exported),canonical(data)))

def graphreg_rights(root):
    """Source-bound ENCODE data policy; never inherited from a code license."""
    root=Path(root)
    receipts=json.loads((root/'rights/manifest.json').read_text(encoding='utf-8'))
    by_name={r['name']:r for r in receipts}
    for name in ('encode-policy','screen-about','screen-downloads','ENCFF431TFM','ENCFF140XYG'):
        row=by_name[name]
        ext='.json' if name.startswith('ENCFF') else '.html'
        if row['status']!=200 or sha256(root/'rights'/ (name+ext))!=row['sha256']:
            raise ValueError('Rights supporting snapshot missing/changed')
    policy=(root/'rights/encode-policy.html').read_text(encoding='utf-8')
    if 'unrestricted use immediately upon release' not in re.sub('<[^>]*>',' ',policy):
        raise ValueError('ENCODE data policy changed; reassess before export')
    for name in ('ENCFF431TFM','ENCFF140XYG'):
        obj=json.loads((root/'rights'/ (name+'.json')).read_text(encoding='utf-8'))
        if obj.get('status')!='released' or obj.get('assembly')!='GRCh38' or 'GraphRegLR' not in obj.get('submitted_file_name',''):
            raise ValueError('Native GraphRegLR file association not established')
    return {'export_allowed':True,'license':'ENCODE unrestricted data use policy; attribution retained',
        'terms_url':by_name['encode-policy']['url'],'terms_sha256':by_name['encode-policy']['sha256'],
        'rights_basis':'SCREEN ENCODE derivative with released native ENCODE file accessions confirmed',
        'rights_scope':'GraphRegLR data only; not PubMed abstracts or other sources',
        'native_files':['ENCFF431TFM','ENCFF140XYG'],'scientific_approval':False}

def integrate(db,root,hgnc,public=False):
    root=Path(root);db.row_factory=sqlite3.Row
    db.executescript(WORKBENCH_SCHEMA)
    db.executescript('''CREATE TABLE IF NOT EXISTS candidate_document(
        pmid TEXT PRIMARY KEY,source_id TEXT NOT NULL,source_sha256 TEXT NOT NULL,
        extraction_version TEXT NOT NULL,candidate_count INTEGER NOT NULL,retracted INTEGER NOT NULL,
        metadata_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS wb_order_candidates ON workbench_evidence(layer,study_id,tissue,id);
        CREATE INDEX IF NOT EXISTS wb_review_candidates ON workbench_evidence(review_status,layer,id);''')
    known={r[0] for r in db.execute('SELECT id FROM node')}
    initial_known=set(known)
    receipt=json.loads((root/'graphreg-source.json').read_text(encoding='utf-8'))
    rights=graphreg_rights(root)
    sid='SCREEN:GraphRegLR:'+receipt['sha256'][:16]
    src={**rights,'id':sid,'source':'SCREEN GraphRegLR','url':receipt['url'],'version':'V4-hg38',
        'sha256':receipt['sha256'],'retrieved_at':receipt['retrieved_at'],'evidence_layer':'regulatory_predicted',
        'expert_review_status':'pending','raw_numeric_semantics':'native untyped thresholded inclusion field; not effect/P/FDR or calibrated probability'}
    db.execute('INSERT INTO source VALUES(?,?)',(sid,canonical(src)))
    counts=Counter();contexts=Counter();targets=set();first=[]
    for row in graphreg_rows(receipt['object_path'],receipt['sha256'],counts):
        ccre,target,symbol,biotype,method,interval,accession,cell,numeric=row['fields']
        if accession not in rights['native_files']:raise ValueError('Unassessed native file accession')
        subject,obj=row['subject'],row['object'];targets.add((obj,target))
        add_node(db,known,subject,'regulatory_region',ccre,{'namespace':'ENCODE-cCRE','identifier':ccre,'genome_build':'GRCh38/hg38','source_ids':[sid]})
        add_node(db,known,obj,'gene',symbol or target,{'source_native_identity':True,'namespace':obj.split(':')[0],
            'identifier':target,'source_symbol':symbol,'source_ids':[sid]},[target,symbol])
        context={'source_raw_fields':row['fields'],'line_number':row['line_number'],'method':'GraphRegLR',
            'source_ref':f"SCREEN source line {row['line_number']}",'source_accession':accession,
            'source_interval':interval,'genome_build':'GRCh38/hg38','source_symbol':symbol,
            'source_numeric_fields':{'native_column_9':numeric},'tissue':cell,'organism':'Homo sapiens',
            'score_semantics':src['raw_numeric_semantics'],'evidence_layer':'regulatory_predicted',
            'review_status':'candidate','expert_review_status':'pending','source_validation_status':'source_bytes_and_schema_checked',
            'experimental_validation':False,'dietary_effect':None,'health_effect':None,'export_allowed':rights['export_allowed'],
            'interpretation':'Cell-line computational prediction; no human dietary response or clinical benefit established.'}
        add_edge(db,row['id'],subject,obj,'predicted_ccre_gene_link','predicted',context,[sid],taxon='Homo sapiens')
        add_workbench(db,row['id'],subject,obj,'predicted_ccre_gene_link','regulatory_predicted',sid,context,cell,'Homo sapiens',rights['export_allowed'])
        contexts[cell]+=1
        if len(first)<3:first.append(row['id'])
        if counts['admitted']%100000==0:
            print(canonical({'phase':'graphreg_projection','rows':counts['admitted']}),flush=True)
    mappings=hgnc_mappings(hgnc)
    hsid='HGNC:'+sha256(Path(hgnc))[:16]
    db.execute('INSERT OR IGNORE INTO source VALUES(?,?)',(hsid,canonical({'id':hsid,'source':'HGNC official complete set',
        'url':'https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt',
        'version':sha256(Path(hgnc)),'sha256':sha256(Path(hgnc)),'export_allowed':True,'license':'CC0-1.0'})))
    exact=0
    for obj,target in sorted(targets):
        mapped=mappings.get(target)
        if mapped and mapped in initial_known and obj!=mapped:
            eid='GRL-ID:'+hashlib.sha256(canonical([obj,mapped,hsid]).encode()).hexdigest()
            add_edge(db,eid,obj,mapped,'source_identifier_cross_reference','regulatory_identity',
                {'mapping_type':'exact_unambiguous_official_Ensembl_to_HGNC','evidence_layer':'molecular_identity',
                 'gene_identity_verified':True,'effect_transfer':False,'export_allowed':True},[hsid],review='source_recorded')
            add_workbench(db,eid,obj,mapped,'verified_gene_identifier_cross_reference','molecular_identity',hsid,
                {'gene_identity_verified':True,'mapping_type':'exact_unambiguous_official_Ensembl_to_HGNC',
                 'source_ref':'HGNC complete set, exact Ensembl ID','export_allowed':True,'effect_transfer':False},exported=True)
            exact+=1
    literature=json.loads((root/'literature-candidates.json').read_text(encoding='utf-8'))
    lcounts=Counter();lfirst=[]
    for paper in literature['documents']:
        pmid=paper['pmid'];lsid='PubMed-candidates:'+pmid+':'+paper['source_snapshot_sha256'][:16]
        pmeta={'id':lsid,'source':'PubMed abstract candidate extraction','url':paper['source_url'],
            'version':paper['source_snapshot_sha256'],'sha256':paper['source_snapshot_sha256'],
            'export_allowed':False,'rights':'Abstract/derived claim redistribution not verified per paper',
            'evidence_layer':'literature_candidate','expert_review_status':'pending'}
        db.execute('INSERT INTO source VALUES(?,?)',(lsid,canonical(pmeta)))
        assertions=paper['extraction']['assertions'];lcounts['documents']+=1
        db.execute('INSERT INTO candidate_document VALUES(?,?,?,?,?,?,?)',(pmid,lsid,paper['source_snapshot_sha256'],
            literature['extractor'],len(assertions),int(paper['retracted']),canonical({'source_url':paper['source_url'],
            'publication_types':paper['publication_types'],'review_status':'candidate','expert_review_status':'pending',
            'text_export_allowed':False,'assertion_visibility':'withheld' if public else 'internal_review'})))
        if public:
            lcounts['withheld_assertions']+=len(assertions);continue
        if paper['retracted']:
            lcounts['excluded_retracted_assertions']+=len(assertions);continue
        for a in assertions:
            if paper['text'][a['start']:a['end']]!=a['quote']:
                raise ValueError('Literature candidate source span changed')
            lcounts['source_span_checks']+=1
            endpoints=[]
            for side in ('subject','object'):
                mention=a['payload'][side+'_mention'];native=a[side]
                # Exact normalized HGNC identity only; entity recognition remains a candidate.
                identifier=native if native.startswith('HGNC:') and native in known else 'Literature:'+native
                kind={'compound':'chemical','protein':'gene'}.get(mention['kind'],mention['kind'])
                add_node(db,known,identifier,kind,mention.get('payload',{}).get('label') or mention['text'],
                    {'original_identifier':native,'mapping_status':'candidate_namespace_not_merged',
                     'review_status':'candidate','source_ids':[lsid]},[mention['text']])
                endpoints.append(identifier)
            context={'evidence_layer':'literature_candidate','extractor':literature['extractor'],
                'dictionary_version':paper['extraction']['dictionary_version'],'source_snapshot_sha256':paper['source_snapshot_sha256'],
                'source_ref':f"PMID {pmid}, parsed title/abstract characters [{a['start']},{a['end']})",
                'quote':a['quote'],'start':a['start'],'end':a['end'],'payload':a['payload'],
                'pmid':pmid,'review_status':'candidate','expert_review_status':'pending','export_allowed':False,
                'source_span_verified':True,'semantic_accuracy':'not_evaluated_by_human_expert',
                'organism':None,'tissue':None,'dose':None,'duration':None,'effect':None,'p_value':None,'q_value':None,
                'interpretation':'Automatically extracted statement candidate; not a verified intervention effect or causal/health claim.'}
            eid='LIT:'+hashlib.sha256(canonical([pmid,paper['source_snapshot_sha256'],a,literature['extractor']]).encode()).hexdigest()
            add_edge(db,eid,*endpoints,a['predicate'],'extracted',context,[lsid],[pmid])
            add_workbench(db,eid,*endpoints,a['predicate'],'literature_candidate',lsid,context)
            lcounts['admitted_assertions']+=1
            if len(lfirst)<3:lfirst.append(eid)
    return {'graphreg':dict(counts),'graphreg_contexts':dict(contexts),'exact_gene_cross_references':exact,
        'literature':dict(lcounts),'representative_graphreg_ids':first,'representative_literature_ids':lfirst,
        'expert_approved_assertions':0,'effect_values_copied':0,'graphreg_rights':rights,
        'literature_corpus_sha256':sha256(root/'literature-candidates.json')}

def validate_projection(db,root,summary,public=False):
    """Every admitted source record, native field, direction and candidate label."""
    root=Path(root);receipt=json.loads((root/'graphreg-source.json').read_text(encoding='utf-8'))
    counts=Counter();matched=0
    for original in graphreg_rows(receipt['object_path'],receipt['sha256'],counts):
        row=db.execute('SELECT * FROM edge WHERE id=?',(original['id'],)).fetchone()
        wb=db.execute('SELECT * FROM workbench_evidence WHERE id=?',(original['id'],)).fetchone()
        if not row or not wb:raise ValueError('Missing native prediction')
        c=decode(row['context_json'])
        if (row['subject']!=original['subject'] or row['object']!=original['object']
            or row['tier']!='predicted' or row['review_status']!='candidate'
            or c['source_raw_fields']!=original['fields'] or c['line_number']!=original['line_number']
            or c['experimental_validation'] is not False or c['health_effect'] is not None
            or wb['layer']!='regulatory_predicted' or wb['tissue']!=original['fields'][7]
            or any(decode(wb['metadata_json']).get(k) is not None for k in ('effect','p_value','q_value'))):
            raise ValueError('Prediction value/context/label changed')
        matched+=1
    actual=db.execute("SELECT count(*) FROM edge WHERE relation='predicted_ccre_gene_link'").fetchone()[0]
    if counts!=summary['graphreg'] or matched!=actual:raise ValueError('Prediction coverage mismatch')
    literature=json.loads((root/'literature-candidates.json').read_text(encoding='utf-8'))
    checked=0
    for paper in literature['documents']:
        document=db.execute('SELECT * FROM candidate_document WHERE pmid=?',(paper['pmid'],)).fetchone()
        if not document or document['candidate_count']!=len(paper['extraction']['assertions']):
            raise ValueError('Literature document coverage mismatch')
        if public or paper['retracted']:continue
        for a in paper['extraction']['assertions']:
            eid='LIT:'+hashlib.sha256(canonical([paper['pmid'],paper['source_snapshot_sha256'],a,literature['extractor']]).encode()).hexdigest()
            row=db.execute('SELECT * FROM edge WHERE id=?',(eid,)).fetchone()
            if not row:raise ValueError('Missing literature candidate')
            c=decode(row['context_json'])
            if (row['tier']!='extracted' or row['review_status']!='candidate' or row['taxon'] is not None
                or row['relation']!=a['predicate'] or c['quote']!=paper['text'][a['start']:a['end']]
                or c['payload']!=a['payload'] or c['export_allowed'] is not False
                or decode(row['pmids_json'])!=[paper['pmid']]
                or any(c.get(k) is not None for k in ('dose','duration','effect','p_value','q_value','organism','tissue'))):
                raise ValueError('Literature candidate meaning/traceability label changed')
            checked+=1
    expected=0 if public else summary['literature'].get('admitted_assertions',0)
    if db.execute("SELECT count(*) FROM edge WHERE id LIKE 'LIT:%'").fetchone()[0]!=expected or checked!=expected:
        raise ValueError('Candidate count mismatch')
    return {'graphreg_all_source_records_checked':matched,'literature_all_source_spans_checked':checked,
        'literature_documents_checked':len(literature['documents']),'mismatches':0,
        'semantic_or_expert_accuracy':'not_evaluated','expert_approved':0,
        'rights_filtered_public':public,'source_sha256':receipt['sha256']}
