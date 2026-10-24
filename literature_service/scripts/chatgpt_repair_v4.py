import os
import sys
from pathlib import Path
for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[name]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
try:
    import psutil
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
except (ImportError,AttributeError,PermissionError):
    pass
from nutri_literature.repair_v4.runner import main
if __name__=='__main__': main()
