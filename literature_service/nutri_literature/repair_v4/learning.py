"""Paper-group nested selection; full strict ID denominators and shared type head."""
from collections import defaultdict
import math
import numpy as np
from .common import *
from .representation import TYPES,strict_ids,validity_features,bags,semantic_truth,project
from ..repair_v3.learning import predict_binary
from ..repair_v3.identity import options
from ..splits_v7 import grouped_folds
from ..chatgpt_repair_io import score
from ..chatgpt_evaluation import _metric

C_GRID=(.1,1.,10.)
_FIT_CACHE={}

def fit_head(features,targets,c,weights=None,multi=False):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    if not targets or len(set(targets))<2:
        if multi:
            target=targets[0] if targets else None
            return {'constant':target,'classes':list(TYPES)}
        return {'intercept':20. if targets and targets[0] else -20.,'weights':{}}
    vectorizer=DictVectorizer();x=vectorizer.fit_transform(features)
    fitted=LogisticRegression(C=c,max_iter=1500,class_weight='balanced',random_state=SEED).fit(x,targets,sample_weight=weights)
    names=vectorizer.get_feature_names_out()
    if multi:
        return {'classes':list(fitted.classes_),
                'heads':[{'intercept':float(b),'weights':dict(zip(names,map(float,w)))} for b,w in zip(fitted.intercept_,fitted.coef_)]}
    return {'intercept':float(fitted.intercept_[0]),'weights':dict(zip(names,map(float,fitted.coef_[0])))}

def linear(model,f):
    return model.get('intercept',0.)+sum(model['weights'].get(k+'='+v if isinstance(v,str) else k,0.)*(1. if isinstance(v,str) else v) for k,v in f.items())

def predict_types(model,f):
    if 'constant' in model:
        return {t:float(t==model['constant']) for t in TYPES}
    z=[linear(h,f) for h in model['heads']]
    if len(z)==1 and len(model['classes'])==2:z=[-z[0]/2,z[0]/2]
    largest=max(z);v=[math.exp(x-largest) for x in z];den=sum(v)
    values=dict(zip(model['classes'],[x/den for x in v]))
    # Unobserved classes have zero support. Masking happens AFTER this competition.
    return {t:values.get(t,0.) for t in TYPES}

