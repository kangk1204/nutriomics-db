"""Audited GPU experiment CLI; no production API side effects."""
import os,sys,argparse
from pathlib import Path
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[key]='1'
os.environ['TOKENIZERS_PARALLELISM']='false'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v6_gpu_r1.common import HOME,atomic,log,lock

def main():
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['audit','probe','resume','status','build-kb','train-id','train-relation','calibrate','freeze','predict','evaluate','compare']);args=p.parse_args()
 if args.stage=='status':
  from nutri_literature.repair_v6_gpu_r1.runner import status
  print(status());return
 with lock():
  try:os.nice(10)
  except (AttributeError,PermissionError):pass
  if args.stage=='audit':
   from nutri_literature.repair_v6_gpu_r1.datasets import audit
   print(audit())
  elif args.stage=='probe':
   from nutri_literature.repair_v6_gpu_r1.engine import probe
   probe()
  else:
   from nutri_literature.repair_v6_gpu_r1.runner import main as execute
   print(execute(args.stage))

if __name__=='__main__':main()
