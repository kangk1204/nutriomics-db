"""Deploy isolated v8 sources/assets; never change another experiment's environment."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "/home/keunsoo/projects/nutriomics-evidence-v8"
ALLOWED_HOSTS = ("100.84.183.6", "100.93.148.94", "100.68.188.102")
# This additional peer was authenticated by the live Tailscale network map.
# Pin that receipt rather than accepting an SSH key on first connection.
PINNED_HOST_KEYS = {"100.68.188.102": (
    ROOT / "literature_service/runtime/v8-deployment/verified-host-100.68.188.102-20261023",
    "cc6ce84a4a02cffc1d55c405b92a30e48f65c078af44b56263be9237e20e6698")}
# Observed tailnet address of the SAME peer; its existing IPv4 host key remains
# mandatory. Never accept a new host identity to work around a connection error.
VERIFIED_TAILSCALE_V6 = {"100.84.183.6": "fd7a:115c:a1e0::134:b707"}
ORIGINAL = "/home/keunsoo/projects/nutriomics-relation-v6-gpu-r2/runtime/relation-repair-v6-gpu-r2"


class RemoteTransportError(RuntimeError):
    """A failed SSH transport, distinct from authentication or remote computation."""


def ssh(host, *, ipv6=False):
    if host not in ALLOWED_HOSTS:
        raise ValueError("GPU host is outside the verified deployment allowlist")
    if ipv6 and host not in VERIFIED_TAILSCALE_V6:
        raise ValueError("No verified IPv6 route for this GPU host")
    # IPv4 is the default verified route. Explicit IPv6 selection keeps the
    # same host key; an unavailable route never replays a remote mutation.
    endpoint = VERIFIED_TAILSCALE_V6[host] if ipv6 else host
    verified_route = ["-6"] if ipv6 else []
    if host in VERIFIED_TAILSCALE_V6:
        verified_route += ["-o", "HostKeyAlias=" + host]
    if host in PINNED_HOST_KEYS:
        keyfile, expected = PINNED_HOST_KEYS[host]
        if not keyfile.is_file() or hashlib.sha256(keyfile.read_bytes()).hexdigest() != expected:
            raise ValueError("Verified Tailscale host-key receipt changed")
        verified_route += ["-o", "UserKnownHostsFile=" + keyfile.as_posix()]
    return ["ssh", *verified_route, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o",
            "ServerAliveCountMax=2", "keunsoo@" + endpoint]


def run_remote(host, code, payload=b"", *, timeout=180, stdout=subprocess.PIPE):
    command = "python3 -c " + shlex.quote(code)
    try:
        p = subprocess.run([*ssh(host), command], input=payload,
                           stdout=stdout, stderr=subprocess.PIPE, timeout=timeout)
        # These exact OpenSSH errors occur before a TCP connection is made.
        # Never replay a command after authentication, disconnection, an
        # unknown timeout, or a remote nonzero exit.
        connection_error = re.fullmatch(
            r'ssh: connect to host ' + re.escape(host)
            + r' port 22: (?:Connection timed out|Connection refused|Network is unreachable|No route to host)',
            p.stderr.decode(errors='replace').strip())
        if p.returncode == 255 and connection_error and host in VERIFIED_TAILSCALE_V6:
            p = subprocess.run([*ssh(host, ipv6=True), command], input=payload,
                               stdout=stdout, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"SSH host {host} exceeded {timeout}s; remote completion unconfirmed") from None
    if p.returncode:
        full_message = p.stderr.decode(errors="replace")
        message = full_message[-1200:]
        lower = full_message.lower()
        authentication = any(s in lower for s in ['host key', 'host identification', 'permission denied', 'authenticat'])
        transport = any(s in lower for s in ['connection timed out', 'connection reset', 'connection refused',
            'connection closed', 'closed by remote host', 'network is unreachable', 'no route to host', 'broken pipe'])
        if p.returncode == 255 and transport and not authentication:
            raise RemoteTransportError("Remote transport failed: " + message)
        raise RuntimeError("Remote operation failed: " + message)
    return p


def remote(host, code, payload=b"", *, timeout=180):
    p = run_remote(host, code, payload, timeout=timeout)
    return p.stdout


def package_sources(root=ROOT):
    paths = list((root / "literature_service/nutri_literature").rglob("*.py"))
    paths += list((root / "literature_service/config").glob("*"))
    paths += [p for p in (root / "literature_service/scripts").glob("*.py") if "evidence_v8" in p.name]
    paths += [root / "literature_service/requirements-evidence-v8.txt"]
    buf, manifest = io.BytesIO(), {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(set(paths)):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            name, blob = p.relative_to(root / "literature_service").as_posix(), p.read_bytes()
            manifest[name] = hashlib.sha256(blob).hexdigest()
            z.writestr(name, blob)
        z.writestr("source-manifest.json", json.dumps(manifest, sort_keys=True))
    return buf.getvalue()


def deploy_sources(host):
    code = r'''
import sys,io,zipfile,json,hashlib,os
from pathlib import Path
b=Path(PROJECT);blob=sys.stdin.buffer.read();digest=hashlib.sha256(blob).hexdigest();root=b/'sources'/digest[:20]
root.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(io.BytesIO(blob)) as z:
 for name in z.namelist():
  p=(root/name).resolve()
  if not p.is_relative_to(root.resolve()):raise ValueError('Archive path escape')
  value=z.read(name);p.parent.mkdir(parents=True,exist_ok=True)
  if p.exists() and p.read_bytes()!=value:raise ValueError('Immutable source conflict')
  if not p.exists():p.write_bytes(value)
manifest=json.loads((root/'source-manifest.json').read_text())
for name,value in manifest.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=value:raise ValueError('Source hash mismatch')
pointer=b/'source-current';temp=b/('source-next-'+str(os.getpid()))
temp.symlink_to(root,target_is_directory=True);temp.replace(pointer)
print(json.dumps({'source_sha256':digest,'source_directory':str(root),'files':len(manifest),'other_experiments_modified':False}))
'''
    return json.loads(remote(host, "PROJECT=" + repr(PROJECT) + "\n" + code, package_sources()))


def init_environment(host):
    """Create a Python 3.12 venv; pip continues with its own owned install receipt."""
    interpreter = {
        "100.84.183.6": "/home/keunsoo/projects/nutriomics-literature-v7-development/.venv/bin/python",
        "100.93.148.94": "/home/keunsoo/projects/nutriomics-relation-v6-gpu-r2/.venv/bin/python",
        "100.68.188.102": "/home/keunsoo/.local/share/uv/python/cpython-3.12.13-linux-x86_64-gnu/bin/python3.12",
    }[host]
    code = r'''
import subprocess,os,json,time,shutil
from pathlib import Path
b=Path(PROJECT);b.mkdir(parents=True,exist_ok=True);h=b/'runtime';h.mkdir(exist_ok=True)
g=subprocess.check_output(['nvidia-smi','--query-gpu=uuid,name,memory.total','--format=csv,noheader,nounits'],text=True).strip()
if any(t in g.upper() for t in ['H200','A6000']):raise ValueError('Excluded GPU model')
if shutil.disk_usage(b).free<50*2**30:raise ValueError('At least 50GiB disk required')
version=subprocess.check_output([PYTHON,'-c','import sys;print("%d.%d"%sys.version_info[:2])'],text=True).strip()
if version!='3.12':raise ValueError('Python3.12 required')
env=b/'.venv'
if not (env/'bin/python').exists():subprocess.run([PYTHON,'-m','venv','--copies',str(env)],check=True)
state=h/'environment-install.json'
if state.exists():
 old=json.loads(state.read_text())
 try:
  os.kill(old['pid'],0)
 except ProcessLookupError:pass
 else:
  print(json.dumps({'status':'existing-installer','pid':old['pid'],'gpu':g}));raise SystemExit
req=b/'source-current/requirements-evidence-v8.txt'
if not req.exists():raise FileNotFoundError('Deploy source requirements before environment install')
runner="""import sys,subprocess,json,time,traceback\nfrom pathlib import Path\nh=Path(sys.argv[1]);req=Path(sys.argv[2]);p=h/'environment-install.json'\ntry:\n subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--index-url','https://download.pytorch.org/whl/cu128','torch==2.10.0'],check=True)\n subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','-r',str(req)],check=True)\n import torch,transformers\n if not torch.cuda.is_available():raise RuntimeError('CUDA runtime did not become available')\n v=json.loads(p.read_text());v.update(status='ready',finished_at=time.time(),torch=torch.__version__,transformers=transformers.__version__,cuda=torch.version.cuda)\nexcept BaseException as exc:\n traceback.print_exc();v=json.loads(p.read_text());v.update(status='failed',finished_at=time.time(),error=type(exc).__name__+': '+str(exc))\nq=p.with_suffix('.complete-part');q.write_text(json.dumps(v));q.replace(p)\n"""
installer=h/'install_environment.py';installer.write_text(runner)
log=(h/'environment-install.log').open('ab')
p=subprocess.Popen([str(env/'bin/python'),'-u','-B',str(installer),str(h),str(req.resolve())],stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env={**os.environ,'PIP_NO_INPUT':'1'})
receipt={'status':'installing','pid':p.pid,'started_at':time.time(),'gpu':g,'requirements':str(req.resolve()),'old_environment_modified':False}
tmp=state.with_suffix('.part');tmp.write_text(json.dumps(receipt));tmp.replace(state)
print(json.dumps(receipt))
'''
    return json.loads(remote(host, "PROJECT=" + repr(PROJECT) + "\nPYTHON=" + repr(interpreter) + "\n" + code))


def download_assets(target):
    """Import permitted training labels and source-only development inputs, not held gold."""
    code = r'''
