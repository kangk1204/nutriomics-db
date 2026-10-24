"""Persistent, resumable source-pair -> CPU smoke -> nested joint worker."""
import json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def worker():
    os.chdir(ROOT);home=ROOT/'runtime';state=home/'v7/joint-state.json'
    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[key]='1'
    os.environ['LITERATURE_PROCESSING_LOCK_HOME']='/home/keunsoo/projects/nutriomics-literature-v6-development/runtime/locks'
    os.environ['LITERATURE_NLP_LOCK']=os.environ['LITERATURE_PROCESSING_LOCK_HOME']+'/nlp-cpu.lock'
    def save(value):
        temp=state.with_suffix('.tmp');temp.write_text(json.dumps(value,sort_keys=True));temp.replace(state)
    for relative in ('v7/separated-features/manifest.json','v7/auxiliary-train/manifest.json','v7/base-biomedbert/v7-weight-manifest.json'):
        deadline=time.monotonic()+86400
        while not (home/relative).exists():
            save({'status':'waiting','stage':'inputs','missing':relative,'pid':os.getpid(),'at':time.time()})
            if time.monotonic()>deadline:raise TimeoutError('Audited joint input wait exceeded 24 h')
            time.sleep(15)
    evaluation=lambda model,mode,label:['evaluate-development','--model',str(home/'v7'/model),
        '--predictions',str(home/'v6/refinement-r4'/('relations-'+mode)),'--models',str(home/'v7/frozen-v6-upstream'),
        '--mode',mode,'--out',str(home/'v7'/label/mode)]
    stages=[('separated-auto',evaluation('separated-head','end_to_end','separated-development')),
            ('separated-oracle',evaluation('separated-head','gold_entities','separated-development')),
            ('prepare',['prepare-joint','--features',str(home/'v7/separated-features'),'--auxiliary',str(home/'v7/auxiliary-train'),'--out',str(home/'v7/joint-features')]),
            ('smoke',['smoke-joint','--features',str(home/'v7/joint-features'),'--base',str(home/'v7/base-biomedbert'),'--out',str(home/'v7/joint-smoke'),'--steps','16']),
            ('nested',['train-joint','--features',str(home/'v7/joint-features'),'--bc2gm',str(home/'v6/models/gene/selected_model'),
                       '--generic',str(home/'v7/base-biomedbert'),'--out',str(home/'v7/joint-head')]),
            ('joint-auto',evaluation('joint-head','end_to_end','joint-development')),
            ('joint-oracle',evaluation('joint-head','gold_entities','joint-development')),
            ('joint-id-auto',evaluation('joint-head','end_to_end','joint-id-development')+[
                '--identity',str(home/'v7/normalization-r3/head')])]
    try:
        for stage,args in stages:
            save({'status':'running','stage':stage,'pid':os.getpid(),'at':time.time(),'scientific_targets_met':False})
            from nutri_literature.contract_v7 import sha
            if stage=='prepare' and (home/'v7/joint-features/manifest.json').exists():
                manifest=json.loads((home/'v7/joint-features/manifest.json').read_bytes())
                if (manifest['source_manifests']!={'bioredirect':sha((home/'v7/separated-features/manifest.json').read_bytes()),
                       'auxiliary':sha((home/'v7/auxiliary-train/manifest.json').read_bytes())}
                    or manifest['pair_source_sha256']!=sha((ROOT/'nutri_literature/joint_data_v7.py').read_bytes())):
                    raise ValueError('Prepared joint contract differs')
                print(json.dumps({'stage':stage,'reused_verified_manifest':True}),flush=True)
                continue
            if stage=='smoke' and (home/'v7/joint-smoke/manifest.json').exists():
                manifest=json.loads((home/'v7/joint-smoke/manifest.json').read_bytes())
                if (not manifest.get('smoke_only') or not manifest.get('adapted')
                    or manifest['source_sha256']!=sha((ROOT/'nutri_literature/joint_training_v7.py').read_bytes())
                    or manifest['weights_sha256']!=sha((home/'v7/joint-smoke/weights.pt').read_bytes())):
                    raise ValueError('Saved smoke identity differs')
                print(json.dumps({'stage':stage,'reused_smoke_only':True}),flush=True)
                continue
            code=subprocess.call([sys.executable,'-m','nutri_literature.cli_v7','--home',str(home),*args],cwd=ROOT)
            if code:
                save({'status':'failed','stage':stage,'returncode':code,'at':time.time(),'scientific_targets_met':False});return code
            if stage in ('separated-auto','joint-auto','joint-id-auto'):
                name={'separated-auto':'separated','joint-auto':'joint','joint-id-auto':'joint-id'}[stage]
                from sys import path
                path.insert(0,str(ROOT))
                from nutri_literature.comparison_v7 import compare
                compare(home/'v6/refinement-r4/relations-end_to_end/report.json',home/'v7'/(name+'-development')/'end_to_end/report.json',
                        home/'v7/validation'/(name+'-vs-v6.json'))
            if stage.endswith(('-auto','-oracle')):
                from nutri_literature.diagnostics_v7 import diagnose
                name='separated' if stage.startswith('separated') else 'joint-id' if stage.startswith('joint-id') else 'joint'
                mode='end_to_end' if stage.endswith('auto') else 'gold_entities'
                diagnose(home/'v7'/(name+'-development')/mode/'report.json',home/'v7/validation'/(name+'-'+mode+'-diagnostics.json'))
        save({'status':'complete','stage':'joint-training','at':time.time(),'scientific_targets_met':False,'next':'same-cohort development comparison'})
    except Exception as error:
        save({'status':'failed','stage':'orchestration','error':str(error),'at':time.time()});raise
    return 0


if __name__=='__main__':
    if '--worker' in sys.argv:raise SystemExit(worker())
    import psutil
    state=ROOT/'runtime/v7/joint-state.json'
    if state.exists():
        value=json.loads(state.read_bytes());pid=value.get('pid')
        if value.get('status') in ('waiting','running') and pid and psutil.pid_exists(pid) and str(ROOT) in ' '.join(psutil.Process(pid).cmdline()):
            raise SystemExit('Owned joint worker already active')
    log=(ROOT/'runtime/v7/joint.log').open('ab')
    proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    print(json.dumps({'pid':proc.pid}))
