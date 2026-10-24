"""Single owned GPU pipeline with explicit, restartable stage receipts."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import os,time,traceback,shutil
from nutri_literature.repair_v6_gpu_r2.common import HOME,POLICY,atomic,lock,read


def main():
    import psutil
    if psutil.virtual_memory().available<16*1024**3 or shutil.disk_usage(HOME.parent if HOME.parent.exists() else Path.cwd()).free<50*1024**3:
        raise RuntimeError('At least 16 GiB RAM and 50 GiB disk are required')
    state={'pid':os.getpid(),'process_created':psutil.Process().create_time(),'running':True,'started':time.time(),'engine':POLICY['engine']}
    def stage(name):state.update(stage=name,updated=time.time());atomic(HOME/'job-state.json',state)
    try:
        with lock():
            try:os.nice(10)
            except PermissionError:pass
            from bootstrap_relation_v6_gpu_r2 import main as prepare
            stage('prepare-contextual-candidates');prepare()
            from nutri_literature.repair_v6_gpu_r2.runner import train_id,train_relation,freeze,predict
            for name,run in [('train-id',train_id),('train-relation',train_relation),('freeze',freeze),('predict-100',predict)]:stage(name);run()
            state.update(running=False,exit=0,stage='predictions-ready',finished=time.time());atomic(HOME/'job-state.json',state)
    except BaseException as exc:
        state.update(running=False,exit=type(exc).__name__,error=str(exc),finished=time.time());atomic(HOME/'job-state.json',state);traceback.print_exc();raise


if __name__=='__main__':main()
