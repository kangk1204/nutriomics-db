"""Target-domain ID supervision is fitted inside each relation training partition."""
from .common import *
from . import identity


def profile(documents):
    external={d['pmid'] for d in read(HOME/'data/identity-train.json')['documents']}
    files=[]
    for d in documents:
        split='normalization' if d['pmid'] in external else 'train'
        p=HOME/'prepared'/split/('paper-'+d['pmid']+'.json')
        files.append({'pmid':d['pmid'],'path':str(p.relative_to(HOME)),'sha256':filehash(p)})
    return {'files':files,'upstream_NER_excludes_all_relation_training_papers':True}


def external():
    return read(HOME/'data/identity-train.json')['documents']


def key(documents):return sha(canonical(sorted(d['pmid'] for d in documents)))[:20]


def calibrate(allowed):
    destination=HOME/'identity/relation-folds'/(key(allowed)+'.json')
    if destination.exists():
        value=read(destination)
        if value['source_sha256']!=filehash(__file__):raise ValueError('Target ID calibration source changed')
        return value
    extra=external()
    if {d['pmid'] for d in extra}&{d['pmid'] for d in allowed}:raise ValueError('External and target ID documents overlap')
    folds=foldmap(allowed,3,'r2-target-ID');outside={(family,C):[] for family in POLICY['id_families'] for C in POLICY['C']}
    for f in range(3):
        train=[d for d in allowed if folds[d['pmid']]!=f];held=[d for d in allowed if folds[d['pmid']]==f]
        if not train or not held:raise ValueError('Target ID fold is empty')
        # No held relation document contributes its ID annotation, policy or validity label.
        fitting=extra+train;training_profile=profile(fitting);held_profile=profile(held)
        for family,C in outside:
            path=identity.fit(fitting,training_profile,C,family)
            outside[family,C].extend(identity.score(held,held_profile,path))
    options=[]
    for (family,C),records in outside.items():
        policy=identity.choose_policy(records)
        accepted=[r for r in records if identity.select(r,policy)[1] in ('new','changed')]
        options.append({'family':family,'C':C,'policy':policy,'accepted':len(accepted),'correct':sum(bool(r['strict_correct']) for r in accepted)})
    selected=max(options,key=lambda v:(v['correct'],v['accepted'],-v['C'],v['family']=='linear'))
    records=outside[selected['family'],selected['C']]
    value={'selected':selected,'alternatives':options,'records':records,'training_pmids':sorted(d['pmid'] for d in allowed),
           'upstream_NER_training_pmids':sorted(d['pmid'] for d in extra),'source_sha256':filehash(__file__),
           'target_ID_labels_used_within_training_partition_only':True,'development_used':False}
    atomic(destination,value,True);return value


def normalized(records,policy):
    out={}
    for r in records:
        chosen,why=identity.select(r,policy)
        clean={k:v for k,v in r.items() if k not in {'valid_span','strict_correct','previous_correct','has_gold','annotation_scope_complete','retrieval_in10','retrieval_in50','retrieval_in200'}}
        clean.update(selected_id=chosen,reason=why,proposal_priority=r['priority'],
                     priority=r['priority'] if chosen==r['proposed_id'] else r['previous_priority'] if chosen else 0.)
        out.setdefault(r['pmid'],[]).append(clean)
    return out


def for_relation_partition(training,held):
    allowed={d['pmid'] for d in training};forbidden={d['pmid'] for d in held}
    if allowed&forbidden:raise ValueError('Relation partition overlap in target-domain ID learning')
    if {textkey(d['text']) for d in training}&{textkey(d['text']) for d in held}:raise ValueError('Duplicate relation text across ID partition')
    if {d['pmcid'] for d in training if d.get('pmcid')}&{d['pmcid'] for d in held if d.get('pmcid')}:raise ValueError('PMCID overlap across ID partition')
    selection=calibrate(training);chosen=selection['selected'];fitting=external()+training
    model=identity.fit(fitting,profile(fitting),chosen['C'],chosen['family'])
    records=identity.score(held,profile(held),model)
    return normalized(selection['records'],chosen['policy']),normalized(records,chosen['policy']),{
        'ID_model':str(model.relative_to(HOME)),'policy':chosen['policy'],'family':chosen['family'],'C':chosen['C'],
        'target_training_pmids':sorted(allowed),'held_pmids':sorted(forbidden),'held_labels_used':False,
        'NER_training_excludes_all_relation_documents':True}


def final_training(documents):
    selection=calibrate(documents);chosen=selection['selected'];fitting=external()+documents
    path=identity.fit(fitting,profile(fitting),chosen['C'],chosen['family'])
    atomic(HOME/'identity/final-relation.json',{'model':str(path.relative_to(HOME)),'selected':chosen,
           'target_training_pmids':sorted(d['pmid'] for d in documents),'development_used':False},True)
    return normalized(selection['records'],chosen['policy'])


def apply_development():
    selection=read(HOME/'identity/final-relation.json');documents=read(HOME/'data/development-sources.json')['documents']
    if any('entities' in d or 'relations' in d for d in documents):raise ValueError('Gold leaked into development ID inference')
    files=[];p={'files':[]}
    for d in documents:
        source=HOME/'prepared/development'/('paper-'+d['pmid']+'.json')
        p['files'].append({'pmid':d['pmid'],'path':str(source.relative_to(HOME)),'sha256':filehash(source)})
    records=identity.score(documents,p,HOME/selection['model']);values=normalized(records,selection['selected']['policy'])
    for d in documents:
        path=HOME/'normalized-final/development'/('paper-'+d['pmid']+'.json')
        atomic(path,{'pmid':d['pmid'],'source_sha256':sha(d['text']),'decisions':values.get(d['pmid'],[]),'source_only':True},True)
        files.append({'path':str(path.relative_to(HOME)),'sha256':filehash(path)})
    atomic(HOME/'normalized-final/manifest.json',{'files':files,'selection_sha256':filehash(HOME/'identity/final-relation.json'),'source_only':True},True)
