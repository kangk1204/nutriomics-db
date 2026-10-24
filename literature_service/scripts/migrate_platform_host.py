"""Preserve a read-only host through an online SQLite snapshot and hashed transfer."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
from pathlib import Path
import subprocess
import sys
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
OUT = PROJECT / "runtime/platform-integration/migration"
OLD_ROOT = "/home/keunsoo/projects/nutriomics-literature"
NEW_ROOT = "/home/keunsoo/projects/nutriomics-literature-platform"
KNOWN = WORKSPACE / "outputs/publication/2026-10-22/tailscale-known-hosts"
SOURCE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "keunsoo@100.82.52.41"]
TARGET = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "StrictHostKeyChecking=yes",
          "-o", "UserKnownHostsFile=" + str(KNOWN), "keunsoo@100.110.101.17"]


def digest(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def receive(name, command, *, stdin=None):
    OUT.mkdir(parents=True, exist_ok=True)
    final = OUT / name
    if final.exists():
        print(json.dumps({"reused": name, "bytes": final.stat().st_size, "sha256": digest(final)}), flush=True)
        return final
    part = final.with_suffix(final.suffix + ".part")
    with part.open("wb") as stream:
        result = subprocess.run(command, input=stdin, stdout=stream, stderr=subprocess.PIPE, timeout=1800)
    if result.returncode:
        raise RuntimeError("Snapshot transfer failed: " + result.stderr.decode(errors="replace")[-1500:])
    part.replace(final)
    print(json.dumps({"captured": name, "bytes": final.stat().st_size, "sha256": digest(final)}), flush=True)
    return final


def capture():
    code = r'''
import sqlite3,gzip,sys,os,json
from pathlib import Path
root=Path('/home/keunsoo/projects/nutriomics-literature/runtime')
available=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))*1024
if available<8*1024**3:raise MemoryError('Online migration requires 8 GiB available memory')
src=sqlite3.connect('file:'+str(root/'data/literature.sqlite3')+'?mode=ro',uri=True)
src.execute('PRAGMA temp_store=MEMORY')
dest=sqlite3.connect(':memory:');src.backup(dest,pages=8192)
if dest.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('SQLite snapshot quick_check failed')
counts={t:dest.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['papers','sources','extractions','reviews','reports','accounts']}
sys.stderr.write(json.dumps({'consistent_snapshot_counts':counts})+'\n')
with gzip.GzipFile(fileobj=sys.stdout.buffer,mode='wb',compresslevel=1,mtime=0) as stream:stream.write(dest.serialize())
dest.close();src.close()
'''
    receive("literature-consistent.sqlite3.gz", SOURCE + ["python3", "-"], stdin=code.encode())
    receive("preserved-assets.tar.gz", SOURCE + ["tar", "-czf", "-", "-C", OLD_ROOT + "/runtime", "raw", "dictionaries", "engine-artifacts", "reports"])
    manifest = {p.name: {"sha256": digest(p), "bytes": p.stat().st_size} for p in OUT.glob("*.gz")}
    (OUT / "capture-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    return manifest


def source_archive():
    path = OUT / "platform-source.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in ("nutri_literature", "scripts", "config", "tests"):
            for p in sorted((PROJECT / folder).rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo"):
                    z.write(p, str(p.relative_to(PROJECT)).replace("\\", "/"))
        z.write(PROJECT / "pyproject.toml", "pyproject.toml")
    return path


def upload(path):
    remote = NEW_ROOT + "/transfer/" + path.name
    code = "import sys,pathlib,hashlib; p=pathlib.Path(" + repr(remote) + ");p.parent.mkdir(parents=True,exist_ok=True);data=sys.stdin.buffer.read();assert hashlib.sha256(data).hexdigest()==" + repr(digest(path)) + ";p.write_bytes(data);print(len(data))"
    with path.open("rb") as data:
        result = subprocess.run(TARGET + ["python3 -c " + shlex.quote(code)], stdin=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    if result.returncode:
        raise RuntimeError("Migration upload failed: " + result.stderr.decode(errors="replace")[-1500:])
    print(json.dumps({"uploaded": path.name, "bytes": int(result.stdout)}), flush=True)


def deploy():
    manifest = json.loads((OUT / "capture-manifest.json").read_text())
    for name, record in manifest.items():
        if digest(OUT / name) != record["sha256"]:
            raise ValueError("Captured migration asset changed")
        upload(OUT / name)
    upload(source_archive())
    code = r'''
import pathlib,json,zipfile,tarfile,gzip,sqlite3,hashlib,os
root=pathlib.Path('/home/keunsoo/projects/nutriomics-literature-platform');runtime=root/'runtime'
root.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(root/'transfer/platform-source.zip') as z:
 for member in z.infolist():
  dest=(root/member.filename).resolve()
  if not dest.is_relative_to(root):raise ValueError('Source archive path escape')
  dest.parent.mkdir(parents=True,exist_ok=True);data=z.read(member)
  if dest.exists() and dest.read_bytes()!=data:raise ValueError('Initial source deploy would overwrite a different file')
  if not dest.exists():dest.write_bytes(data)
runtime.mkdir(exist_ok=True)
with tarfile.open(root/'transfer/preserved-assets.tar.gz','r:gz') as t:t.extractall(runtime,filter='data')
db=runtime/'data/literature.sqlite3';db.parent.mkdir(parents=True,exist_ok=True)
if not db.exists():
 with gzip.open(root/'transfer/literature-consistent.sqlite3.gz','rb') as src, db.with_suffix('.part').open('wb') as dst:
  while chunk:=src.read(1024*1024):dst.write(chunk)
 db.with_suffix('.part').replace(db);os.chmod(db,0o600)
 c=sqlite3.connect(db);c.execute('PRAGMA temp_store=MEMORY')
 if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Restored snapshot corrupt')
 old='/home/keunsoo/projects/nutriomics-literature/runtime/'
 replacements=c.execute('select source_id,raw_path from sources').fetchall()
 for sid,path in replacements:
  if path.startswith(old):
   new=runtime/path[len(old):]
   if not new.is_file():raise FileNotFoundError(str(new))
   c.execute('update sources set raw_path=? where source_id=?',(str(new),sid))
 c.commit();counts={t:c.execute('select count(*) from '+t).fetchone()[0] for t in ['papers','sources','extractions','reviews','reports','accounts']};c.close()
 receipt={'source_host':'100.82.52.41','source_read_only':True,'target_host':'100.110.101.17','consistent_snapshot':True,'counts':counts,'raw_paths_rebased':len(replacements),'original_host_untouched':True}
 (runtime/'migration.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt))
else:print((runtime/'migration.json').read_text())
'''
    result = subprocess.run(TARGET + ["python3", "-"], input=code.encode(), capture_output=True, timeout=1800)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-2500:])
    print(result.stdout.decode(), flush=True)
    (OUT / "restore-receipt.json").write_bytes(result.stdout)


if __name__ == "__main__":
    p = argparse.ArgumentParser();p.add_argument("action", choices=["capture", "deploy"])
    args = p.parse_args()
    capture() if args.action == "capture" else deploy()
