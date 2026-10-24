"""Listwise CPU ID ranking trained and calibrated on nested automatic errors."""
import gc
from collections import Counter
from .common import *
from .knowledge import Knowledge
from .id_features import Builder,FEATURES


def annotation_scope(d,kind):
    if 'entities' not in d:return None
    if d.get('relation_annotation_complete') is False:return kind==('chemical' if d.get('gene_annotation_complete') is False else 'gene')
    return True


def gold_ids(d,m):
    return {v for e in d.get('entities',[]) if e['kind']==m['kind'] and (e['start'],e['end'])==(m['start'],m['end']) for v in e.get('native_ids',[])}


def table(documents,profile):
    import numpy as np,psutil
    key=sha(canonical([[(d['pmid'],sha(d['text']),sha(canonical(d.get('entities',[])))) for d in documents],profile['files'],filehash(Path(__file__).with_name('id_features.py')),filehash(Path(__file__)),POLICY,filehash(HOME/'knowledge/manifest.json')]))[:24]
    directory=HOME/'identity/tables'/key;receipt=directory/'manifest.json'
    if receipt.exists():
        value=read(receipt)
        for name,digest in value['files_sha256'].items():
            if filehash(directory/name)!=digest:raise ValueError('ID 특징 테이블 변경')
        return directory
    paths={f['pmid']:HOME/f['path'] for f in profile['files']};kb=Knowledge();builder=Builder(kb)
    x=[];targets=[];offsets=[0];weights=[];meta=[];stats=Counter()
    try:
        for number,d in enumerate(documents):
            if psutil.Process().memory_info().rss>8*1024**3:raise MemoryError('ID 특징 생성 8GiB 한도')
            paper=read(paths[d['pmid']]);mentions=paper['mentions'];scoped=sum(bool(annotation_scope(d,m['kind'])) for m in mentions)
            for m in mentions:
                scope=annotation_scope(d,m['kind']);truth=gold_ids(d,m)
                if scope is False:
                    stats['unannotated_kind_masked']+=1;continue
                empty_reference=any(e['kind']==m['kind'] and (e['start'],e['end'])==(m['start'],m['end']) and not e.get('native_ids') for e in d.get('entities',[]))
                candidates,features=builder.encode({'pmid':d['pmid'],'text':d['text']},m)
                ids=[v[0] for v in candidates];positive=[i for i,v in enumerate(ids) if v in truth]
                if not positive:positive=[0]
                y=[float(i in positive)/len(positive) for i in range(len(ids))]
                x.append(np.asarray(features,dtype=np.float32));targets.append(np.asarray(y,dtype=np.float32));offsets.append(offsets[-1]+len(ids));weights.append(float(bool(scope) and not empty_reference)/max(1,scoped))
                meta.append({'pmid':d['pmid'],'key':m['key'],'kind':m['kind'],'start':m['start'],'end':m['end'],
                             'ids':ids,'taxa':[v[1].get('taxon') if v[1] else None for v in candidates],
                             'supported_taxa':m.get('supported_taxa',[]),'previous_id':m.get('previous_id'),
                             'truth':sorted(truth),'annotation_scope_complete':scope,
                             'in10':bool(truth&set(m['candidate_ids'][:10])),'in50':bool(truth&set(ids)),
                             'in200':bool(truth&set(m['candidate_ids'][:200]))})
                stats['mentions']+=1;stats['menu_rows']+=len(ids);stats['unannotated_kind_masked']+=not bool(scope)
                stats['gold_ID_outside_menu']+=bool(truth) and not bool(truth&set(ids));stats['automatic_NIL_examples']+=bool(scope) and not truth and not empty_reference
                stats['exact_empty_reference_masked']+=empty_reference
            if number%10==0:log('automatic-ID-features',papers=number+1,total=len(documents),mentions=len(meta),menu_rows=offsets[-1])
        directory.mkdir(parents=True,exist_ok=True)
        if not x:raise ValueError('ID 특징 입력 0건')
        temp=directory/'arrays.part.npz';np.savez_compressed(temp,x=np.concatenate(x),y=np.concatenate(targets),offsets=np.asarray(offsets,dtype=np.int64),weights=np.asarray(weights,dtype=np.float64))
        os.replace(temp,directory/'arrays.npz');atomic(directory/'meta.json',meta,True)
        atomic(receipt,{'stats':dict(stats),'feature_names':FEATURES,'candidate_limit_training_and_inference':50,'gold_injection':False,
                        'files_sha256':{name:filehash(directory/name) for name in ('arrays.npz','meta.json')}},True)
        log('automatic-ID-table',key=key,**dict(stats));return directory
    finally:builder.aliases.cache_clear();kb.close();gc.collect()


