"""Extract a pinned additive source archive only inside the isolated v7 checkout."""
import argparse,hashlib,json,tarfile
from pathlib import Path


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('archive',type=Path);p.add_argument('sha256');args=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    if root.name!='nutriomics-literature-v7-development':raise SystemExit('Isolated v7 checkout required')
    raw=args.archive.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=args.sha256:raise ValueError('Source package checksum differs')
    directory=root/'runtime/v7/source-archives';directory.mkdir(parents=True,exist_ok=True)
    target=directory/('source-'+args.sha256+'.tar.gz')
    if not target.exists():target.write_bytes(raw)
    with tarfile.open(args.archive) as t:
        for m in t:
            if not m.isfile() or not (root/m.name).resolve().is_relative_to(root):raise ValueError('Unsafe archive member')
            if not m.name.startswith(('nutri_literature/','scripts/','tests/','docs/','ui/')):raise ValueError('Unapproved source path')
        t.extractall(root,filter='data')
        count=len(t.getmembers())
    print(json.dumps({'source_sha256':args.sha256,'files':count}))
