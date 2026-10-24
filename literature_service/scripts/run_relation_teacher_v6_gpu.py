"""Durable local worker; never prints authentication material."""
import os,sys,time,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v6_gpu.common import HOME,atomic,lock
from nutri_literature.repair_v6_gpu.teacher import run
if __name__=='__main__':
 import psutil
 psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
 value={'pid':os.getpid(),'started':time.time(),'running':True};atomic(HOME/'teacher/worker.json',value)
 try:
  with lock(HOME/'teacher/worker-lock'):run()
  value['exit']='completed'
 except Exception as exc:
  value['exit']='failed';value['error_class']=type(exc).__name__
 finally:value['running']=False;value['finished']=time.time();atomic(HOME/'teacher/worker.json',value)
