import os,sys,argparse
from pathlib import Path
for variable in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[variable]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psutil
try:psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
except (AttributeError,PermissionError):pass
from nutri_literature.repair_v5.common import *

def main():
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['index','prepare','infer-train','train','freeze','infer-development','predict','evaluate','resume','status']);a=parser.parse_args()
    if a.stage=='status':
        state=read(HOME/'run-state.json') if (HOME/'run-state.json').exists() else {}
        alive=False
        try:alive=any('chatgpt_repair_v5.py' in c for c in psutil.Process(state['pid']).cmdline())
        except (psutil.Error,KeyError):pass
        log('status',budget=budget_status(),controller=state,alive=alive,
            index_complete=(HOME/'knowledge/index.json').exists(),preparation_complete=(HOME/'prepared.json').exists(),
            train_valid=len(list((HOME/'inference/train').glob('*/valid/*.json'))),
            dev_valid=len(list((HOME/'inference/development').glob('*/valid/*.json'))),
            frozen=(HOME/'freeze.json').exists(),complete=(HOME/'comparison.json').exists());return
    from nutri_literature.chatgpt_batch import batch_lock
    from datetime import datetime,timezone
    HOME.mkdir(parents=True,exist_ok=True)
    with batch_lock(HOME):
        state={'pid':os.getpid(),'stage':a.stage,'running':True,'started':datetime.now(timezone.utc).isoformat()}
        def save():(HOME/'run-state.json').write_text(canonical(state),encoding='utf8')
        save()
        try:
            if a.stage=='index':
                from nutri_literature.repair_v5.retrieval import build_index
                build_index()
            else:
                from nutri_literature.repair_v5 import pipeline
                getattr(pipeline,a.stage.replace('-','_'))()
            state['exit']='completed'
        except Exception as e:state.update(exit='stopped',error_class=type(e).__name__);raise
        finally:state.update(running=False,finished=datetime.now(timezone.utc).isoformat());save()

if __name__=='__main__':main()
