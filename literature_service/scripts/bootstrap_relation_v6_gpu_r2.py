"""Prepare source-only contextual candidates in a new revision; share the original budget."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v6_gpu_r2.common import *

ORIGINAL=Path('/home/keunsoo/projects/nutriomics-relation-v6-gpu/runtime/relation-repair-v6-gpu')


def main():
    if ROOT!=Path('/home/keunsoo/projects/nutriomics-relation-v6-gpu-r2'):
        raise ValueError('Only the dedicated r2 development project is permitted')
    if (HOME/'bootstrap.json').exists():return read(HOME/'bootstrap.json')
    HOME.mkdir(parents=True,exist_ok=True)
    def link(name,target=None):
        p=HOME/(target or name);s=ORIGINAL/name
        if not s.exists():raise FileNotFoundError(str(s))
        p.parent.mkdir(parents=True,exist_ok=True)
        if p.exists() or p.is_symlink():
            if p.resolve()!=s.resolve():raise ValueError('Existing revision asset differs')
        else:p.symlink_to(s,target_is_directory=s.is_dir())
    for name in ['data','knowledge','aliases.sqlite3','cache','gpu-budget.json',
                 'identity/ner-oof.json','identity/ner-ready.json','identity/ner-full','identity/alias-dual',
                 *['identity/ner-fold-'+str(i) for i in range(5)],'auxiliary/train.json','auxiliary/manifest.json','auxiliary/cross-source-audit.json']:
        link(name)
    link('prepared','prepared-base')
    budget=read(HOME/'gpu-budget.json')
    if budget.get('active'):raise RuntimeError('Original GPU owner is still active')
    from nutri_literature.repair_v6_gpu_r2.dense import Retriever
    from nutri_literature.repair_v6_gpu_r2.preparation import contextual_candidates
    from nutri_literature.repair_v6_gpu_r2.id_profiles import retrieval_cache,cache_put
    retriever=Retriever();cache=retrieval_cache();files=[];mentions=0
    try:
        for split in ('train','development','normalization'):
            paths=sorted((HOME/'prepared-base'/split).glob('paper-*.json'))
            for n,p in enumerate(paths):
                target=HOME/'prepared'/split/p.name
                if not target.exists():
                    d=read(p)
                    if 'entities' in d or 'relations' in d:raise ValueError('Gold fields in source-only candidates')
                    values=[contextual_candidates(d,m,retriever) for m in d['mentions']]
                    atomic(target,{**d,'mentions':values,'context_retrieval':True,'gold_injection':False},True)
                d=read(target)
                for m in d['mentions']:
                    key=sha(canonical([sha(d['text']),m['kind'],m['start'],m['end']]))
                    cache_put(cache,key,{k:v for k,v in m.items() if k not in ('ner_score','source')});mentions+=1
                cache.commit();files.append({'path':str(target.relative_to(HOME)),'sha256':filehash(target)})
                if n%10==0:log('contextual-candidate-preparation',split=split,papers=n+1,total=len(paths),mentions=mentions)
    finally:cache.close();retriever.close()
    atomic(HOME/'prepared/manifest.json',{'files':files,'normalization':415,'relation_train':384,'development':100,
           'context_retrieval':True,'id_supervision_gold_spans_reused':False,'legacy_predictions_used':False},True)
    original=read(ORIGINAL/'audit.json')
    atomic(HOME/'audit.json',{**original,'revision_of':'relation-repair-v6-gpu','source':source_hashes(),
           'revision_contract':{'same_menu':50,'gold_injection':False,'separate_validity':True,'contextual_retrieval':True,'shared_GPU_budget':True,'new_API_calls':0}},True)
    atomic(HOME/'policy.json',POLICY,True)
    from nutri_literature.repair_v6_gpu_r2.datasets import transfer_contract,audit
    transfer_contract();audit()
    result={'original_home':str(ORIGINAL),'source_only_files':len(files),'mentions':mentions,
            'budget_used_at_start':budget['used_seconds'],'budget_actual_path':str((HOME/'gpu-budget.json').resolve()),
            'no_gold_development_export':True,'source_sha256':source_hashes()}
    atomic(HOME/'bootstrap.json',result,True);return result


if __name__=='__main__':print(canonical(main()))
