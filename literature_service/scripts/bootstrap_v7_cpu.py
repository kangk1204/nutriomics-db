"""Owned, isolated CPU dependency bootstrap on an authorized alternate host."""
import hashlib,io,json,os,subprocess,sys,tarfile,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent
if ROOT.name=='scripts':ROOT=ROOT.parent
STATE=ROOT/'bootstrap-cpu-state.json'


def fetch(url):
    request=urllib.request.Request(url,headers={'User-Agent':'nutriomics-literature-v7'})
    with urllib.request.urlopen(request,timeout=180) as response:return response.read()


def save(stage,status='running',**extra):
    value={'stage':stage,'status':status,'pid':os.getpid(),'at':time.time(),**extra}
    temporary=STATE.with_suffix('.tmp');temporary.write_text(json.dumps(value));temporary.replace(STATE)
    print(json.dumps(value),flush=True)


def worker():
    os.chdir(ROOT)
    os.environ.update(UV_PYTHON_INSTALL_DIR=str(ROOT/'.python'),UV_CACHE_DIR=str(ROOT/'.uv-cache'),CUDA_VISIBLE_DEVICES='')
    os.environ.update(OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',
                      CMAKE_BUILD_PARALLEL_LEVEL='1',MAX_JOBS='1',UV_CONCURRENT_BUILDS='1',MAKEFLAGS='-j1')
    tools=ROOT/'.tools';tools.mkdir(exist_ok=True);uv=tools/'uv'
    try:
        if not uv.exists():
            save('uv-download')
            release=json.loads(fetch('https://api.github.com/repos/astral-sh/uv/releases/latest'))
            name='uv-x86_64-unknown-linux-gnu.tar.gz'
            assets={a['name']:a['browser_download_url'] for a in release['assets']}
            raw=fetch(assets[name]);expected=fetch(assets[name+'.sha256']).decode().split()[0]
            if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Official uv checksum mismatch')
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                member=next(m for m in archive if Path(m.name).name=='uv' and m.isfile())
                uv.write_bytes(archive.extractfile(member).read());uv.chmod(0o755)
            (tools/'uv-source.json').write_text(json.dumps({'tag':release['tag_name'],'url':assets[name],'sha256':expected}))
        save('python-3.12.14')
        subprocess.run([str(uv),'python','install','3.12.14'],check=True)
        interpreter=next((ROOT/'.python').glob('cpython-3.12.14-*/bin/python3.12'))
        if not (ROOT/'.venv/bin/python').exists():
            subprocess.run([str(uv),'venv','--python',str(interpreter),str(ROOT/'.venv')],check=True)
        save('pinned-cpu-dependencies')
        # Give only torch its official CPU index. Other pinned dependencies use
        # PyPI; broadening index precedence would weaken package provenance.
        import html,re
        listing=fetch('https://download.pytorch.org/whl/cpu/torch/').decode()
        links=[html.unescape(u) for u in re.findall(r'href="([^"]+)"',listing)]
        wheel=next(u for u in links if 'torch-2.6.0%2Bcpu-cp312-cp312-linux_x86_64.whl#sha256=' in u)
        if not wheel.startswith('https://download.pytorch.org/'):raise ValueError('Unexpected CPU wheel origin')
        install=ROOT/'requirements-v7-install.lock'
        lines=[line for line in (ROOT/'requirements-v6.lock').read_text().splitlines() if not line.startswith('--extra-index-url')]
        lines=['torch @ '+wheel if line=='torch==2.6.0+cpu' else line for line in lines]
        install.write_text('\n'.join(lines)+'\n')
        subprocess.run([str(uv),'pip','install','--python',str(ROOT/'.venv/bin/python'),'--index-url','https://pypi.org/simple','-r',str(install)],check=True)
        check="import torch,sys;assert torch.version.cuda is None;print(sys.version);print(torch.__version__)"
        subprocess.run([str(ROOT/'.venv/bin/python'),'-c',check],check=True)
        freeze=subprocess.check_output([str(uv),'pip','freeze','--python',str(ROOT/'.venv/bin/python')])
        (ROOT/'requirements-v7-observed.lock').write_bytes(freeze)
        save('environment','complete',observed_sha256=hashlib.sha256(freeze).hexdigest(),cpu_only=True)
    except Exception as error:
        save('bootstrap','failed',error=str(error));raise


if __name__=='__main__':
    if '--worker' in sys.argv:worker();raise SystemExit(0)
    if STATE.exists():
        previous=json.loads(STATE.read_bytes())
        if previous.get('status')=='running':
            try:os.kill(previous['pid'],0)
            except ProcessLookupError:pass
            else:raise SystemExit('Owned bootstrap is already running')
    log=(ROOT/'bootstrap-cpu.log').open('ab')
    proc=subprocess.Popen([sys.executable,__file__,'--worker'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    print(json.dumps({'pid':proc.pid}))
