import copy,json,re
from collections import defaultdict
from .common import *
from ..repair_v4 import representation as oldrep,learning as oldlearn
from ..repair_v3.identity import mention_key,aligned_ids
from ..repair_v3.events import aggregate
from ..repair_v4_source_rules import nominal_inhibition_witness
from ..chatgpt_extraction import exact_span

ROLE=('active_agent','analyte','therapeutic_protein','endogenous_protein','gene_product','variant','residue','assay_reagent','family','generic_class','unknown')

def normalized_role(value):
    s=str(value or 'unknown').lower()
    if s in ROLE:return s
    for cue,label in [('residue','residue'),('variant','variant'),('mutation','variant'),('family','family'),('class','generic_class'),('therapeut','therapeutic_protein'),('endogenous','endogenous_protein'),('reagent','assay_reagent'),('drug','active_agent'),('agonist','active_agent'),('gene','gene_product'),('protein','gene_product'),('analyte','analyte')]:
        if cue in s:return label
    return 'unknown'

def overlay(p,model,raw,payload):
    nodes={m['key']:m for m in p['mentions']};source=p['document']['text'];mapping={k:k for k in nodes};updated={}
    for m in raw.get('new_mentions',[]):
        positions=[a.start() for a in re.finditer(re.escape(m['text']),source) if (a.start()==0 or not source[a.start()-1].isalnum()) and (a.end()==len(source) or not source[a.end()].isalnum())]
        occurrence=m.get('occurrence',0)
        if occurrence>=len(positions):continue
        a=positions[occurrence];token=(m['kind'],a,a+len(m['text']))
        existing=next((x for x in p['mentions'] if mention_key(x)==token),None)
        if existing is None:
            from .retrieval import Search
            existing={'key':'v5:'+sha(canonical(token))[:18],'kind':m['kind'],'start':a,'end':a+len(m['text']),'text':m['text'],'old_ids':[],'reviews':{},'cluster':m['kind']+':'+m['text'].casefold()}
            search=Search();rows=search.retrieve(existing,source);search.close()
            existing.update(candidates=rows[:50],candidate_pool200=[r['native_id'] for r in rows]);p['mentions'].append(existing);nodes[existing['key']]=existing
        mapping[m['key']]=existing['key']
    for n in raw['mentions']+raw.get('new_mentions',[]):
        if n['key'] not in mapping:continue
        m=nodes[mapping[n['key']]];quotes=n.get('id_evidence',[])
        valid=bool(quotes) and all(q and q in source for q in quotes) and any(m['text'].casefold() in q.casefold() for q in quotes)
        decision={'eligible':n.get('eligible'),'role':normalized_role(n.get('role')),'selected_id':str(n['selected_id']) if n.get('selected_id') is not None else None,'valid_id_evidence':valid,'species':n.get('species')}
        if m['key'] in updated:
            previous=updated[m['key']]
            if previous['selected_id']!=decision['selected_id']:decision.update(selected_id=None,identity_conflict=True)
            if previous['eligible']!=decision['eligible']:decision.update(eligible=None,eligibility_conflict=True)
            if previous['role']!=decision['role']:decision['role']='unknown'
            if previous['species']!=decision['species']:decision['species']=None
            decision['valid_id_evidence']=previous['valid_id_evidence'] and valid
        updated[m['key']]=decision;m['reviews'][model]=decision
    # Preserve previous events as historical candidates, without treating them as fresh votes.
    for e in p['events']:
        if e['origin']==model:e['origin']='cached_before_v5:'+model
    sentences={s['key']:s for s in payload['sentences']}
    for event in raw['events']:
        c=nodes.get(mapping.get(event['chemical']));g=nodes.get(mapping.get(event['gene']))
        if not c or not g or c['kind']!='chemical' or g['kind']!='gene':continue
        evidence=[sentences[k] for k in event['evidence']]
        if not all(any(s['start']<=m['start']<m['end']<=s['end'] for s in evidence) for m in (c,g)):continue
        distant=max(int(k[1:]) for k in event['evidence'])-min(int(k[1:]) for k in event['evidence'])>1
        if distant and not event.get('linkage'):continue
        e={'chemical':c['key'],'gene':g['key'],'origin':model,'native_type':event.get('benchmark_relation_type'),
            'measurement':event.get('measurement') or 'unspecified','change':event.get('change') or 'unspecified',
            'perturbation':event.get('perturbation') or 'none','role_direction':event.get('role_direction') or 'unknown',
            'decision':event['decision'],'observation':event.get('observation') or 'unknown','condition':event.get('condition'),
            'negated':bool(event.get('negated')),'speculative':bool(event.get('speculative')),'non_significant':bool(event.get('non_significant')),
            'evidence':evidence,'counterevidence':[sentences[k] for k in event.get('counterevidence',[])],
            'document_level':distant,'linkage':event.get('linkage'),'source_sha256':sha(source),
            'entity_roles':{'chemical':c['reviews'].get(model,{}).get('role','unknown'),'gene':g['reviews'].get(model,{}).get('role','unknown')}}
        eligible=all(m['reviews'].get(model,{}).get('eligible') is True for m in (c,g))
        e['supported_types']=[e['native_type']] if eligible and e['decision']=='supported' and e['native_type'] in oldrep.TYPES and e['observation']!='planned' else []
        e['key']='ev5:'+sha(canonical(e))[:24];p['events'].append(e)
    for m in p['mentions']:
        for r in m['candidates']:
            for model,_ in MODELS:
                v=m['reviews'].get(model,{});k='sol' if 'sol' in model else 'luna'
                r['features']['id_vote_'+k]=float(v.get('selected_id')==r['native_id'] and v.get('valid_id_evidence') is True)
                r['features']['id_vote_eligible_'+k]=float(v.get('selected_id')==r['native_id'] and v.get('eligible') is True)
                r['features']['id_nil_'+k]=float(v.get('selected_id') is None)

