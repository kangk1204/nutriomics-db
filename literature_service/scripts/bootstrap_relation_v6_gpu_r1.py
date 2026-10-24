"""Linux-only isolated revision; original science assets stay in place."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.repair_v6_gpu_r1.common import ROOT,HOME,POLICY,read,atomic,filehash,canonical,sha,source_hashes

ORIGINAL=Path('/home/keunsoo/projects/nutriomics-relation-v6-gpu/runtime/relation-repair-v6-gpu')
EXPECTED=Path('/home/keunsoo/projects/nutriomics-relation-v6-gpu-r1')


def main():
    if ROOT.resolve()!=EXPECTED:raise ValueError('명시한 독립 프로젝트에서만 준비 가능')
    HOME.mkdir(parents=True,exist_ok=True)
    def link(name):
        source=ORIGINAL/name;target=HOME/name
        if not source.exists():raise FileNotFoundError(str(source))
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.resolve()!=source.resolve():raise ValueError('기존 경로 충돌: '+str(target))
        else:target.symlink_to(source,target_is_directory=source.is_dir())
    for name in ['data','knowledge','aliases.sqlite3','cache','gpu-budget.json',
                 'identity/ner-oof.json','identity/ner-ready.json','identity/ner-full','identity/alias-dual',
                 *['identity/ner-fold-'+str(f) for f in range(5)],
                 'auxiliary/train.json','auxiliary/manifest.json','auxiliary/cross-source-audit.json',
                 'prepared/train','prepared/development','prepared/normalization',
                 'diagnostics/ner-oof.json','diagnostics/training-candidates.json']:
        link(name)
    budget=read(HOME/'gpu-budget.json')
    if budget.get('active'):raise RuntimeError('이전 GPU 작업이 종료되지 않음')
    if budget['limit_seconds']!=72*3600:raise ValueError('GPU 예산 한도 변경 금지')
    old_ner=read(HOME/'identity/ner-full/complete.json')
    for key in ('seed','model','revision','epochs','learning_rate','warmup','weight_decay','microbatch'):
        if old_ner['policy'][key]!=POLICY[key]:raise ValueError('고정 상류 모델 설정 불일치: '+key)
    from nutri_literature.repair_v6_gpu_r1.id_profiles import retrieval_cache,cache_put
    cache_db=retrieval_cache();files=[];seeded=0
    for split in ('train','development','normalization'):
        for p in sorted((HOME/'prepared'/split).glob('paper-*.json')):
            d=read(p)
            if any(k in d for k in ('entities','relations')):raise ValueError('원문 후보에 정답 필드 포함')
            for m in d['mentions']:
                key=sha(canonical([sha(d['text']),m['kind'],m['start'],m['end']]))
                metadata={k:v for k,v in m.items() if k not in ('ner_score','source')}
                cache_put(cache_db,key,metadata);seeded+=1
                if seeded%1000==0:cache_db.commit()
            files.append({'path':str(p.relative_to(HOME)),'sha256':filehash(p)})
    cache_db.commit();cache_db.close()
    atomic(HOME/'prepared/manifest.json',{'files':files,'relation_train':384,'development':100,'normalization':415,
             'id_supervision_gold_spans_reused':False,'legacy_predictions_used':False,'mention_strategy':'immutable source-only NER plus official aliases'},True)
    original_audit=read(ORIGINAL/'audit.json')
    audit={**original_audit,'revision_of':'relation-repair-v6-gpu','original_audit_sha256':filehash(ORIGINAL/'audit.json'),
           'source':source_hashes(),'ID_revision_contract':{'candidate_limit_both':50,'gold_ID_injection':False,
           'training': 'nested out-of-fold automatic NER plus official alias errors','ranker':'listwise conditional CPU',
           'C':[.1,1.,10.],'policy_per_kind':True,'minimum_policy_support':50,'wilson_lower_required':.9,
           'old_models_preserved':11,'new_model_API_calls':0,'original_GPU_budget_preserved':True}}
    atomic(HOME/'audit.json',audit,True);atomic(HOME/'policy.json',POLICY,True)
    from nutri_literature.repair_v6_gpu_r1.datasets import transfer_contract,audit as check
    transfer_contract();check()
    atomic(HOME/'bootstrap.json',{'original_home':str(ORIGINAL),'budget_actual_path':str((HOME/'gpu-budget.json').resolve()),
         'budget_used_at_migration':budget['used_seconds'],'budget_limit':budget['limit_seconds'],'source_only_profiles':len(files),
         'retrieval_metadata_seeded':seeded,'bootstrap_source_sha256':filehash(Path(__file__)),'auth_copied':False},True)
    print(json.dumps(read(HOME/'bootstrap.json')))


if __name__=='__main__':main()
