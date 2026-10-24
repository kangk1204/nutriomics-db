from pathlib import Path
from ..repair_v4.common import ROOT,PRIOR,MODELS,read,canonical,sha,immutable,data,baseline,native_metrics,budget_status
V4=ROOT/'runtime/chatgpt-repair-v4'
HOME=ROOT/'runtime/chatgpt-repair-v5'
SEED=20261017
FLOOR=65/154

def write(name,value):
    immutable(HOME/name,value)
    return value

def log(stage,**values):print(canonical({'stage':stage,**values}),flush=True)

def filehash(path):
    import hashlib
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def sources():
    files=[*Path(__file__).parent.glob('*.py'),ROOT/'scripts/chatgpt_repair_v5.py',ROOT/'tests/test_repair_v5.py',ROOT/'requirements-repair-v5.txt']
    return {str(p.relative_to(ROOT)):sha(p.read_bytes()) for p in sorted(files) if p.exists()}

def cached(split):
    ids=read(V4/'cohort.json')['pmids'] if split=='train' else [d['pmid'] for d in data(split)]
    docs={d['pmid']:d for d in data(split)}
    for pmid in ids:
        p=read(V4/'rebuilt'/split/('paper-'+pmid+'.json'))
        p['document']=docs[pmid];p['sample_weight']=read(V4/'cohort.json')['weights'][pmid] if split=='train' else 1.
        for m in p['mentions']:m['_source']=p['document']['text']
        yield p