def derive(p):
    from .source_rules import witness as local_witness
    nodes={m['key']:m for m in p['mentions']};extra=[]
    for e in p['events']:
        if e['decision']!='supported':continue
        c,g=nodes[e['chemical']],nodes[e['gene']]
        nominal=nominal_inhibition_witness(e,c,g)
        witness=oldrep.literal_native_witness(e,c,g)
        values=[]
        if nominal:values.append(nominal)
        if witness:
            native,measurement,change,rule=witness;values.append({'native_type':native,'measurement':measurement,'change':change,'supported_types':[native],'type_rule':rule})
        literal=local_witness(e,c,g)
        if literal:values.append({**literal,'supported_types':[literal['native_type']]})
        for v in values:
            if v['native_type'] in e['supported_types']:continue
            row={**e,**v,'origin':'source_rule_v5','parent_event_key':e['key']};row.pop('key',None);row['key']='ev5:'+sha(canonical(row))[:24];extra.append(row)
    p['events']=sorted({e['key']:e for e in p['events']+extra}.values(),key=lambda e:e['key'])

def load(split):
    from .review import paths
    from ..repair_v4.review import validate
    legacy={p['pmid']:p for p in cached(split)};docs={d['pmid']:d for d in data(split)};papers=[]
    for pmid,old in legacy.items():
        p=read(HOME/'rebuilt'/split/('paper-'+pmid+'.json'));p['document']=docs[pmid];p['sample_weight']=old['sample_weight'];p['_legacy']=old
        for model,effort in MODELS:
            _,vp=paths(split,pmid,model,effort)
            if vp.exists():
                receipt=read(vp);rp=HOME/receipt['response_path'];assert filehash(rp)==receipt['response_sha256']
                payload=read(HOME/'payloads'/split/('paper-'+pmid+'.json'))
                overlay(p,model,validate(json.loads(read(rp)['text']),payload),payload)
        derive(p)
        for m in p['mentions']:
            m['_source']=p['document']['text']
            for v in m['reviews'].values():v['role']=normalized_role(v.get('role'))
            # Names stay in durable retrieval artifacts, not in training memory.
            for r in m['candidates']:
                r.pop('names',None)
                # Learn raw-ID version preference; no fixed historical-ID penalty.
                r['features']['official_current']=float(r.get('retrieval_source')=='official-current')
        papers.append(p)
    return papers

def source_feature(events,native):
    normalized=[]
    for original in events:
        e={**original,'entity_roles':{k:normalized_role(v) for k,v in original.get('entity_roles',{}).items()}}
        normalized.append(e)
    f=aggregate(normalized,native)
    for e in normalized:
        origin=e['origin'];token='sol' if origin=='gpt-6.1-sol' else 'luna' if origin=='gpt-6-luna' else 'cached' if origin.startswith('cached_before') else 'literal'
        f['vote_'+token]=max(f.get('vote_'+token,0.),float(native in e.get('supported_types',[])))
        f['contract='+e['measurement']+'|'+e['change']+'|'+str(e['negated'])]=1.
    return f

def anchor_rows(p,legacy_policy):
    chosen={mention_key(m):oldlearn.choose(m,legacy_policy) for m in p['_legacy']['mentions']};rows=[]
    for r in p['baseline_raw']['relations']:
        ids=[chosen.get(mention_key(m),{}).get('selected_id') or r['entity1' if i==0 else 'entity2'] for i,m in enumerate(r['endpoints'])]
        row={**r,'entity1':ids[0],'entity2':ids[1]}
        if r.get('subject') in (r['entity1'],r['entity2']):row['subject']=ids[int(r['subject']==r['entity2'])]
        rows.append(row)
    return rows

