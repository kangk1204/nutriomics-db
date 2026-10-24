"""Nested paper folds; ID-aware acceptance consumes two independently fitted encoders."""
import math
from collections import defaultdict
from .common import *
from .relations import TYPES,native_truth

def truth(documents):return {(d['pmid'],a,b,t) for d in documents for a,b,t in native_truth(d)}

def feature(row,doc,nli,t):
 # All numbers below are model outputs or source features. No PMID/native identifier is a feature.
 import re
 text=' '.join(v['text'] for v in row['views']);a=doc['types'][t];b=nli['types'][t]
 one=[float(j==t) for j in range(len(TYPES))]
 from .semantics import context
 events=context(row['views'])
 flags=[sum(e['observation_state']==state for e in events)/max(1,len(events)) for state in ('planned','background','observed_candidate','unknown')]
 return [doc['exists'],nli['exists'],a,b,a*b,abs(a-b),*doc['types'],*nli['types'],*row['id_features'],
  float(row['identity_unresolved']),float(any(v['same_sentence'] for v in row['views'])),
  float(bool(re.search(r'\b(no|not|without|failed)\b',text,re.I))),float(bool(re.search(r'\b(may|might|could|hypothes|planned|will)\w*',text,re.I))),
  min(len(row['views']),8)/8,*flags,*one]

def fit_meta(rows,values,C):
 import numpy as np
 from sklearn.linear_model import LogisticRegression
 x=[];y=[]
 for r in rows:
  v=values[r['pair_key']]
  r=v.get('row',r)
  for t,typ in enumerate(TYPES):x.append(feature(r,v['document'],v['nli'],t));y.append(int(typ in r['strict_types']))
 if len(set(y))<2:return {'constant':float(y[0]) if y else 0.,'C':C,'n':len(y)}
 # No class balancing: precision/recall tradeoffs are chosen explicitly on held-out papers.
 model=LogisticRegression(C=C,max_iter=1000,random_state=SEED,solver='lbfgs').fit(np.asarray(x),np.asarray(y))
 return {'C':C,'coef':model.coef_[0].tolist(),'intercept':float(model.intercept_[0]),'n':len(y),'positive':sum(y)}

def priority(row,value,model,t):
 if 'constant' in model:return model['constant']
 z=model['intercept']+sum(a*b for a,b in zip(feature(row,value['document'],value['nli'],t),model['coef']))
 return 1/(1+math.exp(-max(-60,min(60,z))))

def decisions(rows,values,model):
 result=[]
 for r in rows:
  v=values[r['pair_key']];r=v.get('row',r);scores=[priority(r,v,model,t) for t in range(len(TYPES))];typ=max(range(len(TYPES)),key=lambda t:(scores[t],-t))
  result.append({'row':r,'predicate':TYPES[typ],'priority':scores[typ],'type_priorities':scores,'document':v['document'],'nli':v['nli']})
 return result

def predicted(decisions,threshold):
 best={}
 for v in decisions:
  if v['priority']<threshold or not all(v['row']['ids']):continue
  key=(v['row']['pmid'],*v['row']['ids'])
  if key not in best or v['priority']>best[key]['priority']:best[key]=v
 return {(*key,v['predicate']) for key,v in best.items()}

def choose(rows,values,documents):
 expected=truth(documents);options=[]
 # Meta-model regularization is selected inside the training partition using another paper fold.
 split=foldmap(documents,3,'meta-selection');outside={}
 for C in POLICY['C']:
  held_decisions=[]
  for f in range(3):
   model=fit_meta([r for r in rows if split[r['pmid']]!=f],values,C)
   held_decisions.extend(decisions([r for r in rows if split[r['pmid']]==f],values,model))
  outside[C]=held_decisions
  for threshold in POLICY['thresholds']:
   score=metric(expected,predicted(held_decisions,threshold));options.append({'C':C,'threshold':threshold,**score})
 floor=POLICY['recall'];eligible=[v for v in options if v['recall']>=floor];passed=[v for v in eligible if v['precision']>=POLICY['precision']]
 if passed:selected=max(passed,key=lambda v:(v['recall'],v['f1'],-v['C'],v['threshold']));reason='training-targets-passed'
 elif eligible:selected=max(eligible,key=lambda v:(v['precision'],v['recall'],v['f1'],-v['C']));reason='precision-target-missed'
 else:selected=max(options,key=lambda v:(v['recall'],v['precision'],v['f1'],-v['C']));reason='recall-floor-missed'
 return {'selected':selected,'reason':reason,'recall_floor':floor,'grid':options,'model':fit_meta(rows,values,selected['C']),
  'meta_cv_note':'Base predictions are out-of-fold; an additional meta split chooses C and threshold. Only the outer end-to-end partition estimates generalization.'}

