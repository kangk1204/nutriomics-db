"""V4 implementation CLI. Separate from the frozen legacy evaluator commands."""
import argparse
import os
from pathlib import Path
import time
from .db import connect, dumps, initialize, now
from .settings import Settings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home",type=Path)
    parser.add_argument("--models",type=Path)
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("build-gene-dictionary")
    train=sub.add_parser("train")
    train.add_argument("--sources",type=Path,required=True)
    train.add_argument("--benchmark-inputs",type=Path)
    dev=sub.add_parser("evaluate-development")
    dev.add_argument("--corpus",choices=["bioredirect","bioscope","ebmnlp","evidenceoutcomes"],required=True)
    dev.add_argument("--lexicon",type=Path,required=True)
    dev.add_argument("--provider",choices=["biomedbert","bionlp13cg"],default="biomedbert")
    dev.add_argument("--mode",choices=["end_to_end","gold_entities"],default="end_to_end")
    dev.add_argument("--profile",choices=["local","pubtator_assisted"],default="local")
    dev.add_argument("--limit",type=int,default=200)
    for name in ("process","worker","reprocess"):
        item=sub.add_parser(name)
        item.add_argument("--limit",type=int,default=10)
    sub.add_parser("observe")
    sub.add_parser("status")
    sub.add_parser("compare")
    freeze=sub.add_parser("freeze")
    freeze.add_argument("--audit",type=Path,required=True)
    freeze.add_argument("--out",type=Path,required=True)
    final=sub.add_parser("final-evaluate")
    final.add_argument("--spec",type=Path,required=True)
    final.add_argument("--freeze",type=Path,required=True)
    args=parser.parse_args()
    settings=Settings.from_env()
    if args.home:
        settings=Settings(args.home,settings.config)
    if args.models:
        os.environ["LITERATURE_V4_MODEL_HOME"]=str(args.models)
    os.environ.setdefault("LITERATURE_NLP_LOCK",str(settings.home/"locks"/"nlp-cpu.lock"))
    for variable in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS","NUMEXPR_NUM_THREADS"):
        os.environ[variable]="1"
    os.environ["TOKENIZERS_PARALLELISM"]="false"
    if hasattr(os,"nice"):
        os.nice(10)
    initialize(settings)
    from . import operations_v4 as operations
    if args.command=="build-gene-dictionary":
        from .candidates_v4 import build_gene_snapshot
        home=operations.model_home(settings)
        path=build_gene_snapshot(home/"genes")
        (home/"gene-snapshot.json").write_text(dumps({"filename":path.name}),encoding="utf-8")
        result={"snapshot":str(path)}
    elif args.command=="train":
        from .training_v4 import train
        manifest=train(args.sources,operations.model_home(settings)/"clinical",args.benchmark_inputs)
        result={"files":manifest["files"],"elapsed_seconds":manifest["elapsed_seconds"],
                "training_articles":{k:len(v) for k,v in manifest["training_pmids"].items()}}
    elif args.command=="evaluate-development":
        if args.limit<1:
            parser.error("limit must be positive")
        from .evaluation_v4 import development
        report=development(settings,args.corpus,args.lexicon,args.provider,args.mode,args.limit,args.profile)
        result={k:report[k] for k in ("output_path","metrics","failure_counts","papers","provider","mode")}
    elif args.command=="observe":
        result=operations.observe(settings)
    elif args.command=="status":
        result=operations.operation_status(settings)
    elif args.command=="compare":
        from .evaluation_v4 import reports,select_provider
        values=reports(settings)["items"]
        result={"selection":select_provider(values),"reports":values}
    elif args.command=="freeze":
        from .evaluation_v4 import freeze
        result=freeze(settings,args.audit,args.out)
    elif args.command=="final-evaluate":
        from .evaluation_v4 import final_evaluate
        result=final_evaluate(settings,args.spec,args.freeze)
    else:
        from .dictionaries import load_dictionaries
        from .locking import lock
        with lock(settings.home/"locks"/"worker-v4.lock"):
            values=operations.components(settings)
            dictionaries=load_dictionaries(settings,"heuristic-v2")
            if args.command=="reprocess":
                result=operations.register(settings,values,dictionaries,queue_all=True)
            else:
                with connect(settings) as con:
                    con.execute("UPDATE engine_queue SET status='pending',due_at=?,error='v4 exclusive worker restarted' "
                                "WHERE engine_version='heuristic-v4' AND status='running'",(now(),))
                    con.execute("UPDATE runs SET status='interrupted',finished_at=?,error='v4 exclusive worker restarted' "
                                "WHERE kind='process_extract_heuristic-v4' AND status='running'",(now(),))
                while True:
                    result=operations.process(settings,args.limit,values,dictionaries)
                    operations.observe(settings)
                    if args.command!="worker":
                        break
                    if result["processed"] or result["failed"]:
                        print(dumps(result),flush=True)
                    time.sleep(15 if result["processed"] else 300)
    print(dumps(result),flush=True)


if __name__=="__main__":
    main()