def candidates(p,identity,legacy_policy,identity_only=False,lock_ids=False):
    byspan={mention_key(m):m for m in p['mentions']};bykey={m['key']:m for m in p['mentions']}
    grouped={};source=p['document']['text']
    def add(ids,native,endpoints,evidence,events,anchor=False,identity_trace=None):
        if not all(ids) or native not in oldrep.TYPES:return
        token=tuple(ids)+(native,)
        if token not in grouped:
            grouped[token]={'ids':ids,'native':native,'endpoints':endpoints,'evidence':evidence,'events':[],
                'anchor':False,'identity_trace':identity_trace,'source_witness':False}
        row=grouped[token];row['anchor']|=anchor;row['events'].extend(events)
        if events:row['source_witness']|=any(native in e.get('supported_types',[]) for e in events)
    anchors=anchor_rows(p,legacy_policy);locked={}
    for r in anchors:
        for i,m in enumerate(r['endpoints']):locked.setdefault(mention_key(m),set()).add(r['entity1' if i==0 else 'entity2'])
    for r in anchors:
        ms=[byspan.get(mention_key(m),{**m,'key':'fallback','reviews':{},'old_ids':[],'candidates':[]}) for m in r['endpoints']]
        ids=[r['entity1'],r['entity2']];trace=[]
        for i,m in enumerate(ms):
            if not m['candidates'] or lock_ids:continue
            choice=oldlearn.choose({**m,'old_ids':[ids[i]]},identity)
            trace.append({'old_id':ids[i],'proposed':choice.get('proposed_id'),'reason':choice['selection_reason']})
            # NIL preserves a previous candidate; it is never an automatic deletion.
            ids[i]=choice['selected_id'] or ids[i]
        events=[e for e in p['events'] if (byspan.get(mention_key(r['endpoints'][0]),{}).get('key')==e['chemical'] and byspan.get(mention_key(r['endpoints'][1]),{}).get('key')==e['gene'])]
        add(ids,r['predicate'],r['endpoints'],r['evidence'],events,True,trace)
        if r.get('subject') in (r['entity1'],r['entity2']):grouped[tuple(ids)+(r['predicate'],)]['anchor_subject']=ids[int(r['subject']==r['entity2'])]
    if not identity_only:
        chosen={m['key']:oldlearn.choose(m,identity) for m in p['mentions']}
        if lock_ids:
            old={mention_key(m):oldlearn.choose(m,legacy_policy)['selected_id'] for m in p['_legacy']['mentions']}
            for m in p['mentions']:
                values=locked.get(mention_key(m),set())
                chosen[m['key']]['selected_id']=next(iter(values)) if len(values)==1 else old.get(mention_key(m))
        for e in p['events']:
            if e['origin'].startswith('cached_before'):continue
            c,g=bykey[e['chemical']],bykey[e['gene']]
            ids=[chosen[c['key']]['selected_id'],chosen[g['key']]['selected_id']]
            for native in e.get('supported_types',[]):
                add(ids,native,[{k:m[k] for k in ('kind','start','end','text')} for m in (c,g)],e['evidence'],[e])
    result=[]
    for row in grouped.values():
        row['events']=sorted({e['key']:e for e in row['events']}.values(),key=lambda e:e['key'])
        directions={e['role_direction'] for e in row['events'] if row['native'] in e.get('supported_types',[]) and e.get('role_direction') in ('chemical_to_gene','gene_to_chemical')}
        row['role_direction']=next(iter(directions)) if len(directions)==1 else 'unknown'
        row['subject']=row['ids'][int(row['role_direction']=='gene_to_chemical')] if len(directions)==1 else row.get('anchor_subject')
        f=source_feature(row['events'],row['native']);f.update(anchor=float(row['anchor']),source_witness=float(row['source_witness']),native=row['native'])
        quote=' '.join(s['text'] for s in row['evidence'])
        f.update(residue_context=float(bool(re.search(r'\b(?:codon|missense|substitution|residue)\b',quote,re.I))),
            explicit_null=float(bool(re.search(r'\b(?:no effect|unchanged|unaltered|not associated)\b',quote,re.I))),
            explicit_binding=float(bool(re.search(r'\bbind\w*|affinity',quote,re.I))))
        for i,m in enumerate(row['endpoints']):
            original=byspan.get(mention_key(m),{})
            candidate=next((r for r in original.get('candidates',[]) if r['native_id']==row['ids'][i]),None)
            for k in ('exact_alias','normalized_exact','expansion','ngram_similarity','token_similarity','local_tax_match','local_tax_conflict','id_vote_sol','id_vote_luna'):
                f[('chemical_' if i==0 else 'gene_')+k]=candidate['features'].get(k,0.) if candidate else 0.
        row['features']=f
        # Semantics are labeled by positions, independent of the selected native IDs.
        aligned=[aligned_ids(m,p['document']) for m in row['endpoints']]
        from ..evaluation_v6 import relation_sets
        truth=relation_sets(p['document'],{'relations':[]})[0] if 'entities' in p['document'] else set()
        semantics=None if not all(aligned) else int(any(t==row['native'] and set(pair)&aligned[0] and set(pair)&aligned[1] for _,pair,t in truth))
        row['semantic_target']=semantics
        row['strict_target']=int((p['pmid'],tuple(sorted(row['ids'])),row['native']) in truth)
        row['id_target']=int(all(row['ids'][i] in oldrep.strict_ids(m,p['document']) for i,m in enumerate(row['endpoints']))) if 'entities' in p['document'] else None
        result.append(row)
    return sorted(result,key=lambda r:(*r['ids'],r['native']))
