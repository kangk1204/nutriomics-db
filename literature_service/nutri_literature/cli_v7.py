"""Auditable CPU-only v7 lifecycle; final labels remain gated."""
import argparse
import json
import os
from pathlib import Path
from contextlib import nullcontext


def main():
    for key in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ[key] = '1'
    if os.name == 'posix':
        os.nice(10)
    p = argparse.ArgumentParser(description='heuristic-v7 relation development, candidate beta')
    p.add_argument('--home', type=Path, default=Path('runtime'))
    sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare-separated')
    for key in ('data', 'audit', 'cache', 'out'):
        prep.add_argument('--' + key, type=Path, required=True)
    fit = sub.add_parser('train-baseline')
    fit.add_argument('--features', type=Path, required=True)
    fit.add_argument('--out', type=Path, required=True)
    idprep = sub.add_parser('prepare-identity')
    for key in ('data','audit','cache','models','out'):
        idprep.add_argument('--'+key,type=Path,required=True)
    idfit = sub.add_parser('train-identity')
    for key in ('features','out'):
        idfit.add_argument('--'+key,type=Path,required=True)
    rich=sub.add_parser('prepare-joint')
    for key in ('features','auxiliary','out'):
        rich.add_argument('--'+key,type=Path,required=True)
    joint=sub.add_parser('train-joint')
    for key in ('features','bc2gm','generic','out'):
        joint.add_argument('--'+key,type=Path,required=True)
    smoke=sub.add_parser('smoke-joint')
    for key in ('features','base','out'):
        smoke.add_argument('--'+key,type=Path,required=True)
    smoke.add_argument('--steps',type=int,default=16)
    evaluate = sub.add_parser('evaluate-development')
    for key in ('model', 'predictions', 'models', 'out'):
        evaluate.add_argument('--' + key, type=Path, required=True)
    evaluate.add_argument('--mode', choices=('end_to_end', 'gold_entities'), default='end_to_end')
    evaluate.add_argument('--identity',type=Path)
    for name in ('status', 'observe'):
        sub.add_parser(name)
    freeze=sub.add_parser('freeze')
    for key in ('head','automatic','oracle','regression','audit','events','out'):
        freeze.add_argument('--'+key,type=Path,required=True)
    final=sub.add_parser('final-evaluate')
    for key in ('spec','freeze','models','out'):
        final.add_argument('--'+key,type=Path,required=True)
    for name in ('process','reprocess'):
        batch=sub.add_parser(name)
        batch.add_argument('--head',type=Path,required=True)
        batch.add_argument('--models',type=Path,required=True)
        batch.add_argument('--limit',type=int,default=10)
        batch.add_argument('--identity',type=Path)
    args = p.parse_args()
    if args.command=='train-joint':
        # An owned orchestration receipt can serialize normalization recovery
        # before the long CPU experiment without occupying a shared slot.
        prerequisite=args.home/'v7/joint-prerequisite.json'
        if prerequisite.exists():
            import time
            value=json.loads(prerequisite.read_bytes())
            state=args.home/value['state']
            deadline=time.monotonic()+86400
            while True:
                status=json.loads(state.read_bytes()) if state.exists() else {}
                if status.get('status')=='complete':break
                if status.get('status')=='failed':raise RuntimeError('Joint prerequisite failed: '+str(state))
                if time.monotonic()>deadline:raise TimeoutError('Joint prerequisite exceeded 24 hours')
                print(json.dumps({'stage':'waiting-prerequisite','state':str(state)}),flush=True)
                time.sleep(20)
    from .contract_v7 import canonical, POLICY
    if args.command == 'status':
        result = {'policy': POLICY, 'reports': [str(x) for x in (args.home / 'v7').rglob('manifest.json')],
                  'default_engine': 'heuristic-v1', 'scientific_targets_met': False}
    elif args.command == 'freeze':
        from .freeze_v7 import freeze
        result=freeze(args.head,args.automatic,args.oracle,args.regression,args.audit,args.out,args.events)
    elif args.command == 'final-evaluate':
        from .final_v7 import evaluate
        result=evaluate(args.home,args.spec,args.freeze,args.models,args.out)
    elif args.command == 'observe':
        from .operations_v7 import observe
        result = observe(args.home)
    elif args.command in ('process','reprocess'):
        from .operations_v7 import process
        from .settings import Settings
        result=process(Settings(args.home,Path('config/topics.json')),args.head,args.models,args.limit,args.identity)
    else:
        from .settings import Settings
        from .operations_v6 import processing_slot
        from .resources_v6 import memory_budget
        from .nlp_v2 import cpu_inference_lock
        from .operations_v7 import resource_check
        resource_check(args.home)
        # Source-pair preparation invokes CpuParser, which locks each forward
        # pass itself. flock is not reentrant across separately opened files.
        nlp_lock = cpu_inference_lock() if args.command in ('train-baseline','train-identity','train-joint','smoke-joint') else nullcontext()
        with processing_slot(Settings(args.home, Path('config/topics.json'))), nlp_lock, memory_budget() as resources:
            if args.command == 'prepare-separated':
                from .training_v7 import prepare_cached
                result = prepare_cached(args.data, args.audit, args.cache, args.out)
            elif args.command == 'train-baseline':
                from .training_v7 import fit_cached
                result = fit_cached(args.features, args.out)
            elif args.command == 'prepare-identity':
                from .identity_training_v7 import prepare
                result=prepare(args.data,args.audit,args.cache,args.models,args.out)
            elif args.command == 'train-identity':
                from .identity_training_v7 import fit
                result=fit(args.features,args.out)
            elif args.command=='prepare-joint':
                from .joint_data_v7 import prepare
                result=prepare(args.features,args.auxiliary,args.out)
            elif args.command=='train-joint':
                from .joint_training_v7 import nested_train
                result=nested_train(args.features,args.bc2gm,args.generic,args.out)
            elif args.command=='smoke-joint':
                from .joint_training_v7 import load_documents,fit_model
                manifest,docs=load_documents(args.features)
                # Explicitly a throughput/gradient smoke test, never a scientific model.
                chosen=[d for task in ('bioredirect','drugprot','cg') for d in [x for x in docs if x['task']==task][:8]]
                _,result=fit_model(chosen,manifest['vocab'],args.base,True,args.out,epochs=1,smoke_steps=args.steps)
            else:
                from .evaluation_v7 import development
                result = development(args.home, args.model, args.predictions, args.models, args.out, args.mode,args.identity)
        result = {**result, 'resources': resources}
    if args.command not in ('status','observe'):
        result={k:v for k,v in result.items() if k in ('schema','selection','outer_metrics','metrics','counts','papers','resources','automatic_enabled')}
        result['command']=args.command
    print(canonical(result), flush=True)


if __name__ == '__main__':
    main()
