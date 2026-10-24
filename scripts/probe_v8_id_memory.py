"""Reproduce nested-profile residency using saved public training artifacts only."""
import argparse
import json
from pathlib import Path
import resource
import time

import psutil

from nutri_literature.evidence_v8.common import atomic, canonical_digest, filehash, read


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--old-home',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    resource.setrlimit(resource.RLIMIT_AS,(8*1024**3,8*1024**3))
    psutil.Process().nice(10)
    args.output.mkdir(parents=True,exist_ok=False)
    paths=[args.old_home/'examples/id-normalization.json',*sorted((args.old_home/'ner/profiles').glob('*.json'))]
    retained=[];receipts=[];started=time.monotonic()
    for path in paths:
        value=read(path)
        before=filehash(path)
        if canonical_digest(value)!=before:raise ValueError('Candidate values or canonical bytes changed')
        retained.append(value)
        row={'file':str(path.relative_to(args.old_home)),'sha256':before,'rows':len(value['rows']),
             'rss_bytes':psutil.Process().memory_info().rss,'bytes':path.stat().st_size}
        receipts.append(row);print(json.dumps(row),flush=True)
    result={'files':receipts,'rows_retained_together':sum(r['rows'] for r in receipts),
            'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'elapsed_seconds':time.monotonic()-started,'canonical_values_and_hashes_identical':True,
            'old_profiles_modified':False,'training_or_scientific_performance_evaluation':False,
            'new_model_calls':0,'sealed_labels_read':False}
    atomic(args.output/'receipt.json',result,True)
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
