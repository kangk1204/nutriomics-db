import argparse
import os
from datetime import datetime,timezone
from .common import *
from .review import request

def main():
    parser=argparse.ArgumentParser(description='격리된 관계·ID v4 실제 추론 실험')
    parser.add_argument('stage',choices=['audit','prepare-canonical','pilot','infer-train','train','freeze','infer-dev','predict','evaluate','compare','resume','status'])
    args=parser.parse_args()
    if args.stage=='status':
        state=read(HOME/'run-state.json') if (HOME/'run-state.json').exists() else None
        alive=False
        if state:
            import psutil
            try:alive=any('chatgpt_repair_v4.py' in token for token in psutil.Process(state['pid']).cmdline())
            except (psutil.Error,KeyError):pass
        log('status',budget=budget_status(),
            train_valid=sum(1 for _ in (INFERENCE_HOME/'train').glob('*/valid/*.json')),
            development_valid=sum(1 for _ in (INFERENCE_HOME/'development').glob('*/valid/*.json')),
            legacy_technical_valid=sum(1 for _ in (HOME/'inference').glob('*/*/valid/*.json')),
            controller=state,controller_alive=alive,
            evaluation_complete=(HOME/'completion-audit.json').exists())
        return
    from ..chatgpt_batch import batch_lock
    with batch_lock(HOME):
        state={'pid':os.getpid(),'command':args.stage,'started_utc':datetime.now(timezone.utc).isoformat(),'running':True}
        (HOME/'run-state.json').write_text(canonical(state),encoding='utf8')
        try:
            if args.stage=='audit':prepare()
            elif args.stage=='prepare-canonical':
                from .aliases import prepare_canonical
                prepare_canonical()
            elif args.stage=='pilot':request('train',True)
            elif args.stage=='infer-train':request('train')
            elif args.stage=='infer-dev':request('development')
            else:
                from . import pipeline
                getattr(pipeline,args.stage.replace('-','_'))()
            state['exit']='completed'
        except Exception as exc:
            state.update(exit='stopped',error_class=type(exc).__name__)
            raise
        finally:
            state.update(running=False,ended_utc=datetime.now(timezone.utc).isoformat())
            (HOME/'run-state.json').write_text(canonical(state),encoding='utf8')
