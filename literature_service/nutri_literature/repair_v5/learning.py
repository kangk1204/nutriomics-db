"""Fold-local identity and residual acceptance; no independent-score product."""
from collections import defaultdict
from .common import *
from .representation import candidates,anchor_rows
from ..repair_v4 import learning as old
from ..repair_v4.representation import TYPES
from ..repair_v3.learning import predict_binary
from ..splits_v7 import grouped_folds
from ..evaluation_v6 import relation_sets

_POLICIES={}

def policies(papers):
    token=tuple(sorted(p['pmid'] for p in papers))
    if token not in _POLICIES:
        legacy=[p['_legacy'] for p in papers]
        base=old.calibrate_identity(legacy,1.)
        new=old.calibrate_identity(papers,1.)
        _POLICIES[token]=(base,new)
        if len(_POLICIES)>18:_POLICIES.pop(next(iter(_POLICIES)))
    return _POLICIES[token]

def fit(papers,c):
    import psutil
    if psutil.Process().memory_info().rss>8*1024**3:raise MemoryError('v5 8GiB 메모리 한도')
    base,identity=policies(papers);features=[];strict=[];weights=[];semantic_x=[];semantic_y=[];semantic_w=[];id_x=[];id_y=[];id_w=[]
    for p in papers:
        for r in candidates(p,identity,base):
            features.append(r['features']);strict.append(r['strict_target']);weights.append(p['sample_weight'])
            if r['semantic_target'] is not None:
                semantic_x.append({k:v for k,v in r['features'].items() if not k.startswith(('chemical_','gene_'))});semantic_y.append(r['semantic_target']);semantic_w.append(p['sample_weight'])
            if r['id_target'] is not None:
                id_x.append(r['features']);id_y.append(r['id_target']);id_w.append(p['sample_weight'])
    return {'C':c,'identity':identity,'legacy_identity':base,
        'acceptance':old.fit_head(features,strict,c,weights),
        'semantics':old.fit_head(semantic_x,semantic_y,c,semantic_w),
        'id_correctness':old.fit_head(id_x,id_y,c,id_w),
        'targets':{'strict_rows':len(strict),'semantic_rows':len(semantic_y),'semantic_id_errors_not_negative':True},
        'no_score_product':True,'legacy_policy_fitted_inside_training_fold':True,'identity_C_fixed':1.}

def scored(papers,model,lock=False):
    result={};base={}
    for p in papers:
        rows=candidates(p,model['identity'],model['legacy_identity'],lock_ids=lock)
        for r in rows:
            r['priority']=predict_binary(model['acceptance'],r['features'])
            r['semantic_priority']=predict_binary(model['semantics'],r['features'])
            r['id_priority']=predict_binary(model['id_correctness'],r['features'])
        result[p['pmid']]=rows;base[p['pmid']]=anchor_rows(p,model['legacy_identity'])
    return result,base

def choose_rows(rows,policy):
    selected=[];accepted_anchors=defaultdict(list)
    for r in rows:
        if r['anchor'] and r['priority']>=policy['anchor_threshold']:
            selected.append(r);accepted_anchors[tuple(r['ids'])].append(r)
    alternatives=defaultdict(list)
    for r in rows:
        if not r['anchor'] and r['source_witness'] and r['priority']>=policy['new_threshold']:
            alternatives[tuple(r['ids'])].append(r)
    for pair,values in alternatives.items():
        best=max(values,key=lambda r:(r['priority'],r['native']))
        anchors=accepted_anchors[pair]
        if anchors and best['priority']<max(r['priority'] for r in anchors)+policy['relabel_margin']:continue
        # Any relabel has its OWN source witness. Original anchors remain in candidate audit.
        if anchors:selected=[r for r in selected if tuple(r['ids'])!=pair]
        selected.append(best)
    return sorted(selected,key=lambda r:(*r['ids'],r['native']))

def counts(papers,rows,policy,weighted=True):
    tp=fp=fn=0.
    for p in papers:
        if '_gold_set' not in p:p['_gold_set']=relation_sets(p['document'],{'relations':[]})[0]
        truth=p['_gold_set'];pmid=p['pmid'];w=p['sample_weight'] if weighted else 1.
        if policy.get('mode')=='baseline':
            pred={ (pmid,tuple(sorted((r['entity1'],r['entity2']))),r['predicate']) for r in rows[pmid] }
        else:pred={(pmid,tuple(sorted(r['ids'])),r['native']) for r in choose_rows(rows[pmid],policy)}
        tp+=w*len(truth&pred);fp+=w*len(pred-truth);fn+=w*len(truth-pred)
    precision=tp/(tp+fp) if tp+fp else 0.;recall=tp/(tp+fn) if tp+fn else 0.
    return {'tp':tp,'fp':fp,'fn':fn,'precision':precision,'recall':recall,'f1':2*precision*recall/(precision+recall) if precision+recall else 0.}

def objective(m,floor):
    p,r,f=m['precision'],m['recall'],m['f1']
    return (2,r,f,p) if p>=.85 and r>=floor else (1,p,r,f) if r>=floor else (0,r,p,f)