def fit_and_score(train_rows,held_rows):
 from .relations import fit,score,build_rows
 from .fold_identity import for_relation_partition,key
 docs={d['pmid']:d for d in read(HOME/'data/relations-train.json')['documents']}
 training=[docs[p] for p in sorted({r['pmid'] for r in train_rows})];held=[docs[p] for p in sorted({r['pmid'] for r in held_rows})]
 train_norm,held_norm,receipt=for_relation_partition(training,held)
 train_rows=build_rows('train',training,train_norm,'fit-'+key(training))
 held_rows=build_rows('train',held,held_norm,'held-'+key(training)+'-'+key(held))
 atomic(HOME/'calibration/identity-partitions'/(key(training)+'-'+key(held)+'.json'),receipt,True)
 values={r['pair_key']:{'row':r} for r in held_rows}
 for mode in ('document','nli'):
  model=fit(train_rows,mode);result=score(held_rows,model,mode)
  for key,v in result.items():values[key][mode]=v
 return values

def oof(rows,documents,salt):
 path=HOME/'calibration/oof'/(sha(canonical([sorted(d['pmid'] for d in documents),salt]))[:20]+'.json')
 if path.exists():return read(path)['values']
 folds=foldmap(documents,3,salt);values={}
 for f in range(3):
  partial=path.with_name(path.stem+'-fold'+str(f)+'.json')
  if partial.exists():v=read(partial)
  else:
   v=fit_and_score([r for r in rows if folds[r['pmid']]!=f],[r for r in rows if folds[r['pmid']]==f]);atomic(partial,v,True)
  values.update(v)
 atomic(path,{'values':values,'papers':sorted(d['pmid'] for d in documents),'folds':folds},True);return values

def train():
 from .relations import build_rows,fit
 docs=read(HOME/'data/relations-train.json')['documents'];rows=build_rows('train');folds=foldmap(docs,5,'outer-relation');all_decisions=[];receipts=[]
 for f in range(5):
  path=HOME/'calibration'/('outer-'+str(f)+'.json')
  if path.exists():value=read(path)
  else:
   train_docs=[d for d in docs if folds[d['pmid']]!=f];held=[d for d in docs if folds[d['pmid']]==f]
   training=[r for r in rows if folds[r['pmid']]!=f];test=[r for r in rows if folds[r['pmid']]==f]
   selection=choose(training,oof(training,train_docs,'inner-relation'),train_docs)
   values=fit_and_score(training,test);scored=decisions(test,values,selection['model'])
   accepted=[v for v in scored if v['priority']>=selection['selected']['threshold']]
   value={'fold':f,'selection':selection,'held_pmids':[d['pmid'] for d in held],'accepted':accepted,
    'metrics':metric(truth(held),predicted(scored,selection['selected']['threshold']))}
   atomic(path,value,True)
  receipts.append(value);all_decisions.extend(value['accepted']);log('relation-outer-complete',fold=f,metrics=value['metrics'])
 final=choose(rows,oof(rows,docs,'inner-relation'),docs)
 from .fold_identity import final_training
 from .relations import build_rows as construct_rows
 final_rows=construct_rows('train',docs,final_training(docs),'final-crossfit-ID')
 models={mode:str(fit(final_rows,mode).relative_to(HOME)) for mode in ('document','nli')}
 actual=metric(truth(docs),predicted(all_decisions,0.));passed=actual['precision']>=.85 and actual['recall']>=POLICY['recall']
 result={'final':final,'models':models,'outer_metrics':actual,'outer_folds':[{'fold':v['fold'],'metrics':v['metrics']} for v in receipts],
  'training_gate_passed':passed,'development_used_for_selection':False,'all_gold_denominator':len(truth(docs)),'baseline_oof_recall':.32786885245901637,
  'baseline_oof_source':'preserved v5-r1 selection.json baseline_weighted; conditional legacy exposure, below fixed floor',
  'identity_models_fitted_within_relation_training_partitions':True,'independent_final_test':False}
 atomic(HOME/'calibration/selection.json',result,True);return result