def listwise_objective(w,x,y,offsets,group_weights,C):
    import numpy as np
    score=x@w;maximum=np.maximum.reduceat(score,offsets[:-1]);lengths=np.diff(offsets)
    exp=np.exp(score-np.repeat(maximum,lengths));sums=np.add.reduceat(exp,offsets[:-1]);p=exp/np.repeat(sums,lengths)
    total=max(float(group_weights.sum()),1.);normalizer=maximum+np.log(sums)
    positive=np.add.reduceat(score*y,offsets[:-1]);loss=float(group_weights@(normalizer-positive))/total+float(w@w)/(2*C*total)
    gradient=(x.T@((p-y)*np.repeat(group_weights,lengths)))/total+w/(C*total)
    return loss,gradient


def fit(documents,profile,C):
    import numpy as np,psutil
    from scipy.optimize import minimize
    directory=table(documents,profile);destination=HOME/'identity/linear-rankers'/sha(canonical([filehash(directory/'manifest.json'),C,filehash(Path(__file__))]))[:24]
    receipt=destination/'complete.json'
    if receipt.exists():
        if filehash(destination/'model.json')!=read(receipt)['model_sha256']:raise ValueError('ID 순위 모델 변경')
        return destination/'model.json'
    arrays=np.load(directory/'arrays.npz');x=arrays['x'];y=arrays['y'];offsets=arrays['offsets'];weights=arrays['weights'];started=time.monotonic()
    if not weights.sum():raise ValueError('ID 학습 주석 0건')
    def objective(w):
        if psutil.Process().memory_info().rss>8*1024**3:raise MemoryError('ID CPU 학습 8GiB 한도')
        return listwise_objective(w,x,y,offsets,weights,C)
    result=minimize(objective,np.zeros(x.shape[1]),jac=True,method='L-BFGS-B',options={'maxiter':400,'ftol':1e-8,'gtol':1e-5})
    if not np.isfinite(result.fun) or not np.isfinite(result.x).all():raise FloatingPointError('ID 순위 학습 NaN/Inf')
    if not result.success and result.status!=1:raise RuntimeError('ID 순위 최적화 실패: '+str(result.message))
    model={'weights':result.x.tolist(),'C':C,'features':FEATURES,'candidate_limit':50,'gold_injection':False,
           'training_papers':sorted(d['pmid'] for d in documents),'table_sha256':filehash(directory/'manifest.json'),
           'feature_source_sha256':filehash(Path(__file__).with_name('id_features.py')),'optimizer':'conditional-listwise-LBFGS',
           'iterations':int(result.nit),'optimizer_success':bool(result.success),'loss':float(result.fun),
           'numpy_version':np.__version__}
    atomic(destination/'model.json',model,True);atomic(receipt,{'model_sha256':filehash(destination/'model.json'),'seconds':time.monotonic()-started,'training_papers':len(documents),'C':C,'iterations':int(result.nit),'optimizer_success':bool(result.success)},True)
    log('CPU-ID-ranker-complete',papers=len(documents),C=C,seconds=time.monotonic()-started,iterations=int(result.nit));return destination/'model.json'


def score(documents,profile,path):
    import numpy as np
    model=read(path)
    if model['feature_source_sha256']!=filehash(Path(__file__).with_name('id_features.py')):raise ValueError('ID 특징 정의 변경')
    directory=table(documents,profile);a=np.load(directory/'arrays.npz');meta=read(directory/'meta.json');scores=a['x']@np.asarray(model['weights']);out=[]
    for index,r in enumerate(meta):
        values=scores[a['offsets'][index]:a['offsets'][index+1]];order=sorted(range(len(values)),key=lambda j:(-values[j],str(r['ids'][j])));top=order[0]
        p=np.exp(values-values.max());p/=p.sum();chosen=r['ids'][top];old=r['previous_id'];old_index=r['ids'].index(old) if old in r['ids'] else 0
        out.append({'pmid':r['pmid'],'key':r['key'],'kind':r['kind'],'start':r['start'],'end':r['end'],'previous_id':old,'proposed_id':chosen,
                    'taxon':r['taxa'][top],'priority':float(p[top]),'previous_priority':float(p[old_index]) if old else 0.,'nil_priority':float(p[0]),
                    'margin':float(values[top]-values[order[1]]) if len(order)>1 else 0.,
                    'species_evidence_compatible':r['kind']=='chemical' or (len(r['supported_taxa'])==1 and r['taxa'][top] in r['supported_taxa']),
                    'annotation_scope_complete':r['annotation_scope_complete'],
                    'strict_correct':bool(chosen and chosen in r['truth']) if r['annotation_scope_complete'] else None,
                    'previous_correct':bool(old and old in r['truth']) if r['annotation_scope_complete'] else None,
                    'has_gold':bool(r['truth']),'retrieval_in10':r['in10'],'retrieval_in50':r['in50'],'retrieval_in200':r['in200'],
                    'top10':[{'native_id':r['ids'][j],'score':float(values[j])} for j in order[:10]],'candidate_count':len(values),
                    'normalization_scores_are_probabilities_of_accuracy':False})
    return out


