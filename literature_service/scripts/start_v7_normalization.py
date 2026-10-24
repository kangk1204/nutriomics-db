"""Resume the omitted official training data, then fit a separately versioned ID model."""
import json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def stage(name):
    from nutri_literature.settings import Settings
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.operations_v7 import resource_check
    home=ROOT/'runtime';resource_check(home)
    with processing_slot(Settings(home,ROOT/'config/topics.json')),memory_budget():
        from nutri_literature.normalization_v7 import sources,automatic
        result=(sources if name=='sources' else automatic)(home)
        print(json.dumps({'stage':name,'complete':True,'articles':len(result.get('files',result.get('training_pmids',[])))}),flush=True)


def worker():
    home=ROOT/'runtime';state=home/'v7/normalization-r3-state.json';root=home/'v7/normalization-r3'
    stages=[('sources',[sys.executable,__file__,'--stage','sources']),
            ('automatic',[sys.executable,__file__,'--stage','automatic']),
            ('prepare',[sys.executable,'-m','nutri_literature.cli_v7','--home',str(home),'prepare-identity',
               '--data',str(root/'audited/relation-train.json'),'--audit',str(root/'audited/audit.json'),
               '--cache',str(root/'automatic'),'--models',str(home/'v7/frozen-v6-upstream'),'--out',str(root/'features')]),
            ('fit',[sys.executable,'-m','nutri_literature.cli_v7','--home',str(home),'train-identity',
               '--features',str(root/'features'),'--out',str(root/'head')])]
    for name,args in stages:
        state.write_text(json.dumps({'status':'running','stage':name,'pid':os.getpid(),'at':time.time()}))
        code=subprocess.call(args,cwd=ROOT)
        if code:
            state.write_text(json.dumps({'status':'failed','stage':name,'returncode':code,'at':time.time()}));return code
    state.write_text(json.dumps({'status':'complete','at':time.time(),'scientific_targets_met':False}));return 0


if __name__=='__main__':
    os.chdir(ROOT)
    for k in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[k]='1'
    os.environ['LITERATURE_PROCESSING_LOCK_HOME']='/home/keunsoo/projects/nutriomics-literature-v6-development/runtime/locks'
    os.environ['LITERATURE_NLP_LOCK']=os.environ['LITERATURE_PROCESSING_LOCK_HOME']+'/nlp-cpu.lock'
    if os.name=='posix':os.nice(10)
    if '--stage' in sys.argv:stage(sys.argv[sys.argv.index('--stage')+1]);raise SystemExit(0)
    if '--worker' in sys.argv:raise SystemExit(worker())
    import psutil
    state=ROOT/'runtime/v7/normalization-r3-state.json'
    if state.exists():
        value=json.loads(state.read_bytes());pid=value.get('pid')
        if value.get('status')=='running' and pid and psutil.pid_exists(pid) and str(ROOT) in ' '.join(psutil.Process(pid).cmdline()):
            raise SystemExit('Owned normalization worker is already active')
    log=(ROOT/'runtime/v7/normalization-r3.log').open('ab')
    proc=subprocess.Popen([sys.executable,__file__,'--worker'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    print(json.dumps({'pid':proc.pid}))
