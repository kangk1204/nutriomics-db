"""CPU-only memory reproduction using saved public-training NER profiles."""
import argparse
import gc
import json
from pathlib import Path
import resource
import time

import psutil

from nutri_literature.evidence_v8.common import atomic, canonical_digest, filehash, read, sources


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--old-home',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
    psutil.Process().nice(10)
    args.output.mkdir(parents=True,exist_ok=False)
    process=psutil.Process();rows=[];profiles=[];start=time.monotonic()
    for p in sorted((args.old_home/'ner/profiles').glob('*.json')):
        value=read(p)
        digest=filehash(p)
        if canonical_digest(value)!=digest:raise ValueError('Canonical bytes changed')
        rows.extend(value['rows'])
        profiles.append({'name':p.name,'rows':len(value['rows']),'sha256':digest,'rss_bytes':process.memory_info().rss})
        print(json.dumps(profiles[-1]),flush=True)
    count=len(rows)
    payload={'rows':rows,'gold_injected':False,'upstream_crossfit':True,'retrieval_limit':200,'menu_limit':50}
    destination=args.output/'id-normalization.json'
    atomic(destination,payload,True)
    digest=filehash(destination)
    del payload,rows,value
    gc.collect()
    reloaded=read(destination)
    assert len(reloaded['rows'])==count
    result={'profiles':profiles,'rows':count,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        'elapsed_seconds':time.monotonic()-start,'combined_sha256':digest,'combined_bytes':destination.stat().st_size,
        'read_write_reload_completed':True,'old_training_profiles_modified':False,'new_training':False,
        'protected_labels_opened':False,'additional_model_calls':0,'source_sha256':sources(),
        'probe_script_sha256':filehash(Path(__file__))}
    atomic(args.output/'receipt.json',result,True)
    print(json.dumps({k:v for k,v in result.items() if k not in ('profiles','source_sha256')}),flush=True)


if __name__=='__main__':main()
