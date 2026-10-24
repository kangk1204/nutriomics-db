"""Nested policy/family selection using only the official train calibration40.

Identity cross-fit hyperparameter selection is inherited; this is explicitly a
policy-selection diagnostic, not a fully nested estimate of the entire learner.
"""
from pathlib import Path
import json
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def select_training():
    from nutri_literature import chatgpt_tuning as base,chatgpt_joint_verifier as joint
    from nutri_literature.chatgpt_grounding import Catalog
    from nutri_literature.chatgpt_extraction import project
    from nutri_literature.chatgpt_id_ablation import restore_ids
    from nutri_literature.chatgpt_evaluation import _read,_metric
    from nutri_literature.contract_v7 import immutable,sha
    from nutri_literature.splits_v7 import grouped_folds
    while not (joint.HOME/'selection.json').exists():
        time.sleep(5)
    docs=_read(base.HOME/'inputs/calibration-gold.json')['documents']
    identity=_read(base.HOME/'identity.json')
    catalog=Catalog.load(ROOT/'runtime/chatgpt-inputs/official-id-dictionary.json.gz')
    families,options={},{}
    for mode in ('fill_missing','revalidate_all'):
        predictions={}
        for doc in docs:
            response=_read(joint.HOME/'original-calibration/responses'/('paper-'+doc['pmid']+'.json'))
            original=project(doc['text'],response['text'])['chatgpt_only']
            model=identity['crossfit_models'][str(identity['folds'][doc['pmid']])]
            predictions[doc['pmid']]=restore_ids(doc['text'],original,catalog,model,mode)
        rows=base.score_rows(docs,predictions)
        row={'configuration':mode,'per_paper':{p:r['relation'] for p,r in rows.items()}}
        families['sol_'+mode]=row
        options['sol_'+mode]=[row]
    for model,_ in base.MODELS:
        selection=_read(base.directory(model)/'selection.json')
        name='sol_candidate' if model=='gpt-6.1-sol' else 'luna_candidate'
        options[name]=[{'configuration':r['configuration'],'per_paper':{p:v['relation'] for p,v in r['per_paper'].items()}}
                       for r in selection['grid']]
        families[name]=next(r for r in options[name] if r['configuration']==selection['selected'])
    selection=_read(joint.HOME/'selection.json')
    options['joint']=[{'configuration':r['configuration'],'per_paper':{p:v['relation'] for p,v in r['per_paper'].items()}}
                      for r in selection['grid']]
    families['joint']=next(r for r in options['joint'] if r['configuration']==selection['selected'])
    def objective(metric):
        met=bool(metric['precision'] is not None and metric['precision']>=.85 and metric['recall']>=.30)
        return (met,metric['recall'] if met else metric['f1'],metric['f1'],metric['precision'] or 0.)
    for row in families.values():
        row['training_metrics']=_metric(list(row['per_paper'].values()))
    winner=max(families,key=lambda k:objective(families[k]['training_metrics']))
    folds=grouped_folds(docs,salt='pipeline-family-selection')
    held,fold_choices=[],[]
    for fold in range(5):
        chosen_options={}
        for family,variants in options.items():
            # Re-select the policy without the held fold. The first selector's
            # reuse of full40-selected policies is deliberately not executed.
            def policy_objective(row):
                metric=_metric([r for pmid,r in row['per_paper'].items() if folds[pmid]!=fold])
                return base.criterion(metric) if family in {'sol_candidate','luna_candidate'} else objective(metric)
            chosen_options[family]=max(variants,key=policy_objective)
        family=max(chosen_options,key=lambda k:objective(_metric([r for p,r in chosen_options[k]['per_paper'].items()
                                                                  if folds[p]!=fold])))
        row=chosen_options[family]
        held.extend(r for p,r in row['per_paper'].items() if folds[p]==fold)
        fold_choices.append({'fold':fold,'family':family,'configuration':row['configuration']})
    prereg=base.HOME/'overall-selection-preregistration-v2.json'
    if _read(prereg)['selector_source_sha256']!=sha(Path(__file__).read_bytes()):
        raise ValueError('Family selector changed after preregistration')
    record={'schema':'chatgpt-training-only-family-selection-v2','selected_family':winner,'families':families,
            'selection_scope':'official train40 only; nested policy and family choices conditional on inherited ID cross-fit models',
            'development_results_read_for_selection':False,'family_holdout_metrics':_metric(held),'fold_choices':fold_choices,
            'not_fully_nested_identity_hyperparameter_training':True,
            'calibration_gold_sha256':sha((base.HOME/'inputs/calibration-gold.json').read_bytes()),
            'reviewer_selection_sha256':sha((joint.HOME/'selection.json').read_bytes()),
            'source_sha256':sha(Path(__file__).read_bytes()),'preregistration_sha256':sha(prereg.read_bytes()),
            'default_engine':'heuristic-v1'}
    immutable(base.HOME/'overall-selection.json',record)
    print(json.dumps({'selected_family':winner,'training_metrics':families[winner]['training_metrics'],
                      'family_holdout_metrics':record['family_holdout_metrics']},ensure_ascii=False))


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='report':
        from select_chatgpt_tuned_pipeline import report
        report()
    else:
        select_training()
