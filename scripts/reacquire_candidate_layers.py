"""Reacquire SCREEN predictions and the frozen PMID candidate corpus, never approvals.

Resume only verifies already downloaded batches. A fresh run requires a new root.
No language-model service, credentials or production database is used.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import requests
from nutrigenomics.candidate_rules.dictionaries import load_dictionaries
from nutrigenomics.candidate_rules.extraction import extract, RULES_VERSION
from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json, fetch_source

def parse_pubmed(raw):
    tree = ET.fromstring(raw)
    if tree.tag != 'PubmedArticleSet':
        raise ValueError('Unexpected PubMed response')
    output = []
    def txt(n):
        return ''.join(n.itertext()) if n is not None else ''
    for article in tree:
        if article.tag not in ('PubmedArticle', 'PubmedBookArticle'):
            raise ValueError('Unsupported PubMed element: ' + article.tag)
        book = article.tag == 'PubmedBookArticle'
        citation = article.find('BookDocument' if book else 'MedlineCitation')
        body = citation if book else citation.find('Article')
        pmid = citation.findtext('PMID')
        if not re.fullmatch(r'[1-9]\d*', pmid or ''):
            raise ValueError('Invalid PMID')
        text = txt(body.find('ArticleTitle')) or txt(body.find('Book/BookTitle'))
        sections = [{'label': 'TITLE', 'start': 0, 'end': len(text)}]
        for node in body.findall('Abstract/AbstractText'):
            start = len(text)+1
            text += '\n' + txt(node)
            sections.append({'label':node.get('NlmCategory') or node.get('Label') or 'ABSTRACT',
                             'start':start,'end':len(text)})
        types = [txt(n) for n in body.findall('.//PublicationType')]
        corrections = [{'type':n.get('RefType'),'pmid':n.findtext('PMID')}
                       for n in citation.findall('CommentsCorrectionsList/CommentsCorrections')]
        output.append({'pmid':pmid,'text':text,'sections':sections,'publication_types':types,
            'retracted':'Retracted Publication' in types or any(n['type']=='RetractionIn' for n in corrections),
            'corrections':corrections,'source_url':f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/'})
    if len({r['pmid'] for r in output}) != len(output):
        raise ValueError('Duplicate PMID')
    return output

def acquire(root, source, hgnc):
    root, source, hgnc = map(Path, (root,source,hgnc))
    root.mkdir(parents=True,exist_ok=True)
    if (root/'acquisition.json').exists():
        raise FileExistsError('Completed acquisition is immutable; choose a fresh versioned root')
    status = {'phase':'acquiring','scientific_review':'pending','expert_approved':0}
    atomic_json(root/'status.json',status)
    registry = json.loads((source/'config/update_sources.json').read_text(encoding='utf-8'))
    cfg = next(r for r in registry['sources'] if r['id']=='screen-graphreg')
    receipt = fetch_source(cfg,root/'raw/screen')
    atomic_json(root/'graphreg-source.json',receipt)
    print(json.dumps({'phase':'screen_downloaded','sha256':receipt['sha256']}),flush=True)
    rights=root/'rights';rights.mkdir(exist_ok=True)
    rights_sources={
        'encode-policy':'https://www.encodeproject.org/about/data-use-policy/',
        'screen-about':'https://screen.wenglab.org/about',
        'screen-downloads':'https://screen.wenglab.org/downloads',
        'ENCFF431TFM':'https://www.encodeproject.org/files/ENCFF431TFM/?format=json',
        'ENCFF140XYG':'https://www.encodeproject.org/files/ENCFF140XYG/?format=json'}
    policy_receipts=[]
    for name,url in rights_sources.items():
        r=fetch_source({'id':name.lower(),'url':url,'allowed_hosts':['www.encodeproject.org','screen.wenglab.org'],
            'max_bytes':2_000_000},root/'raw/rights')
        path=rights/(name+('.json' if name.startswith('ENCFF') else '.html'))
        path.write_bytes(Path(r['object_path']).read_bytes())
        policy_receipts.append({'name':name,'url':url,'sha256':r['sha256'],'bytes':path.stat().st_size,
            'retrieved_at':r['retrieved_at'],'status':200})
    atomic_json(rights/'manifest.json',policy_receipts)
    selection_path=source/'config/literature_reacquisition_pmids.json'
    selection=json.loads(selection_path.read_text(encoding='utf-8'))
    study=json.loads((source/'config/human_exposures.v1.json').read_text(encoding='utf-8'))
    pmids=sorted(set(selection['pmids']) | {str(r['pubmed_id']) for r in study['studies'] if r.get('pubmed_id')})
    atomic_json(root/'pmid-selection.json',{'pmids':pmids,'selection_sha256':sha256(selection_path),
        'study_manifest_sha256':sha256(source/'config/human_exposures.v1.json'),
        'previous_corpus':len(selection['pmids']),'scope':'Previous identity selection plus current study PMIDs; no old source text/assertions'})
    batches=root/'raw/pubmed';batches.mkdir(parents=True,exist_ok=True)
    docs=[];sources=[]
    for index,start in enumerate(range(0,len(pmids),100)):
        selected=pmids[start:start+100]
        path=batches/f'batch-{index:03}.xml';manifest=path.with_suffix('.receipt.json')
        if manifest.exists():
            item=json.loads(manifest.read_text(encoding='utf-8'))
            if item['requested_pmids']!=selected or sha256(path)!=item['sha256']:
                raise ValueError('Frozen batch changed')
            raw=path.read_bytes()
        else:
            for attempt in range(4):
                try:
                    response=requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi',
                        params={'db':'pubmed','id':','.join(selected),'retmode':'xml'},
                        timeout=(10,45),headers={'User-Agent':'NutriOmicsResearch/0.1 public-source-rebuild'})
                    response.raise_for_status();raw=response.content
                    if len(raw)>20_000_000:raise ValueError('PubMed byte limit')
                    records=parse_pubmed(raw)
                    if {d['pmid'] for d in records}-set(selected):raise ValueError('Unexpected PMID')
                    item={'url':response.url,'retrieved_at':datetime.now(UTC).isoformat(),
                        'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),
                        'requested_pmids':selected,'returned_pmids':[d['pmid'] for d in records],
                        'export_allowed':False,'rights':'Abstract rights not assessed per article; private research only'}
                    path.write_bytes(raw);atomic_json(manifest,item);break
                except (requests.RequestException,ValueError):
                    if attempt==3:raise
                    time.sleep(2**attempt)
            time.sleep(.4)
        sources.append(item)
        for d in parse_pubmed(raw):
            d['source_snapshot_sha256']=item['sha256'];docs.append(d)
        print(json.dumps({'phase':'pubmed_batch','batch':index,'documents':len(docs)}),flush=True)
    dictionaries=root/'dictionaries';dictionaries.mkdir(exist_ok=True)
    import shutil
    snapshot=dictionaries/'hgnc.tsv';shutil.copyfile(hgnc,snapshot)
    atomic_json(dictionaries/'hgnc-active.json',{'path':snapshot.name,'sha256':sha256(snapshot),
        'version':'hgnc-sha256:'+sha256(snapshot),'source':'HGNC public complete set'})
    shutil.copyfile(source/'config/literature_candidate_entities.json',dictionaries/'entities.json')
    dictionary=load_dictionaries(SimpleNamespace(config=dictionaries/'topics.json',dictionaries=dictionaries))
    items=[];assertion_count=0
    for i,d in enumerate(docs):
        result=extract(d['text'],d['sections'],{'publication_types':d['publication_types'],'organism':'unknown'},dictionary)
        for a in result['assertions']:
            if d['text'][a['start']:a['end']] != a['quote']:
                raise ValueError('Candidate source span differs')
        items.append({**d,'extraction':result,'review_status':'candidate','expert_review_status':'pending',
            'export_allowed':False,'clinical_or_dietary_effect_validated':False})
        assertion_count+=len(result['assertions'])
        if i%50==0:print(json.dumps({'phase':'extracting','papers':i,'candidates':assertion_count}),flush=True)
    atomic_json(root/'literature-candidates.json',{'documents':items,'extractor':RULES_VERSION,
        'dictionary_version':dictionary.version,'expert_approved_assertions':0})
    state={'phase':'acquisition_complete','graphreg':receipt,'sources':sources,'documents':len(docs),
        'requested_pmids':len(pmids),'missing_pmids':sorted(set(pmids)-{d['pmid'] for d in docs}),
        'assertions':assertion_count,'expert_approved':0,'retracted_pmids':[d['pmid'] for d in docs if d['retracted']],
        'literature_sha256':sha256(root/'literature-candidates.json'),'hgnc_sha256':sha256(hgnc),
        'extractor':RULES_VERSION,'dictionary_version':dictionary.version,'export_allowed':False}
    atomic_json(root/'acquisition.json',state);atomic_json(root/'status.json',state)
    return state

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('root','source','hgnc'):p.add_argument('--'+key,type=Path,required=True)
    a=p.parse_args();result=acquire(a.root,a.source,a.hgnc)
    print(json.dumps({k:v for k,v in result.items() if k not in ('graphreg','sources')}))