def choose_policy(records):
    result={}
    for kind in ('gene','chemical'):
        result[kind]={}
        for category in ('new','change'):
            eligible=[r for r in records if r['kind']==kind and r.get('annotation_scope_complete') and r['species_evidence_compatible'] and r['proposed_id'] and r['proposed_id']!=r['previous_id'] and bool(r['previous_id'])==(category=='change')];options=[]
            for threshold in POLICY['thresholds']:
                for margin in (0.,.25,.5,1.,2.):
                    accepted=[r for r in eligible if r['priority']>=threshold and r['margin']>=margin];n=len(accepted);ok=sum(bool(r['strict_correct']) for r in accepted);lower=wilson(ok,n)
                    if n>=50 and lower>=.9:options.append({'enabled':True,'threshold':threshold,'margin':margin,'n':n,'correct':ok,'wilson_lower':lower})
            result[kind][category]=max(options,key=lambda r:(r['correct'],r['wilson_lower'],r['threshold'])) if options else {'enabled':False,'threshold':1.000001,'margin':0.,'n':0,'correct':0,'wilson_lower':0.}
    return result


def select(r,policy):
    old=r['previous_id'];proposed=r['proposed_id']
    if proposed==old:return old,'unchanged' if old else 'NIL'
    rule=policy[r['kind']]['change' if old else 'new']
    if proposed and r['species_evidence_compatible'] and rule['enabled'] and r['priority']>=rule['threshold'] and r['margin']>=rule['margin']:
        return proposed,'changed' if old else 'new'
    return old,'retained' if old else 'NIL'


def calibration(documents):
    from .id_profiles import profile,oof
    key=sha(canonical(sorted(d['pmid'] for d in documents)))[:20];receipt=HOME/'identity/policies'/(key+'.json')
    if receipt.exists():return read(receipt)
    folds=foldmap(documents,3,'r1-id-inner');outside={C:[] for C in POLICY['C']}
    for f in range(3):
        training=[d for d in documents if folds[d['pmid']]!=f];held=[d for d in documents if folds[d['pmid']]==f]
        train_profile=oof(training,2);held_profile=profile(held,training)
        for C in POLICY['C']:outside[C].extend(score(held,held_profile,fit(training,train_profile,C)))
    alternatives=[]
    for C,records in outside.items():
        policy=choose_policy(records);accepted=[r for r in records if select(r,policy)[1] in ('new','changed')]
        alternatives.append({'C':C,'policy':policy,'accepted':len(accepted),'correct':sum(bool(r['strict_correct']) for r in accepted)})
    selected=max(alternatives,key=lambda v:(v['correct'],v['accepted'],-v['C']))
    atomic(receipt,{'selected':selected,'alternatives':alternatives,'training_pmids':sorted(d['pmid'] for d in documents),'automatic_held_errors_used':True,'held_scores_sha256':{str(C):sha(canonical(v)) for C,v in outside.items()}},True)
    return read(receipt)