def select(papers,salt):
    folds=grouped_folds([p['document'] for p in papers],folds=3,salt=salt);choices=[];allrows={};allbase={}
    for c in (.1,1.,10.):
        rows={};base={}
        for f in range(3):
            train=[p for p in papers if folds[p['pmid']]!=f];held=[p for p in papers if folds[p['pmid']]==f]
            model=fit(train,c);r,b=scored(held,model);rows.update(r);base.update(b)
        bm=counts(papers,base,{'mode':'baseline'});floor=max(FLOOR,bm['recall'])
        if c==.1:choices.append({'mode':'baseline','C':1.,'weighted':bm,'actual':counts(papers,base,{'mode':'baseline'},False),'recall_floor':floor})
        # Fixed grid for keep/remove, new additions, and supported relabeling.
        for a in (0.,.05,.15,.3,.5,.7,.85,.95,.99):
            for n in (.5,.7,.85,.95,.99,1.000001):
                for margin in (0.,.1,.2):
                    policy={'mode':'editor','C':c,'anchor_threshold':a,'new_threshold':n,'relabel_margin':margin}
                    choices.append({**policy,'weighted':counts(papers,rows,policy),'actual':counts(papers,rows,policy,False),'recall_floor':floor})
        log('inner-selection',C=c,papers=len(papers),best=max([x for x in choices if x['C']==c],key=lambda x:objective(x['weighted'],x['recall_floor'])))
    chosen=max(choices,key=lambda x:(objective(x['weighted'],x['recall_floor']),x['mode']=='baseline',-x['C']))
    return chosen,choices

def train_nested(papers):
    folds=grouped_folds([p['document'] for p in papers],folds=5,salt='repair-v5-outer');outer=[];combined={};base_all={}
    for f in range(5):
        train=[p for p in papers if folds[p['pmid']]!=f];held=[p for p in papers if folds[p['pmid']]==f]
        chosen,_=select(train,'repair-v5-inner-'+str(f));model=fit(train,chosen['C']);rows,base=scored(held,model)
        use=base if chosen['mode']=='baseline' else {k:choose_rows(v,chosen) for k,v in rows.items()}
        if chosen['mode']=='editor':
            use={k:[{'entity1':r['ids'][0],'entity2':r['ids'][1],'predicate':r['native']} for r in v] for k,v in use.items()}
        combined.update(use);base_all.update(base)
        metric=counts(held,use,{'mode':'baseline'});outer.append({'fold':f,'selected':chosen,'weighted':metric,'held_pmids':[p['pmid'] for p in held]});log('outer-complete',fold=f,metrics=metric)
    chosen,grid=select(papers,'repair-v5-final');metric=counts(papers,combined,{'mode':'baseline'});baseline_metric=counts(papers,base_all,{'mode':'baseline'})
    return {'selected':chosen,'outer_folds':outer,'outer_weighted':metric,'outer_actual':counts(papers,combined,{'mode':'baseline'},False),
        'outer_baseline_weighted':baseline_metric,'grid':grid,'model':fit(papers,chosen['C']),
        'training_gate_passed':metric['precision']>=.85 and metric['recall']>=max(FLOOR,baseline_metric['recall']),
        'model_selected_from_development':False,'upstream_fixed_v2_exposure':'known; conditional development comparison'}

def project(p,model,policy,arm='combined'):
    from ..chatgpt_extraction import exact_span
    rows,base=scored([p],model,lock=arm=='events_only');selected=[]
    if policy['mode']=='baseline' and arm=='combined':
        return {'relations':base[p['pmid']],'decision':'training-selected baseline; new candidates retained in audit','candidates':len(rows[p['pmid']])}
    if policy['mode']=='baseline':policy={'mode':'editor','anchor_threshold':0.,'new_threshold':1.000001,'relabel_margin':.2}
    if arm=='retrieval_only':selected=[r for r in rows[p['pmid']] if r['anchor']]
    else:selected=choose_rows(rows[p['pmid']],policy)
    relations=[]
    for r in selected:
        for span in r['evidence']+r['endpoints']:
            if not exact_span(p['document']['text'],span):raise ValueError('출처 위치 불일치')
        relation={'entity1':r['ids'][0],'entity2':r['ids'][1],'predicate':r['native'],'endpoints':r['endpoints'],
            'evidence':r['evidence'],'scientific_events':r['events'],'priority':r['priority'],
            'semantic_priority':r['semantic_priority'],'id_priority':r['id_priority'],'priority_is_accuracy_probability':False,
            'review_status':'unreviewed','decision':'keep_or_id_edit' if r['anchor'] else 'source_supported_add_or_relabel',
            'identity_trace':r.get('identity_trace'),'role_direction':r.get('role_direction','unknown')}
        if r.get('subject'):relation['subject']=r['subject']
        relations.append(relation)
    return {'relations':relations,'candidate_audit':[{'ids':r['ids'],'type':r['native'],'anchor':r['anchor'],'source_witness':r['source_witness'],'priority':r['priority']} for r in rows[p['pmid']]],'all_old_candidates_preserved':True}