def fit_ranker(papers,c):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    xs=[];ys=[];ws=[]
    for p in papers:
        if '_rank_examples' not in p:
            px=[];py=[]
            for m in p['mentions']:
                truth=strict_ids(m,p['document'])
                rows=options(m)
                positive=[r for r in rows if r['native_id'] in truth]
                if not positive:positive=[rows[-1]]
                negative=[r for r in rows if r not in positive]
                # Deterministic hard negatives; all retrieved candidates remain at inference.
                negative=sorted(negative,key=lambda r:(-sum(r['features'].get(k,0.) for k in ('exact_alias','expansion','id_vote_sol','id_vote_luna')),str(r['native_id'])))[:12]
                for yes in positive:
                    for no in negative:
                        delta={k:yes['features'].get(k,0.)-no['features'].get(k,0.) for k in yes['features'].keys()|no['features'].keys()}
                        px.extend([delta,{k:-v for k,v in delta.items()}]);py.extend([1,0])
            p['_rank_examples']=(px,py)
        px,py=p['_rank_examples'];xs.extend(px);ys.extend(py);ws.extend([p['sample_weight']]*len(py))
    vectorizer=DictVectorizer();x=vectorizer.fit_transform(xs)
    model=LogisticRegression(C=c,fit_intercept=False,max_iter=1000,random_state=SEED).fit(x,ys,sample_weight=ws)
    return {'weights':dict(zip(vectorizer.get_feature_names_out(),map(float,model.coef_[0]))),'training_comparisons':len(ys)//2}

def ranked(m,model):
    result=[{**r,'ranking_score':linear(model,r['features'])} for r in options(m)]
    return sorted(result,key=lambda r:(-r['ranking_score'],str(r['native_id'])))

def fit_validity(papers,c):
    xs=[];ys=[];weights=[]
    for p in papers:
        for m in p['mentions']:
            xs.append(validity_features(m,p['document']['text']))
            ys.append(int(bool(strict_ids(m,p['document']))));weights.append(p['sample_weight'])
    return fit_head(xs,ys,c,weights)

def proposal(m,rank,validity,source):
    rows=ranked(m,rank);top=rows[0]
    delta=top['ranking_score']-rows[1]['ranking_score'] if len(rows)>1 else 100.
    sigmoid=1/(1+math.exp(-max(-60.,min(60.,top['ranking_score']))))
    gate=predict_binary(validity,validity_features(m,source))
    return top['native_id'],sigmoid*gate,delta,rows

def calibrate_identity(papers,c):
    folds=grouped_folds([p['document'] for p in papers],folds=3,salt='repair-v4-id-policy')
    records={'new':[],'change':[]}
    for fold in range(3):
        training=[p for p in papers if folds[p['pmid']]!=fold]
        rank=fit_ranker(training,c);validity=fit_validity(training,c)
        for p in papers:
            if folds[p['pmid']]!=fold:continue
            for m in p['mentions']:
                selected,priority,gap,_=proposal(m,rank,validity,p['document']['text'])
                if selected is None or selected in m['old_ids']:continue
                category='change' if m['old_ids'] else 'new'
                records[category].append((priority,gap,int(selected in strict_ids(m,p['document'])),p['sample_weight']))
    choices={}
    for category,records_list in records.items():
        best=None
        for margin in (0.,.25,.5,1.,2.):
            values=[r for r in records_list if r[1]>=margin]
            values.sort(key=lambda r:-r[0])
            correct=wrong=weighted_correct=weighted_wrong=0.
            rows=[]
            for i,r in enumerate(values):
                correct+=r[2];wrong+=1-r[2]
                weighted_correct+=r[2]*r[3];weighted_wrong+=(1-r[2])*r[3]
                if i+1<len(values) and values[i+1][0]==r[0]:continue
                precision=weighted_correct/(weighted_correct+weighted_wrong)
                row={'threshold':r[0],'margin':margin,'correct':int(correct),'wrong':int(wrong),
                     'weighted_precision':precision,'enabled':precision>=.9 and correct>0,
                     'full_strict_denominator_including_unmatched':True}
                rows.append(row)
            for row in rows:
                if best is None or (row['enabled'],row['correct'] if row['enabled'] else 0,row['weighted_precision'])>(best['enabled'],best['correct'] if best['enabled'] else 0,best['weighted_precision']):best=row
        choices[category]=best if best and best['enabled'] else {'enabled':False,'threshold':1.000001,'margin':0.,'correct':0,'wrong':0,
            'full_strict_denominator_including_unmatched':True,'reason':'no nonempty 90 percent OOF policy'}
    return {**choices,'ranker':fit_ranker(papers,c),'validity':fit_validity(papers,c),
            'calibration_papers':[p['pmid'] for p in papers],'source_features_exclude_old_id_and_historical':True}

def choose(m,policy,lock=False):
    original=m['old_ids'][0] if len(m['old_ids'])==1 else None
    if lock:return {**m,'selected_id':original,'selection_reason':'locked_v2_id'}
    source=m.get('_source','')
    proposed,priority,gap,rows=proposal(m,policy['ranker'],policy['validity'],source)
    category='change' if m['old_ids'] else 'new';rule=policy[category]
    eligible=not all(m['reviews'].get(model,{}).get('eligible') is False for model,_ in MODELS)
    selected=original if eligible else None
    reason='preserved_existing' if selected else 'nil'
    if proposed is not None and proposed in m['old_ids'] and eligible:
        selected=proposed;reason='ranked_existing'
    elif proposed is not None and rule['enabled'] and priority>=rule['threshold'] and gap>=rule['margin'] and eligible:
        selected=proposed;reason='ranked_'+category
    return {**m,'selected_id':selected,'proposed_id':proposed,'selection_reason':reason,
            'ranking_priority':priority,'ranking_margin':gap,
            'top10':[r['native_id'] for r in rows if r['native_id']][:10],
            'top50':[r['native_id'] for r in rows if r['native_id']][:50]}

def fit(papers,c):
    import psutil
    if psutil.Process().memory_info().rss>8*1024**3:
        raise MemoryError('v4 CPU 학습 메모리 예산 8GiB 초과; 체크포인트 보존')
    token=sha(canonical([c,sorted(p['pmid'] for p in papers)]))
    if token in _FIT_CACHE:return _FIT_CACHE[token]
    identity=calibrate_identity(papers,c)
    px=[];py=[];pw=[];tx=[];ty=[];tw=[]
    for p in papers:
        for bag in bags(p,identity):
            truth=semantic_truth(bag,p['document'])
            if truth is None:continue
            px.append(bag['features']);py.append(int(bool(truth)));pw.append(p['sample_weight'])
            for native in truth:
                tx.append(bag['features']);ty.append(native);tw.append(p['sample_weight']/len(truth))
    result={'identity':identity,'presence':fit_head(px,py,c,pw),
            'type':fit_head(tx,ty,c,tw,multi=True),'C':c,
            'semantic_bags':len(py),'positive_semantic_bags':sum(py),
            'type_supervision':'document bag; not cloned sentence labels; ID-independent boundary alignment'}
    _FIT_CACHE[token]=result
    return result

def infer(papers,model,threshold,lock=False,only_model=None):
    return {p['pmid']:project(p,bags(p,model['identity'],lock,only_model),model,threshold) for p in papers}

def metrics(papers,predictions):
    per=score([p['document'] for p in papers],predictions)
    actual=_metric([x['relation'] for x in per.values()])
    tp=sum(p['sample_weight']*per[p['pmid']]['relation']['tp'] for p in papers)
    fp=sum(p['sample_weight']*per[p['pmid']]['relation']['fp'] for p in papers)
    fn=sum(p['sample_weight']*per[p['pmid']]['relation']['fn'] for p in papers)
    precision=tp/(tp+fp) if tp+fp else 0.;recall=tp/(tp+fn) if tp+fn else 0.
    weighted={'tp':tp,'fp':fp,'fn':fn,'precision':precision,'recall':recall,
              'f1':2*precision*recall/(precision+recall) if precision+recall else 0.}
    return actual,weighted,per

def objective(m):
    p=m['precision'] or 0.;r=m['recall'] or 0.;f=m['f1'] or 0.
    if p>=.85 and r>=FLOOR:return (2,r,f,p)
    if r>=FLOOR:return (1,p,r,f)
    return (0,r,p,f)

def threshold_sweep(papers,predictions,c):
    """Exact native-ID/type counts at every distinct OOF score, without rescoring texts."""
    from ..evaluation_v6 import relation_sets
    gold={p['pmid']:relation_sets(p['document'],{'relations':[]})[0] for p in papers}
    weights={p['pmid']:p['sample_weight'] for p in papers}
    total=sum(len(v) for v in gold.values())
    weighted_total=sum(len(v)*weights[k] for k,v in gold.items())
    rows=[]
    for p in papers:
        seen=set()
        for r in predictions[p['pmid']]['relations']:
            token=(p['pmid'],tuple(sorted((r['entity1'],r['entity2']))),r['predicate'])
            if token in seen:raise ValueError('중복 예측은 임계값 탐색 전에 통합해야 함')
            seen.add(token)
            rows.append((r['priority'],int(token in gold[p['pmid']]),weights[p['pmid']]))
    rows.sort(key=lambda r:-r[0]);index=0;tp=fp=wtp=wfp=0.
    result=[]
    for threshold in sorted({0.,1.,*[r[0] for r in rows]},reverse=True):
        while index<len(rows) and rows[index][0]>=threshold:
            _,correct,w=rows[index];tp+=correct;fp+=1-correct;wtp+=correct*w;wfp+=(1-correct)*w;index+=1
        actual=_metric([{'tp':int(tp),'fp':int(fp),'fn':total-int(tp)}])
        precision=wtp/(wtp+wfp) if wtp+wfp else 0.
        recall=wtp/weighted_total if weighted_total else 0.
        weighted={'tp':wtp,'fp':wfp,'fn':weighted_total-wtp,'precision':precision,'recall':recall,
                  'f1':2*precision*recall/(precision+recall) if precision+recall else 0.}
        result.append({'C':c,'threshold':threshold,'actual_reviewed_metrics':actual,
                       'reviewed_papers':len(papers),'weighted_estimate':weighted})
    return result

def select(papers,salt):
    folds=grouped_folds([p['document'] for p in papers],folds=3,salt=salt)
    choices=[]
    for c in C_GRID:
        predictions={}
        for fold in range(3):
            training=[p for p in papers if folds[p['pmid']]!=fold]
            held=[p for p in papers if folds[p['pmid']]==fold]
            predictions.update(infer(held,fit(training,c),0.))
        choices.extend(threshold_sweep(papers,predictions,c))
        best=max((r for r in choices if r['C']==c),key=lambda r:objective(r['weighted_estimate']))
        log('inner-selection',papers=len(papers),C=c,best=best)
    chosen=max(choices,key=lambda r:(objective(r['weighted_estimate']),-r['C'],-r['threshold']))
    return chosen,choices

def train_nested(papers):
    folds=grouped_folds([p['document'] for p in papers],folds=5,salt='repair-v4-outer')
    outer=[];predictions={}
    for fold in range(5):
        training=[p for p in papers if folds[p['pmid']]!=fold]
        held=[p for p in papers if folds[p['pmid']]==fold]
        chosen,_=select(training,'repair-v4-inner-'+str(fold))
        model=fit(training,chosen['C']);pred=infer(held,model,chosen['threshold']);predictions.update(pred)
        actual,weighted,_=metrics(held,pred)
        outer.append({'fold':fold,'training_pmids':[p['pmid'] for p in training],
                      'held_pmids':[p['pmid'] for p in held],'selected':chosen,
                      'actual_metrics':actual,'weighted_estimate':weighted})
        log('outer-complete',fold=fold,actual=actual,weighted=weighted)
    chosen,grid=select(papers,'repair-v4-final-selection')
    actual,weighted,_=metrics(papers,predictions)
    return {'schema':'repair-v4-nested-selection','selected':chosen,'grid':grid,'outer_folds':outer,
            'outer_metrics':actual,'outer_weighted_estimate':weighted,'model':fit(papers,chosen['C']),
            'training_gate_passed':weighted['precision']>=.85 and weighted['recall']>=FLOOR,
            'development_labels_used':False,'thresholds':'all distinct inner-OOF priorities plus 0 and 1',
            'scope':'reviewed200 actual counts and sampling-weighted estimates are separate',
            'upstream_fixed_models':'historical exposure retained; not independent final performance'}
