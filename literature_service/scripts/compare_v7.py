from pathlib import Path
import argparse,json,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from nutri_literature.comparison_v7 import compare
if __name__=='__main__':
    p=argparse.ArgumentParser()
    for key in ('baseline','current','out'):p.add_argument('--'+key,type=Path,required=True)
    args=p.parse_args()
    print(json.dumps(compare(args.baseline,args.current,args.out),ensure_ascii=False))