def train():
    from .id_profiles import oof,existing_outer
    documents=read(HOME/'data/identity-train.json')['documents'];folds=foldmap(documents,5,'independent-ner-id');outer=[];all_decisions=[]
    for f in range(5):
        receipt=HOME/'identity'/('outer-'+str(f)+'.json')
        if receipt.exists():value=read(receipt)
        else:
            training=[d for d in documents if folds[d['pmid']]!=f];held=[d for d in documents if folds[d['pmid']]==f]
            selection=calibration(training)['selected'];training_profile=oof(training,3,'r1-id-inner');path=fit(training,training_profile,selection['C'])
            records=score(held,existing_outer(held),path);decisions=[]
            for r in records:
                chosen,why=select(r,selection['policy']);decisions.append({**r,'selected_id':chosen,'reason':why,'selected_correct':r['strict_correct'] if chosen==r['proposed_id'] else r['previous_correct']})
            value={'fold':f,'held_pmids':[d['pmid'] for d in held],'policy':selection['policy'],'C':selection['C'],'decisions':decisions,'automatic_inner_and_outer_profiles':True};atomic(receipt,value,True)
        outer.append(value);all_decisions.extend(value['decisions']);log('automatic-ID-outer-complete',fold=f,accepted=sum(r['reason'] in ('new','changed') for r in value['decisions']))
    selected=calibration(documents)['selected'];policy=selected['policy']
    for kind in ('gene','chemical'):
        for category,why in [('new','new'),('change','changed')]:
            accepted=[r for r in all_decisions if r['kind']==kind and r.get('annotation_scope_complete') and r['reason']==why];ok=sum(bool(r['selected_correct']) for r in accepted)
            if len(accepted)<50 or wilson(ok,len(accepted))<.9:policy[kind][category]={**policy[kind][category],'enabled':False,'blocked_by_automatic_outer_gate':True}
    path=fit(documents,oof(documents,3,'r1-id-inner'),selected['C'])
    summary={}
    for kind in ('gene','chemical'):
        rows=[r for r in all_decisions if r['kind']==kind and r.get('annotation_scope_complete')];summary[kind]={}
        for why in ('new','changed'):
            accepted=[r for r in rows if r['reason']==why];ok=sum(bool(r['selected_correct']) for r in accepted)
            summary[kind][why]={'n':len(accepted),'correct':ok,'precision':ok/len(accepted) if accepted else None,'wilson_lower':wilson(ok,len(accepted))}
        summary[kind]['correct_previous_IDs_damaged']=sum(bool(r['previous_correct']) and not bool(r['selected_correct']) for r in rows)
    atomic(HOME/'identity/selection.json',{'model':str(path.relative_to(HOME)),'policy':policy,'C':selected['C'],'outer_folds':outer,'automatic_outer_summary':summary,
            'automatic_outer_decisions':len(all_decisions),'selection_uses_development':False,'reference_is_dictionary_only':True,
            'training_and_inference_menu_limit':50,'gold_ID_injection':False,'entity_validity_learned_through_NIL':True},True)
    return policy


def apply_all():
    selection=read(HOME/'identity/selection.json');path=HOME/selection['model'];files=[];categories=set(read(HOME/'knowledge/chemical-categories.json')['chemical_ids'])
    excluded=('strict_correct','previous_correct','has_gold','retrieval_in10','retrieval_in50','retrieval_in200','annotation_scope_complete')
    for split in ('train','development'):
        name='relations-train' if split=='train' else 'development-sources';docs=[{'pmid':d['pmid'],'text':d['text']} for d in read(HOME/'data'/(name+'.json'))['documents']]
        profile={'files':[{'pmid':d['pmid'],'path':str((Path('prepared')/split/('paper-'+d['pmid']+'.json'))),'sha256':filehash(HOME/'prepared'/split/('paper-'+d['pmid']+'.json'))} for d in docs]}
        byid={}
        for r in score(docs,profile,path):
            chosen,why=select(r,selection['policy']);record={k:v for k,v in r.items() if k not in excluded}
            record.update(selected_id=chosen,reason=why,proposal_priority=r['priority'],priority=r['priority'] if chosen==r['proposed_id'] else r['previous_priority'],
                          selected_chemical_category_confirmed=r['kind']=='gene' or chosen in categories,category_is_not_an_identity_or_benchmark_type_rule=True)
            byid.setdefault(r['pmid'],[]).append(record)
        for d in docs:
            p=HOME/'normalized'/split/('paper-'+d['pmid']+'.json');atomic(p,{'pmid':d['pmid'],'source_sha256':sha(d['text']),'source_only':True,'decisions':byid.get(d['pmid'],[])},True);files.append({'path':str(p.relative_to(HOME)),'sha256':filehash(p)})
    atomic(HOME/'normalized/manifest.json',{'files':files,'selection_sha256':filehash(HOME/'identity/selection.json'),'automatic_errors_and_menu_width_aligned':True},True)
