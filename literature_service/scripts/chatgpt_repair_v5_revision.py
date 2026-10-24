import os,sys
from pathlib import Path
for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[k]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psutil
try:psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
except (AttributeError,PermissionError):pass
from nutri_literature import repair_v5_revision as r
from nutri_literature.chatgpt_batch import batch_lock
from datetime import datetime,timezone
if __name__=='__main__':
    r.HOME.mkdir(parents=True,exist_ok=True)
    with batch_lock(r.HOME):
        state={'pid':os.getpid(),'running':True,'started':datetime.now(timezone.utc).isoformat()}
        def save():(r.HOME/'run-state.json').write_text(r.canonical(state),encoding='utf8')
        save()
        try:r.resume();state['exit']='completed'
        except Exception as e:state.update(exit='stopped',error_class=type(e).__name__);raise
        finally:state.update(running=False,finished=datetime.now(timezone.utc).isoformat());save()
