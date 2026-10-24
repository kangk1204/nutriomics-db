"""Public source-only inventories. Annotation members/columns are never extracted.

Downloads stay in memory. Only bibliographic IDs, original source text, hashes and
coverage receipts are persisted. BC2GM remains waiting without article mapping.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import time
from urllib.parse import urlsplit
import xml.sax
from xml.sax.handler import feature_external_ges, feature_external_pes
import zipfile

from audit_v8_training_sources import ROOT, canonical, normalized_text_hash

OUT=ROOT/'literature_service/runtime/v8-deployment/public-gold-inventories'
CG_COMMIT='b4a603dee25bd6ac5636017c2be41fd2edc52a3e'
URLS={
 'BioRED':'https://ftp.ncbi.nlm.nih.gov/pub/lu/BioRED/BIORED.zip',
 'BioREDirect':'https://ftp.ncbi.nlm.nih.gov/pub/lu/BioREDirect/datasets.zip',
 'NLM-Chem':'https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMChem/NLM-Chem-corpus.zip',
 'CG':'https://codeload.github.com/openbiocorpora/bionlp-st-2013-cg/zip/'+CG_COMMIT,
 'EBM-NLP':'https://raw.githubusercontent.com/bepnye/EBM-NLP/master/ebm_nlp_2_00.tar.gz',
 'EvidenceOutcomes':'https://raw.githubusercontent.com/ebmlab/EvidenceOutcomes/main/consensus.tar.gz',
 'EvidenceInference':'https://evidence-inference.ebm-nlp.com/v2.0.tar.gz'}


def fetch(url):
    import requests
    allowed={'ftp.ncbi.nlm.nih.gov','raw.githubusercontent.com','codeload.github.com','evidence-inference.ebm-nlp.com'}
    if urlsplit(url).scheme!='https' or urlsplit(url).hostname not in allowed:
        raise ValueError('Only explicit official public source hosts are permitted')
    response=requests.get(url,timeout=(12,150),stream=True);response.raise_for_status()
    chunks=[];size=0
    for part in response.iter_content(1024*1024):
        size+=len(part)
        if size>120*1024**2:raise ValueError('Public source download exceeds the finite size cap')
        chunks.append(part)
    return b''.join(chunks)


def pubtator_headers(stream):
    documents={}
    for raw in stream:
        # Ignore non-header lines before decoding: no entity/relation values parsed.
        m=re.match(rb'^(\d+)\|([ta])\|(.*?)(?:\r?\n)?$',raw)
        if not m:continue
        pmid=m[1].decode('ascii');key=m[2].decode('ascii')
        record=documents.setdefault(pmid,{})
        if key in record:raise ValueError('Duplicate bibliographic header')
        record[key]=m[3].decode('utf8')
    if not documents or any('t' not in d for d in documents.values()):raise ValueError('Incomplete PubTator source headers')
    return [{'pmid':pmid,'text':v['t']+'\n'+v.get('a','')} for pmid,v in sorted(documents.items())]


def rct_headers(stream):
    # Role labels and sentence fields are neither decoded nor retained.
    return [{'pmid':m[1].decode('ascii')} for line in stream
            if (m:=re.fullmatch(rb'###(\d+)\s*',line))]


def plain_tar(raw,prefix,expected=None):
    docs=[]
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        names=[m for m in archive.getmembers() if m.isfile() and m.name.startswith(prefix) and m.name.endswith('.txt')]
        for member in names:
            pmid=Path(member.name).stem
            if not pmid.isdigit():raise ValueError('Unexpected source text filename')
            docs.append({'pmid':pmid,'text':archive.extractfile(member).read().decode('utf8')})
    if expected is not None and len(docs)!=expected:raise ValueError('Official source text inventory count differs')
    return docs


class BioCHeader(xml.sax.handler.ContentHandler):
    """Capture document/source passage data; do not construct annotation subtrees."""
    def __init__(self):
        super().__init__();self.stack=[];self.skip=0;self.key=None;self.docs=[];self.current=None;self.target=None
    def startElement(self,name,attrs):
        self.stack.append(name)
        if self.skip or name in {'annotation','relation'}:self.skip+=1;return
        if name=='document':self.current={'passages':[]}
        if name=='id' and len(self.stack)>1 and self.stack[-2]=='document':self.target='id';self.current['id']=''
        elif name=='infon' and attrs.get('key')=='article-id_pmid':self.target='pmid';self.current['pmid']=''
        elif name=='text' and len(self.stack)>1 and self.stack[-2]=='passage':self.target='text';self.current['passages'].append('')
    def characters(self,content):
        if self.skip or self.current is None:return
        if self.target=='text':self.current['passages'][-1]+=content
        elif self.target:self.current[self.target]+=content
    def endElement(self,name):
        if self.skip:self.skip-=1
        else:
            if name in {'id','infon','text'}:self.target=None
            if name=='document':self.docs.append(self.current);self.current=None
        self.stack.pop()


def bioc_source_headers(raw):
    parser=xml.sax.make_parser()
    parser.setFeature(feature_external_ges,False)
    try:parser.setFeature(feature_external_pes,False)
    except xml.sax.SAXNotSupportedException:pass
    handler=BioCHeader();parser.setContentHandler(handler);parser.parse(io.BytesIO(raw))
    return handler.docs


def official_pmc_map(identifiers):
    """Official bibliographic links only; no article, outcome or annotation API."""
    import requests
    mapping={};receipts=[];identifiers=sorted(identifiers)
    if any(not re.fullmatch(r'[0-9]+',i) for i in identifiers):
        raise ValueError('PMC numeric identifiers required for official mapping')
    endpoint='https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/'
    for at in range(0,len(identifiers),200):
        chunk=identifiers[at:at+200]
        requested={'PMC'+i for i in chunk}
        params={'ids':','.join('PMC'+i for i in chunk),'idtype':'pmcid',
                'format':'json','tool':'NutriOmicsSourceAudit'}
        for retry in range(3):
            response=requests.get(endpoint,params=params,timeout=(12,30))
            if response.status_code not in {429,500,502,503,504}:break
            time.sleep(min(5,2**retry))
        response.raise_for_status()
        payload=response.json()
        if payload.get('status')!='ok' or not isinstance(payload.get('records'),list):
            raise ValueError('NCBI bibliographic mapping returned an error')
        seen=set();confirmed_without_pmid=set()
        for item in payload['records']:
            key=str(item.get('requested-id','')).upper()
            if key not in requested:raise ValueError('NCBI returned an unrequested source identifier')
            if key in seen:raise ValueError('Duplicate official mapping response record')
            seen.add(key)
            pmid=str(item.get('pmid',''))
            if not pmid.isascii() or not pmid.isdecimal():
                if item.get('pmcid')==key and not item.get('errmsg') and item.get('status')!='error':
                    confirmed_without_pmid.add(key)
                continue
            if item.get('pmcid')!=key:raise ValueError('NCBI requested and canonical PMC identifiers differ')
            numeric=key.removeprefix('PMC')
            if numeric in mapping and mapping[numeric]!=pmid:
                raise ValueError('Conflicting official PMID mappings')
            mapping[numeric]=pmid
        receipts.append({'source':'NCBI PMC ID Converter bibliographic API','url':endpoint,
                         'requested_pmcids':len(chunk),'sha256':hashlib.sha256(response.content).hexdigest(),
                         'confirmed_no_pmid_pmcids':sorted(confirmed_without_pmid),
                         'unresolved_pmcids':sorted(requested-({'PMC'+i for i in mapping}&requested)-confirmed_without_pmid),
                         'labels_read':False})
        time.sleep(0.4)
    return mapping,receipts


def medtag_source_excerpts(stream):
    """Only SQL excerpt metadata records; no annotation INSERT is decoded."""
    result=[];selected=False
    for raw in stream:
        if raw.lower().startswith(b'insert into excerpt '):selected=True;continue
        if not selected:continue
        if not raw.strip():continue
        selected=False
        # Only the first three bibliographic fields are inspected. Later source text
        # may contain SQL quoting/commas and is irrelevant to an article-ID inventory.
        m=re.match(rb"values\s*\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*'PMID:\s*(\d+)\b",raw,re.I)
        if not m:continue  # Unmapped excerpts remain an explicitly incomplete inventory.
        result.append({'pmid':str(int(m[1]))})
    return result


def write_inventory(dataset,documents,split_counts,receipt,*,output=OUT,complete=True,waiting_reason=None):
    by_id={};pmcids=set()
    for row in documents:
        key=str(row.get('pmid') or row.get('pmcid') or '')
        if not key:raise ValueError('Source article has no bibliographic identifier')
        if key in by_id and row.get('text') and by_id[key].get('text') and row['text']!=by_id[key]['text']:
            raise ValueError('Conflicting original source text for a document')
        by_id[key]={**by_id.get(key,{}),**row}
        if row.get('pmcid'):pmcids.add(row['pmcid'])
    payload={'dataset':dataset,'split':'all','coverage_complete':complete,
             'expected_documents':len(by_id),'pmids':sorted(by_id),'pmcids':sorted(pmcids),
             'normalized_text_hashes':sorted({normalized_text_hash(v['text']) for v in by_id.values() if v.get('text')}),
             'documents':list(by_id.values()),'split_counts':split_counts,'source_receipt':receipt,
             'labels_read':False,'annotation_members_extracted':False,
             'waiting_reason':waiting_reason,'article_mapping_complete':all(str(k).isdigit() for k in by_id)}
    output=Path(output);output.mkdir(parents=True,exist_ok=True);path=output/(dataset.lower().replace('-','')+'-all-source-inventory.json')
    raw=canonical(payload).encode('utf8')
    if path.exists() and path.read_bytes()!=raw:raise FileExistsError('Version a changed public inventory instead of overwriting it')
    if not path.exists():path.write_bytes(raw)
    return {'name':dataset+'-all-source-inventory','dataset':dataset,'split':'all','kind':'id_manifest',
            'path':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest(),'documents':len(by_id),
            'coverage_complete':complete,'waiting_reason':waiting_reason}


def build_dataset(dataset,*,output=OUT,cache=None):
    cache={} if cache is None else cache
    receipt=[];docs=[];counts={}
    def get(url):
        if url not in cache:cache[url]=fetch(url)
        raw=cache[url];receipt.append({'url':url,'sha256':hashlib.sha256(raw).hexdigest()});return raw
    if dataset=='NLM-Gene':
        for split,expected in [('Train',450),('Test',100)]:
            values=get('https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMGene/Pmidlist.'+split+'.txt').decode().split()
            if len(values)!=expected or any(not p.isdigit() for p in values):raise ValueError('NLMGene official split count differs')
            docs.extend({'pmid':p} for p in values);counts[split.lower()]=len(values)
    elif dataset in {'BioRED','BioREDirect','BC5CDR'}:
        raw=get(URLS['BioREDirect' if dataset=='BC5CDR' else dataset]);z=zipfile.ZipFile(io.BytesIO(raw))
        if dataset=='BioRED':members=[('train','BioRED/Train.PubTator',400),('development','BioRED/Dev.PubTator',100),('test','BioRED/Test.PubTator',100)]
        elif dataset=='BioREDirect':members=[(s,'bioredirect/bioredirect_'+s+'.pubtator',n) for s,n in [('train',400),('dev',100),('test',100),('bc8_test',400)]]
        else:members=[('train','cdr/CDR_TrainingSet.PubTator.txt',500),('development','cdr/CDR_DevelopmentSet.PubTator.txt',500),('test','cdr/CDR_TestSet.PubTator.txt',500)]
        for split,name,expected in members:
            with z.open(name) as stream:part=pubtator_headers(stream)
            if len(part)!=expected:raise ValueError(dataset+' header count differs')
            counts[split]=len(part);docs.extend(part)
    elif dataset=='NLM-Chem':
        z=zipfile.ZipFile(io.BytesIO(get(URLS[dataset])))
        for split,expected in [('train',80),('dev',20),('test',50)]:
            name=next(n for n in z.namelist() if n.endswith('pmcids_'+split+'.txt'))
            identifiers=z.read(name).decode().split()
            if len(identifiers)!=expected:raise ValueError('NLMChem official split count differs')
            counts[split]=len(identifiers)
            for identifier in identifiers:
                article=bioc_source_headers(z.read('FINAL_v1/ALL/'+identifier+'_v1.xml'))
                if len(article)!=1:raise ValueError('NLMChem article header count differs')
                a=article[0];pmcid='PMC'+identifier.removeprefix('PMC')
                docs.append({'pmid':a.get('pmid') or pmcid,'pmcid':pmcid,'text':'\n'.join(a['passages'])})
    elif dataset=='CG':
        z=zipfile.ZipFile(io.BytesIO(get(URLS[dataset])))
        for split,expected in [('train',300),('devel',100),('test',200)]:
            names=[n for n in z.namelist() if '/original-data/'+split+'/' in n and re.search(r'/PMID-\d+\.txt$',n)]
            if len(names)!=expected:raise ValueError('CG source file count differs')
            counts[split]=len(names)
            docs.extend({'pmid':re.search(r'PMID-(\d+)\.txt$',n)[1],'text':z.read(n).decode('utf8')} for n in names)
    elif dataset=='DrugProt':
        path=ROOT/'final_2026/repos/nutriomics-evidence-engine/data/drugprot/drugprot-training-development-test-background.zip'
        if not path.exists():path=ROOT.parent.parent/'final_2026/repos/nutriomics-evidence-engine/data/drugprot/drugprot-training-development-test-background.zip'
        raw=path.read_bytes();receipt.append({'local_source_name':path.name,'sha256':hashlib.sha256(raw).hexdigest()});z=zipfile.ZipFile(io.BytesIO(raw))
        for split,suffix,expected in [('train','training/drugprot_training_abstracs.tsv',3500),('development','development/drugprot_development_abstracs.tsv',750),('test_background','test-background/test_background_abstracts.tsv',10750)]:
            name=next(n for n in z.namelist() if n.endswith(suffix));part=[]
            with z.open(name) as stream:
                for line in stream:
                    fields=line.decode('utf8').rstrip('\r\n').split('\t')
                    if len(fields)!=3 or not fields[0].isdigit():raise ValueError('Not a plain bibliographic TSV')
                    part.append({'pmid':fields[0],'text':fields[1]+'\n'+fields[2]})
            if len(part)!=expected:raise ValueError('DrugProt bibliographic file count differs')
            docs.extend(part);counts[split]=len(part)
    elif dataset in {'EBM-NLP','EvidenceOutcomes'}:
        prefix='ebm_nlp_2_00/documents/' if dataset=='EBM-NLP' else 'consensus/'
        expected=4993 if dataset=='EBM-NLP' else 640
        docs=plain_tar(get(URLS[dataset]),prefix,expected);counts['all-source-texts']=len(docs)
    elif dataset=='PubMed-RCT':
        for split,expected in [('train',15000),('dev',2500),('test',2500)]:
            url='https://raw.githubusercontent.com/Franck-Dernoncourt/pubmed-rct/master/PubMed_20k_RCT/'+split+'.txt'
            part=rct_headers(io.BytesIO(get(url)))
            if len(part)!=expected:raise ValueError('PubMed20kRCT article header count differs')
            counts[split]=len(part);docs.extend(part)
    elif dataset=='EvidenceInference':
        raw=get(URLS[dataset]);z=tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz');ids=set()
        # All released ev1/ev2/combined split lists; no prompts/effect CSV is opened.
        for name in z.getnames():
            if name.startswith('splits/') and name.endswith('_article_ids.txt'):
                values=z.extractfile(name).read().decode().split()
                if any(not p.isdigit() for p in values):raise ValueError('EvidenceInference split IDs are not numeric PMC IDs')
                counts[Path(name).stem]=len(values);ids.update(values)
        if not ids:raise ValueError('EvidenceInference article split inventories missing')
        mapping,map_receipts=official_pmc_map(ids);receipt.extend(map_receipts)
        for identifier in sorted(ids):
            name='txt_files/PMC'+identifier+'.txt'
            try:text=z.extractfile(name).read().decode('utf8')
            except KeyError:raise ValueError('Missing public article source text')
            docs.append({'pmid':mapping.get(identifier,'PMC'+identifier),'pmcid':'PMC'+identifier,'text':text})
        confirmed_no_pmid={p.removeprefix('PMC') for r in map_receipts for p in r['confirmed_no_pmid_pmcids']}
        missing=ids-set(mapping)-confirmed_no_pmid
        counts['pmcid_to_pmid_mapped']=len(mapping);counts['official_pmc_records_without_pmid']=len(confirmed_no_pmid)
        counts['pmcid_without_verified_mapping_status']=len(missing)
        for row in docs:
            row['pmid_mapping_status']='mapped' if row['pmcid'].removeprefix('PMC') in mapping else (
                'official_record_without_pmid' if row['pmcid'].removeprefix('PMC') in confirmed_no_pmid else 'unresolved')
        return write_inventory(dataset,docs,counts,receipt,output=output,complete=not missing,
            waiting_reason=f'{len(missing)} released PMC articles lack a verified current official mapping status' if missing else None)
    elif dataset=='BC2GM':
        url='https://ftp.ncbi.nlm.nih.gov/pub/lsmith/MedTag/medtag.tar.gz'
        raw=get(url)
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as z:
            docs=medtag_source_excerpts(z.extractfile('medtag/genetag/genetag.sql'))
        if not 0<len(docs)<=15000:raise ValueError('Legacy MedTag mapped source count differs')
        return write_inventory(dataset,docs,{'legacy_mapped_source_sentences':len(docs),
            'legacy_unmapped_source_sentences':15000-len(docs),
            'BC2GM_official_sentences':20000,'remaining_source_sentences_unverified':5000},receipt,
            output=output,complete=False,waiting_reason=f'Official 2005 MedTag maps {len(docs)} of 15000 legacy source sentences; later BC2GM 5000 test sentences and replacements remain unverified')
    else:raise ValueError('Unknown explicitly supported dataset')
    return write_inventory(dataset,docs,counts,receipt,output=output)


def main(argv=None):
    from audit_v8_training_sources import REQUIRED, default_config
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,default=OUT)
    p.add_argument('--dataset',action='append',choices=REQUIRED);a=p.parse_args(argv)
    a.output_dir.mkdir(parents=True,exist_ok=True)
    cache={};results=[]
    for dataset in a.dataset or REQUIRED:
        try:r=build_dataset(dataset,output=a.output_dir,cache=cache);results.append(r)
        except Exception as e:results.append({'dataset':dataset,'status':'waiting','error':str(e)[:300]})
        print(canonical({k:v for k,v in results[-1].items() if k in {'dataset','documents','coverage_complete','waiting_reason','status','error'}}),flush=True)
    manifest={'schema_version':'v8-public-source-inventories-v1','items':results,'annotation_members_extracted':False}
    target=a.output_dir/'inventory-build-receipt.json';raw=canonical(manifest).encode()
    if target.exists() and target.read_bytes()!=raw:raise FileExistsError('Use a new output directory for a new inventory build')
    if not target.exists():target.write_bytes(raw)
    config=default_config()
    mapping={'nlmgene':'NLM-Gene','nlmchem':'NLM-Chem','pubmedrct':'PubMed-RCT','evidenceinference':'EvidenceInference'}
    for s in config['sources']:
        for short,name in mapping.items():
            local=a.output_dir/(short+'-test-source-only.json')
            if s['name']==name+'-test-source-only' and local.exists():s['path']=str(local.resolve())
    config['sources'].extend({k:r[k] for k in ('name','dataset','split','kind','path','sha256')}
                             for r in results if r.get('path'))
    c=a.output_dir/'audit-config.json'
    if c.exists() and c.read_text(encoding='utf8')!=canonical(config):raise FileExistsError('Version the audit config')
    if not c.exists():c.write_text(canonical(config),encoding='utf8')


if __name__=='__main__':main()
