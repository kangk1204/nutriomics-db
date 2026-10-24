"""Prepare fresh official holdouts and audit source identity without prediction."""
import argparse
from pathlib import Path
from nutri_literature.corpora_v4 import prepare
from nutri_literature.db import dumps
from nutri_literature.settings import Settings

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--sources",type=Path,required=True)
parser.add_argument("--output",type=Path,required=True)
parser.add_argument("--clinical-manifest",type=Path,required=True)
parser.add_argument("--lexicon",type=Path,required=True)
args=parser.parse_args()
settings=Settings.from_env()
result=prepare(args.sources,args.output,args.lexicon,args.clinical_manifest,settings.home/"public_benchmarks")
print(dumps({"audit":str(args.output/"audit.json"),"completed":result["completed"],
             "specs":[{k:s[k] for k in ("corpus","papers","training_overlap_articles","pmid_mapping_missing")} for s in result["specs"]]}),flush=True)
