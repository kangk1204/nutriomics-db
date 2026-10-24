"""Stage only literature-owned code on reserved ports; production 8061 stays untouched."""
import argparse,hashlib,io,json,shlex,subprocess,zipfile,os
from pathlib import Path

HOST='keunsoo@100.110.101.17'
KNOWN='C:/Projects/00_NutriGenomics_auto/outputs/publication/2026-10-22/tailscale-known-hosts'
SSH=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=12','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+KNOWN,HOST]
if os.environ.get('NUTRIOMICS_SSH_JUMP'):SSH[1:1]=['-J',os.environ['NUTRIOMICS_SSH_JUMP']]


def package(root):
    paths=[]
    for name in ('nutrigenomics','config','literature_service/nutri_literature','literature_service/config','frontend/explorer/dist'):
        paths += [p for p in (root/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc']
    paths += [root/'scripts/refresh_platform_snapshots.py',root/'scripts/build_literature_snapshot.py',root/'scripts/serve_verified_release.py',root/'scripts/__init__.py',*root.joinpath('literature_service/scripts').glob('*platform*.py')]
    data=io.BytesIO();manifest={}
    with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(set(paths)):
            if not p.exists():continue
            name=p.relative_to(root).as_posix();blob=p.read_bytes();z.writestr(name,blob);manifest[name]=hashlib.sha256(blob).hexdigest()
        baseline=root/'literature_service/runtime/relation-repair-v6-gpu/verification/baseline-reference.json'
        z.writestr('baseline-reference.json',baseline.read_bytes())
        z.writestr('source-manifest.json',json.dumps(manifest,sort_keys=True))
    return data.getvalue()


def main():
    root=Path(__file__).resolve().parents[1];blob=package(root);digest=hashlib.sha256(blob).hexdigest()
    code="""import sys,io,zipfile,json,hashlib,os
from pathlib import Path
base=Path('/home/keunsoo/projects/nutriomics-literature-platform');blob=sys.stdin.buffer.read();digest=hashlib.sha256(blob).hexdigest();root=base/'web-sources'/digest[:16]
root.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(io.BytesIO(blob)) as z:
 for name in z.namelist():
  p=root/name
  if not p.resolve().is_relative_to(root):raise ValueError('Archive path escape')
  p.parent.mkdir(parents=True,exist_ok=True)
  if p.exists() and p.read_bytes()!=z.read(name):raise ValueError('Immutable source changed')
  p.write_bytes(z.read(name))
manifest=json.loads((root/'source-manifest.json').read_text())
for name,h in manifest.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=h:raise ValueError('Transfer hash mismatch')
baseline=base/'runtime/baseline-reference.json'
if baseline.exists() and baseline.read_bytes()!=(root/'baseline-reference.json').read_bytes():raise ValueError('Baseline changed')
baseline.write_bytes((root/'baseline-reference.json').read_bytes())
pointer=base/'web-source';temp=base/'web-source-next'
if temp.is_symlink():temp.unlink()
temp.symlink_to(root,target_is_directory=True);temp.replace(pointer)
print(json.dumps({'source_sha256':digest,'source_directory':str(root),'files':len(manifest),'production_restarted':False}))
"""
    result=subprocess.run([*SSH,'python3 -c '+shlex.quote(code)],input=blob,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)
    if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace'))
    receipt=json.loads(result.stdout);out=root/'literature_service/runtime/platform-integration';out.mkdir(parents=True,exist_ok=True)
    (out/'staging-source-transfer.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt))


if __name__=='__main__':main()
