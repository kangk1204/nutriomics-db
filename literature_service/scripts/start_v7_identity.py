"""Owned ID worker: preparation may run alongside one relation fit; fits serialize."""
import json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def worker():
    os.chdir(ROOT)
    home=ROOT/'runtime'
    state=home/'v7/identity-state.json'
    data=home/'v6/training-data/audited-complete-v6-e00e690ae91d2648'
    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[key]='1'
    os.environ['LITERATURE_PROCESSING_LOCK_HOME']='/home/keunsoo/projects/nutriomics-literature-v6-development/runtime/locks'
    os.environ['LITERATURE_NLP_LOCK']=os.environ['LITERATURE_PROCESSING_LOCK_HOME']+'/nlp-cpu.lock'
    stages=[('prepare',['prepare-identity','--data',str(data/'relation-train.json'),'--audit',str(data/'audit.json'),
              '--cache',str(home/'v6/refinement-r4/automatic-features'),'--models',str(home/'v6/models'),'--out',str(home/'v7/identity-features')]),
            ('fit',['train-identity','--features',str(home/'v7/identity-features'),'--out',str(home/'v7/identity-head')])]
    for stage,args in stages:
        state.write_text(json.dumps({'stage':stage,'status':'running','pid':os.getpid(),'at':time.time(),'scientific_targets_met':False}))
        code=subprocess.call([sys.executable,'-m','nutri_literature.cli_v7','--home',str(home),*args],cwd=ROOT)
        if code:
            state.write_text(json.dumps({'stage':stage,'status':'failed','returncode':code,'at':time.time()}))
            return code
    state.write_text(json.dumps({'stage':'identity','status':'complete','at':time.time(),'scientific_targets_met':False}))
    return 0


if __name__=='__main__':
    if '--worker' in sys.argv:
        raise SystemExit(worker())
    import psutil
    state=ROOT/'runtime/v7/identity-state.json'
    if state.exists():
        previous=json.loads(state.read_bytes())
        pid=previous.get('pid')
        if previous.get('status')=='running' and psutil.pid_exists(pid) and str(ROOT) in ' '.join(psutil.Process(pid).cmdline()):
            raise SystemExit('Owned identity worker already running')
    log=(ROOT/'runtime/v7/identity.log').open('ab')
    proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    print(json.dumps({'pid':proc.pid}))
