"""Recover official normalization TRAIN coverage with inherited heldout protection."""
import json
from pathlib import Path
import re
import zipfile
from .contract_v7 import immutable,sha,canonical


def sources(home):
    from .corpora_v6 import download_train_corpora,audit_training
    from .splits_v7 import text_key
    home=Path(home);root=home/'v7/normalization-r3';raw=home/'v7/normalization-r2/official'
    parsed=[raw/(name+'-train.json') for name in ('nlmgene','nlmchem')]
    if all(path.exists() for path in parsed):
        # The frozen downloader creates immutable files with mode x; resuming
        # must validate and reuse these outputs rather than write them again.
        values=[json.loads(path.read_bytes()) for path in parsed]
        receipts={r['file']:r for value in values for r in value['sources']}
        for name,record in receipts.items():
            if Path(name).name!=name or sha((raw/name).read_bytes())!=record['sha256']:
                raise ValueError('Official normalization source checksum differs')
        expected=set((raw/'nlmgene-train-ids.txt').read_text().split())
        if {d['pmid'] for d in values[0]['documents']}!=expected or len(expected)!=450:
            raise ValueError('Official NLM-Gene training inventory differs')
        if any(v.get('split')!='train' or v.get('test_labels_read') for v in values):
            raise ValueError('Normalization inputs must be train-only')
        receipt={'nlmgene_train':len(values[0]['documents']),'nlmchem_train':len(values[1]['documents']),
                 'sources':list(receipts.values()),'test_labels_read':False,
                 'parsed_sha256':{p.name:sha(p.read_bytes()) for p in parsed}}
    else:
        receipt=download_train_corpora(raw)
    prior=home/'v6/training-data/audited-complete-v6-e00e690ae91d2648'
    documents=json.loads((prior/'relation-train.json').read_bytes())['documents']
    extra=[]
    for name in ('nlmgene','nlmchem'):
        extra.extend(json.loads((raw/(name+'-train.json')).read_bytes())['documents'])
    # Build the protected CG inventory only from source text and filenames.
    protected=[]
    with zipfile.ZipFile(home/'v7/raw/cg-official.zip') as archive:
        for name in archive.namelist():
            if '/original-data/' in name and any('/'+s+'/' in name for s in ('devel','test')) and name.endswith('.txt'):
                match=re.fullmatch(r'PMID-(\d+)\.txt',Path(name).name)
                if match:protected.append({'pmid':match[1],'text':archive.read(name).decode('utf-8')})
    protected_path=immutable(root/'cg-protected-sources.json',{'documents':protected,'labels_read':False})
    # The v7 normalizer is also checked with its own Unicode normalization.
    old_audit=json.loads((prior/'audit.json').read_bytes())
    text_protection={text_key(d['text']) for d in protected}
    protected_sources=[]
    for record in old_audit['protected_sources']:
        parts=record['path'].replace('\\','/').split('/')
        index=parts.index('runtime')
        path=home.joinpath(*parts[index+1:])
        if not path.exists():path=home/'v7/protected-sources'/record['filename']
        raw_source=path.read_bytes()
        if sha(raw_source)!=record['sha256']:raise ValueError('Inherited protected source checksum differs')
        heldout=json.loads(raw_source)['documents']
        text_protection.update(text_key(d['text']) for d in heldout)
        protected_sources.append({'path':str(path),'sha256':sha(raw_source),'labels_read':False})
    protected_ids=set(old_audit['protected_pmids'])|{d['pmid'] for d in protected}
    filtered=[d for d in [*documents,*extra] if d['pmid'] not in protected_ids and text_key(d['text']) not in text_protection]
    from .data_audit_v7 import canonical_training_documents
    merged=immutable(root/'merged-train.json',{'split':'train','documents':canonical_training_documents(filtered),'test_labels_read':False})
    result=audit_training(merged,[protected_path],root/'audited',prior_audit=prior/'audit.json')
    immutable(root/'source-manifest.json',{'official':receipt,'prior_sha256':sha((prior/'relation-train.json').read_bytes()),
        'audit_sha256':sha((root/'audited/audit.json').read_bytes()),'articles':len(result['training_pmids']),
        'unicode_protected_sources':protected_sources,'unicode_normalization':'NFKC whitespace casefold',
        'test_labels_read':False})
    return result


def automatic(home):
    import os
    from .settings import Settings
    from .operations_v6 import components
    from .dictionaries import load_dictionaries
    from .candidates_v6 import collect
    from .text import sentence_spans
    from .fast_frozen_v6 import SentenceMemoEncoder
    home=Path(home);root=home/'v7/normalization-r3';output=root/'automatic'
    data=root/'audited/relation-train.json';audit=root/'audited/audit.json'
    value=json.loads(data.read_bytes());receipt=json.loads(audit.read_bytes())
    if receipt['audited_training_sha256']!=sha(data.read_bytes()) or not receipt['passed']:
        raise ValueError('Normalization training audit changed')
    os.environ['LITERATURE_V6_MODEL_HOME']=str(home/'v7/frozen-v6-upstream')
    settings=Settings(home,Path('config/topics.json'))
    values=components(settings,'union');values['_encoder']=SentenceMemoEncoder(values['_encoder'])
    dictionaries=load_dictionaries(settings,'heuristic-v2')
    upstream={key:values[key].manifest['manifest_sha256'] for key in ('_v6_models','_clinical_models')}
    old=home/'v6/refinement-r4/automatic-features'
    files=[]
    docs=[d for d in value['documents'] if d.get('gene_annotation_complete') is not False]
    for n,doc in enumerate(docs):
        path=output/('paper-'+doc['pmid']+'.json')
        if not path.exists():
            previous=old/path.name
            if previous.exists():
                row=json.loads(previous.read_bytes())
                if row['source_hash']!=sha(doc['text']):raise ValueError('Cached normalization source mismatch')
                if row['identity']['upstream_models']!=upstream['_v6_models']:
                    raise ValueError('Cached upstream model differs')
                entities=row['entities'];cache_sha=sha(previous.read_bytes())
            else:
                # This boundary receives source only; no gold candidates or IDs.
                sentences=sentence_spans(doc['text'],doc['sections'])
                entities,_,_,_=collect(doc['text'],sentences,dictionaries,values)
                cache_sha=None
            immutable(path,{'pmid':doc['pmid'],'source_hash':sha(doc['text']),'entities':entities,
                'upstream':upstream,'reused_cache_sha256':cache_sha,'source_only':True})
        files.append({'path':path.name,'sha256':sha(path.read_bytes())})
        print(canonical({'stage':'normalization-automatic','papers':n+1,'total':len(docs)}),flush=True)
    result={'files':files,'audit_sha256':sha(audit.read_bytes()),'training_sha256':sha(data.read_bytes()),
            'upstream':upstream,'source_only':True,'split':'train'}
    immutable(output/'manifest.json',result)
    return result