import sys,zipfile,json,hashlib
from pathlib import Path
h=Path(ORIGINAL)
files=[]
for name in ['data','prepared']:
 files.extend((h/name).rglob('*.json'))
for name in ['aliases.sqlite3','audit.json','bootstrap.json','transfer-contract.json','identity/ner-oof.json','identity/ner-ready.json','identity/ner-full/model.safetensors','identity/ner-full/complete.json']:
 p=h/name
 if p.is_file():files.append(p)
for group in ['knowledge/*.json','knowledge/*.sqlite3','knowledge/base-source/*']:
 files.extend(p for p in h.glob(group) if p.is_file())
names=[p.relative_to(h).as_posix() for p in files]
if any('gold' in n.lower() for n in names):raise ValueError('Unexpected gold-named input')
development=json.loads((h/'data/development-sources.json').read_text())['documents']
if any('entities' in d or 'relations' in d for d in development):raise ValueError('Development labels must remain local')
manifest={}
with zipfile.ZipFile(sys.stdout.buffer,'w',zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as z:
 for p in sorted(set(files)):
  name=p.relative_to(h).as_posix()
  with p.open('rb') as f:manifest[name]=hashlib.file_digest(f,'sha256').hexdigest()
  z.write(p,name)
 z.writestr('asset-manifest.json',json.dumps({'files':manifest,'protected_labels_exported':False,'development_source_only':True},sort_keys=True))
'''
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".zip.part")
    with part.open("wb") as out:
        p = subprocess.run([*ssh("100.93.148.94"), "python3 -c " + shlex.quote("ORIGINAL=" + repr(ORIGINAL) + "\n" + code)],
                           stdout=out, stderr=subprocess.PIPE, timeout=900)
    if p.returncode:
        raise RuntimeError("Asset export failed: " + p.stderr.decode(errors="replace")[-800:])
    with zipfile.ZipFile(part) as z:
        manifest = json.loads(z.read("asset-manifest.json"))
        for name, digest in manifest["files"].items():
            with z.open(name) as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != digest:
                    raise ValueError("Asset hash mismatch: " + name)
    os.replace(part, target)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"archive": str(target), "sha256": digest, "files": len(manifest["files"])}


def upload_assets(host, path):
    """Hash each 16MiB block so a transient SSH reset does not discard gigabytes."""
    path = Path(path)
    with path.open("rb") as f:
        archive_sha = hashlib.file_digest(f, "sha256").hexdigest()
    check = r'''
import json,hashlib
from pathlib import Path
d=Path(PROJECT)/'assets/import-chunks'/ARCHIVE_SHA;d.mkdir(parents=True,exist_ok=True)
have={}
for p in d.glob('*.chunk'):
 with p.open('rb') as f:have[int(p.stem)]=hashlib.file_digest(f,'sha256').hexdigest()
print(json.dumps(have))
'''
    prefix = "PROJECT=" + repr(PROJECT) + "\nARCHIVE_SHA=" + repr(archive_sha) + "\n"
    have = json.loads(remote(host, prefix + check, timeout=120))
    chunks = []
    with path.open("rb") as stream:
        index = 0
        while True:
            block = stream.read(16 * 1024 ** 2)
            if not block:
                break
            digest = hashlib.sha256(block).hexdigest()
            chunks.append({"index": index, "sha256": digest, "size": len(block)})
            if have.get(str(index)) != digest:
                code = r'''
import sys,hashlib,json
from pathlib import Path
blob=sys.stdin.buffer.read()
if len(blob)!=SIZE or hashlib.sha256(blob).hexdigest()!=DIGEST:raise ValueError('Chunk integrity failure')
p=Path(PROJECT)/'assets/import-chunks'/ARCHIVE_SHA/('%06d.chunk'%INDEX)
q=p.with_suffix('.part');q.write_bytes(blob);q.replace(p);print(json.dumps({'index':INDEX,'verified':True}))
'''
                variables = prefix + "INDEX=" + str(index) + "\nDIGEST=" + repr(digest) + "\nSIZE=" + str(len(block)) + "\n"
                for attempt in range(3):
                    try:
                        remote(host, variables + code, block, timeout=180)
                        break
                    except (RuntimeError, subprocess.TimeoutExpired):
                        if attempt == 2:
                            raise
                        time.sleep(2 ** attempt)
            index += 1
            if index % 8 == 0:
                print(json.dumps({"upload_verified_MiB": round(stream.tell() / 1024 ** 2, 1),
                                  "total_MiB": round(path.stat().st_size / 1024 ** 2, 1)}), flush=True)
    code = r'''
import sys,zipfile,json,hashlib,shutil,os
from pathlib import Path
b=Path(PROJECT);h=b/'assets/r2-snapshot';h.mkdir(parents=True,exist_ok=True)
archive=b/'assets/r2-import.zip.part'
parts=b/'assets/import-chunks'/ARCHIVE_SHA
with archive.open('wb') as out:
 for chunk in CHUNKS:
  p=parts/('%06d.chunk'%chunk['index'])
  with p.open('rb') as f:
   if hashlib.file_digest(f,'sha256').hexdigest()!=chunk['sha256']:raise ValueError('Stored chunk changed')
  with p.open('rb') as f:shutil.copyfileobj(f,out,4*1024**2)
with archive.open('rb') as f:
 if hashlib.file_digest(f,'sha256').hexdigest()!=ARCHIVE_SHA:raise ValueError('Assembled archive changed')
with zipfile.ZipFile(archive) as z:
 manifest=json.loads(z.read('asset-manifest.json'))
 if manifest.get('protected_labels_exported') is not False or manifest.get('development_source_only') is not True:raise ValueError('Asset supervision contract absent')
 for name,digest in manifest['files'].items():
  p=(h/name).resolve()
  if not p.is_relative_to(h.resolve()):raise ValueError('Asset path escape')
  p.parent.mkdir(parents=True,exist_ok=True)
  if p.exists():
   with p.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
   if actual!=digest:raise ValueError('Immutable asset conflict: '+name)
   continue
  temporary=p.with_name(p.name+'.transfer-part')
  with z.open(name) as src,temporary.open('wb') as dst:shutil.copyfileobj(src,dst,4*1024**2)
  with temporary.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
  if actual!=digest:raise ValueError('Asset transfer hash mismatch: '+name)
  temporary.chmod(0o600);temporary.replace(p)
(h/'asset-manifest.json').write_text(json.dumps(manifest,sort_keys=True));archive.replace(archive.with_suffix(''))
print(json.dumps({'asset_directory':str(h),'files':len(manifest['files']),'protected_labels_exported':False,'archive_sha256':ARCHIVE_SHA}))
'''
    return json.loads(remote(host, prefix + "CHUNKS=" + repr(chunks) + "\n" + code, timeout=900))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("source", "init", "download-assets", "upload-assets"))
    p.add_argument("--host", choices=ALLOWED_HOSTS, default="100.84.183.6")
    p.add_argument("--archive", type=Path, default=ROOT / "literature_service/runtime/v8-deployment/r2-permitted-assets.zip")
    a = p.parse_args()
    if a.command == "source":
        result = deploy_sources(a.host)
    elif a.command == "init":
        result = init_environment(a.host)
    elif a.command == "download-assets":
        result = download_assets(a.archive)
    else:
        result = upload_assets(a.host, a.archive)
    home = ROOT / "literature_service/runtime/v8-deployment"
    home.mkdir(parents=True, exist_ok=True)
    (home / (a.command + "-" + a.host + ".json")).write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
