"""Full closed-file SQLite integrity on a SHA-identical RAM copy, then atomic promotion.

Random integrity reads on an HDD can dominate a release build. This changes the
verification medium only. Serving and HTTP benchmarks still use permanent disk.
"""
import argparse
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from nutrigenomics.explorer_build import sha256,verify_closed_sqlite,read_only
from nutrigenomics.source_updates import atomic_json

def verify_large_closed(path,proof_path):
    path=Path(path).resolve();size=path.stat().st_size
    ram=Path('/dev/shm')
    available=int(next(l.split()[1] for l in Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))*1024
    if not ram.is_dir() or shutil.disk_usage(ram).free<size+2_000_000_000 or available<size+8_000_000_000:
        raise RuntimeError('Insufficient declared RAM reserve for full integrity; preserve staging and retry with disk verification')
    target=ram/('nutriomics-candidate-integrity-'+uuid.uuid4().hex+'.sqlite')
    before=path.stat()
    try:
        shutil.copyfile(path,target)
        original=sha256(path);copied=sha256(target)
        if original!=copied:raise ValueError('Integrity RAM copy differs')
        old=os.environ.get('NUTRIGENOMICS_INTEGRITY_CACHE_MIB')
        os.environ['NUTRIGENOMICS_INTEGRITY_CACHE_MIB']='4096'
        try:verify_closed_sqlite(target)
        finally:
            if old is None:os.environ.pop('NUTRIGENOMICS_INTEGRITY_CACHE_MIB',None)
            else:os.environ['NUTRIGENOMICS_INTEGRITY_CACHE_MIB']=old
        after=path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or sha256(path)!=original:
            raise ValueError('Permanent database changed during closed-copy integrity')
        proof={'source_database':path.name,'source_reference':'Relative to this immutable release',
            'database_sha256':original,'ram_copy_sha256':copied,
            'bytes':size,'integrity_check':'ok','foreign_key_check':[],
            'verification_scope':'Entire closed SQLite file on a SHA-identical RAM copy',
            'not_an_http_performance_measurement':True,'permanent_source_unchanged':True}
        atomic_json(proof_path,proof);return proof
    finally:
        target.unlink(missing_ok=True)

def finalize(staging,output,public=False):
    staging,output=map(Path,(staging,output))
    if output.exists():raise FileExistsError('Completed release is immutable')
    filename='nutriomics.sqlite3' if public else 'explorer.sqlite'
    proof=verify_large_closed(staging/filename,staging/'closed-integrity.json')
    with read_only(staging/filename) as db:
        manifest={r[0]:json.loads(r[1]) for r in db.execute('SELECT * FROM meta')}
    manifest['database_sha256']=proof['database_sha256']
    atomic_json(staging/'coverage.json',manifest['counts'])
    atomic_json(staging/'candidate-layers.json',manifest['candidate_layers'])
    if public:
        manifest['files']=[{'path':str(p.relative_to(staging)),'bytes':p.stat().st_size,'sha256':sha256(p)}
            for p in sorted(staging.rglob('*')) if p.is_file() and p.name!='manifest.json']
    atomic_json(staging/'manifest.json',manifest)
    os.replace(staging,output)
    print(json.dumps({'release_id':manifest['release_id'],'database_sha256':proof['database_sha256'],
        'closed_integrity':'passed','source_accuracy':json.loads((output/'source-accuracy.json').read_text())}),flush=True)
    return manifest

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--staging',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--public',action='store_true');a=p.parse_args();finalize(a.staging,a.output,a.public)
