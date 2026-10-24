"""Source-exact independent reviews; original IDs are candidates, not evidence."""
from collections import defaultdict,Counter
import copy
import math
import re
from .common import *
from .review import paths,validate
from ..repair_v3.identity import aligned_ids,mention_key
from ..repair_v3.events import aggregate,semantic_truth
from ..chatgpt_grounding import key,definitions
from ..chatgpt_extraction import exact_span
from ..repair_v4_source_rules import nominal_inhibition_witness

TYPES=('Association','Bind','Negative_Correlation','Positive_Correlation')
_CATALOG=None

def strict_ids(m,d):
    kind='component' if m['kind']=='chemical' else 'gene'
    return {i for e in d.get('entities',[]) if e['kind']==kind and (e['start'],e['end'])==(m['start'],m['end']) for i in e.get('native_ids',[])}

def model_key(model):
    return 'sol' if model=='gpt-6.1-sol' else 'luna' if model=='gpt-6-luna' else 'source_rule'

def literal_native_witness(e,c,g):
    """A new type requires a local literal predicate, never just an old label/quote."""
    if e['observation']=='planned' or e['negated'] or e['decision']=='unsupported':return None
    if e.get('perturbation','none') not in ('none','unspecified','unknown'):return None
    for s in e['evidence']:
        if not all(s['start']<=m['start']<m['end']<=s['end'] for m in (c,g)):continue
        text=s['text']
        for m,token in sorted([(c,' CHEM '),(g,' GENE ')],key=lambda x:-x[0]['start']):
            text=text[:m['start']-s['start']]+token+text[m['end']-s['start']:]
        if e['measurement']=='binding' and re.search(r'CHEM\s+(?:directly\s+)?bind\w*\s+(?:to\s+)?GENE|GENE\s+bind\w*\s+(?:to\s+)?CHEM',text,re.I):
            return 'Bind','binding','unspecified','literal-binding-v4'
        if e['measurement'] not in ('expression','activity'):continue
        verbs=r'(increas\w*|enhanc\w*|induc\w*|reduc\w*|decreas\w*|suppress\w*|inhibit\w*)'
        active=re.search(r'CHEM\s+(?:treatment\s+)?(?:significantly\s+|directly\s+)?'+verbs+r'\s+(?:the\s+)?(?:(expression|activity)\s+of\s+)?GENE(?:\s+(?:mRNA\s+)?(expression|activity))?',text,re.I)
        passive=re.search(r'GENE\s+(expression|activity)\s+(?:was\s+|were\s+|is\s+)'+verbs+r'\s+by\s+CHEM',text,re.I)
        if not active and not passive:continue
        if active:
            verb=active[1];measurement=active[2] or active[3]
        else:
            measurement=passive[1];verb=passive[2]
        # No measurement-free inhibition shortcut or cross-clause transfer.
        if not measurement or measurement.lower()!=e['measurement']:continue
        change='increase' if re.match(r'increas|enhanc|induc',verb,re.I) else 'decrease'
        if e['change'] not in (change,'unspecified'):continue
        return ('Positive_Correlation' if change=='increase' else 'Negative_Correlation'),measurement.lower(),change,'literal-measured-change-v4'
    return None

def new_candidates(m,source):
    global _CATALOG
    if _CATALOG is None:
        from .aliases import load_retriever
        _CATALOG=load_retriever()
    return _CATALOG.retrieve(m,source,[])

def official_hypothesis(m,identifier,source):
    """Look up a model proposal; require name/defined-alias support, never a gold ID."""
    global _CATALOG
    if _CATALOG is None:new_candidates(m,source)
    rows=_CATALOG.by_id.get((m['kind'],identifier),[])
    terms={key(m['text']),*[key(v['long']) for v in definitions(source).get(m['text'],[])]}
    supported=[r for r in rows if terms&{key(n) for n in r['names']}]
    return supported[0] if supported else None

def build(pmid,split):
    p=paper(pmid,split)
    supplied=read(PAYLOAD_HOME/split/('paper-'+pmid+'.json'))
    source=p['document']['text']
    mentions=read(HOME/'canonical-mentions'/split/('paper-'+pmid+'.json'))['mentions']
    by_key={m['key']:m for m in mentions};by_span={mention_key(m):m for m in mentions}
    for m in mentions:
        m['reviews']={}
        for r in m['candidates']:
            r['features']={k:v for k,v in r['features'].items() if k not in ('old_id','historical')}
    reviews={};reference={};rejections=Counter();id_evidence=[]
    for model,effort in MODELS:
        _,vpath=paths(split,model,effort,pmid)
        receipt=read(vpath);rpath=HOME/receipt['response_path']
        response=read(rpath)
        assert receipt['response_sha256']==sha(rpath.read_bytes())
        raw=validate(json.loads(response['text']),supplied)
        reviews[model]=raw;reference[model]=receipt['response_sha256']
        mapping={k:k for k in by_key}
        for n in raw['mentions']:
            original=by_key[n['key']]
            identifier=str(n['selected_id']) if n.get('selected_id') is not None else None
            if not identifier or identifier in {r['native_id'] for r in original['candidates']}:continue
            proposed_kind='chemical' if re.fullmatch('[CD][0-9]+',identifier) else 'gene' if identifier.isdigit() else None
            if proposed_kind is None:continue
            probe={**{k:original[k] for k in ('kind','start','end','text')},'kind':proposed_kind}
            official=official_hypothesis(probe,identifier,source)
            source_candidates=new_candidates(probe,source) if proposed_kind!=original['kind'] else []
            source_identity=any(r['features'].get('exact_alias') or r['features'].get('expansion') for r in source_candidates)
            if official is None and not source_identity:
                rejections['unverified_off_menu_identity']+=1;continue
            if proposed_kind==original['kind']:
                rows=_CATALOG.retrieve({**original,'candidates':original['candidates']+[official]},source,[])
                candidate=next((r for r in rows if r['native_id']==identifier),None)
                if candidate:
                    candidate['features']={k:v for k,v in candidate['features'].items() if k not in ('old_id','historical')}
                    original['candidates']=original['candidates'][:49]+[candidate]
                continue
            corrected=by_span.get(mention_key(probe))
            if corrected is None:
                corrected={**probe,'key':'kind:'+sha(canonical(mention_key(probe)))[:18],
                    'old_ids':[],'cluster':proposed_kind+':'+key(original['text']),
                    'reviews':{},'source':'source_verified_kind_hypothesis','candidates':source_candidates}
                for r in corrected['candidates']:
                    r['features']={k:v for k,v in r['features'].items() if k not in ('old_id','historical')}
                mentions.append(corrected);by_key[corrected['key']]=corrected;by_span[mention_key(probe)]=corrected
            mapping[n['key']]=corrected['key']
            original['reviews'][model]={'eligible':False,'role':'kind_conflict','selected_id':None,
                'valid_id_evidence':False,'kind_hypothesis':corrected['key']}
            id_evidence.append({'mention':original['key'],'model':model,'kind_hypothesis':corrected['key'],
                'official_name_supported':True,'raw_id_verified':official is not None,
                'raw_id':identifier,'historical_id_equality_assumed':False,'relation_score_used':False})
        for n in raw.get('new_mentions',[]):
            occurrences=[x.start() for x in re.finditer(re.escape(n['text']),source)]
            bounded=[a for a in occurrences if (a==0 or not source[a-1].isalnum())
                     and (a+len(n['text'])==len(source) or not source[a+len(n['text'])].isalnum())]
            if bounded:occurrences=bounded
            occurrence=n.get('occurrence',0)
            if not isinstance(occurrence,int) or occurrence<0 or occurrence>=len(occurrences):
                rejections['new_mention_occurrence']+=1;continue
            start=occurrences[occurrence]
            probe={'kind':n['kind'],'start':start,'end':start+len(n['text']),'text':n['text']}
            m=by_span.get(mention_key(probe))
            if m is None:
                expansions=definitions(source).get(n['text'],[])
                normalized=key(expansions[0]['long']) if len(expansions)==1 else key(n['text'])
                m={**probe,'key':'x:'+sha(canonical(mention_key(probe)))[:18],
                   'old_ids':[],'cluster':n['kind']+':'+normalized,
                   'candidates':[],'reviews':{},'source':'independent_review'}
                m['candidates']=new_candidates(m,source)
                for r in m['candidates']:
                    r['features']={k:v for k,v in r['features'].items() if k not in ('old_id','historical')}
                mentions.append(m);by_key[m['key']]=m;by_span[mention_key(m)]=m
            mapping[n['key']]=m['key']
        for n in raw['mentions']+raw.get('new_mentions',[]):
            if n['key'] not in mapping:continue
            m=by_key[mapping[n['key']]]
            proposed=str(n['selected_id']) if n.get('selected_id') is not None else None
            if n['key'].startswith('n') and proposed and proposed not in {r['native_id'] for r in m['candidates']}:
                # A new model ID is a retrieval hypothesis only. Require a pinned official record.
                official=_CATALOG.by_id.get((m['kind'],proposed),[]) if _CATALOG else []
                if official:
                    expanded=_CATALOG.retrieve({**m,'candidates':m['candidates']+[official[0]]},source,[])
                    selected=[r for r in expanded if r['native_id']==proposed]
                    if selected:
                        candidate=selected[0]
                        candidate['features']={k:v for k,v in candidate['features'].items() if k not in ('old_id','historical')}
                        m['candidates']=(m['candidates'][:49]+[candidate]) if len(m['candidates'])>=50 else m['candidates']+[candidate]
            quotes=n.get('id_evidence',[])
            valid=bool(quotes) and all(isinstance(q,str) and q and q in source for q in quotes)
            previous_review=m['reviews'].get(model)
            current_review={'eligible':n.get('eligible'), 'role':str(n.get('role') or 'unknown'),
                'selected_id':proposed,
                'species':n.get('species'),'valid_id_evidence':valid}
            if previous_review is not None:
                if previous_review['selected_id']!=current_review['selected_id']:
                    current_review.update(selected_id=None,identity_conflict=True)
                if previous_review['eligible']!=current_review['eligible']:
                    current_review.update(eligible=None,eligibility_conflict=True)
                if previous_review['role']!=current_review['role']:current_review['role']='mixed'
            m['reviews'][model]=current_review
            spans=[]
            if valid:
                for q in quotes:
                    # Preserve literal intervals separately; no synthetic joined quote.
                    positions=[s.start() for s in re.finditer(re.escape(q),source)]
                    a=min(positions,key=lambda a:abs(a-m['start']))
                    spans.append({'start':a,'end':a+len(q),'text':q})
                long_forms=[key(v['long']) for v in definitions(source).get(m['text'],[])]
                valid=any(s['start']<=m['start']<m['end']<=s['end'] or key(m['text']) in key(s['text'])
                          or any(v in key(s['text']) for v in long_forms) for s in spans)
                m['reviews'][model]['valid_id_evidence']=valid
            id_evidence.append({'mention':m['key'],'model':model,'selected_id':n.get('selected_id'),
                                'valid':valid,'spans':spans})
        raw['_key_mapping']=mapping
    sentences={s['key']:s for s in supplied['sentences']};events=[]
    for model in reviews:
        raw=reviews[model]
        for r in raw['events']:
            c=by_key.get(raw['_key_mapping'].get(r['chemical']))
            g=by_key.get(raw['_key_mapping'].get(r['gene']))
            if not c or not g or c['kind']!='chemical' or g['kind']!='gene':
                rejections['wrong_entity_kind']+=1;continue
            evidence=[sentences[k] for k in sorted(set(r['evidence']),key=lambda k:sentences[k]['start'])]
            # Exact mention occurrences must participate in the source chain.
            if any(not any(s['start']<=m['start']<m['end']<=s['end'] for s in evidence) for m in (c,g)):
                rejections['endpoint_outside_evidence']+=1;continue
            distant=max(int(k[1:]) for k in r['evidence'])-min(int(k[1:]) for k in r['evidence'])>1
            if distant and not r.get('linkage'):
                rejections['missing_document_linkage']+=1;continue
            e={'chemical':c['key'],'gene':g['key'],'origin':model,
               'native_type':r.get('benchmark_relation_type'),'decision':r.get('decision'),
               'measurement':str(r.get('measurement') or 'unspecified'),'change':str(r.get('change') or 'unspecified'),
               'perturbation':str(r.get('perturbation') or 'none'),'role_direction':str(r.get('role_direction') or 'unknown'),
               'observation':str(r.get('observation') or 'unknown'),'condition':r.get('condition'),
               'negated':r.get('negated') is True,'speculative':r.get('speculative') is True,
               'non_significant':r.get('non_significant') is True,'evidence':evidence,
               'counterevidence':[sentences[k] for k in r.get('counterevidence',[])],
               'document_level':distant,'linkage':r.get('linkage'),'source_sha256':sha(source),
               'entity_roles':{kind:by_key[m['key']]['reviews'].get(model,{}).get('role','unknown') for kind,m in [('chemical',c),('gene',g)]}}
            # A native type needs an independent asserted witness. No legacy-label pass.
            eligible=all(m['reviews'].get(model,{}).get('eligible') is True for m in (c,g))
            e['supported_types']=[e['native_type']] if eligible and e['decision']=='supported' and e['native_type'] in TYPES and e['observation']!='planned' else []
            e['key']='ev:'+sha(canonical(e))[:24]
            events.append(e)
    derived=[]
    for e in events:
        if e['decision']!='supported':continue
        witness=literal_native_witness(e,by_key[e['chemical']],by_key[e['gene']])
        if witness and witness[0] not in e['supported_types']:
            native,measurement,change,rule=witness
            typed={**e,'origin':'source_rule_v4','native_type':native,'measurement':measurement,'change':change,
                   'supported_types':[native],'type_rule':rule,'parent_event_key':e['key']}
            typed.pop('key',None);typed['key']='ev:'+sha(canonical(typed))[:24];derived.append(typed)
        nominal=nominal_inhibition_witness(e,by_key[e['chemical']],by_key[e['gene']])
        if nominal and nominal['native_type'] not in e['supported_types']:
            typed={**e,**nominal,'origin':'source_rule_v4','parent_event_key':e['key']}
            typed.pop('key',None);typed['key']='ev:'+sha(canonical(typed))[:24];derived.append(typed)
    events.extend(derived)
    for m in mentions:
        m['_source']=source
        for r in m['candidates']:
            for model in reviews:
                rev=m['reviews'].get(model,{})
                token=model_key(model)
                r['features']['id_vote_'+token]=float(rev.get('selected_id')==r['native_id'] and rev.get('valid_id_evidence') is True)
                r['features']['id_vote_eligible_'+token]=float(rev.get('selected_id')==r['native_id'] and rev.get('eligible') is True)
                r['features']['id_nil_'+token]=float(rev.get('selected_id') is None)
    p.update(mentions=mentions,events=sorted({e['key']:e for e in events}.values(),key=lambda e:e['key']),
             source_sha256=sha(source),review_response_sha256=reference,event_rejections=dict(rejections),id_evidence=id_evidence)
    return p

def validity_features(m,source):
    inside=source[max(0,m['start']-50):m['end']+50]
    f={'kind':m['kind'],'length':math.log1p(len(m['text'])),'short':float(len(m['text'])<=3),
       'residue_context':float(bool(re.search(r'residue|codon|substitut|missense',inside,re.I))),
       'any_exact_alias':float(any(r['features'].get('exact_alias') for r in m['candidates'])),
       'species_match':float(any(r['features'].get('local_tax_match') for r in m['candidates']))}
    for model,_ in MODELS:
        rev=m['reviews'].get(model,{})
        token=model_key(model)
        f['eligible_'+token]=float(rev.get('eligible') is True)
        f['ineligible_'+token]=float(rev.get('eligible') is False)
        f['role_'+token]=str(rev.get('role') or 'unknown')
    return f

def features(events):
    base=aggregate(events)
    # Types are competing outputs of ONE common representation, not independent heads.
    for e in events:
        token=model_key(e['origin'])
        fields=[e['native_type'] or 'unknown',e['measurement'],e['change'],e['observation'],e['perturbation']]
        for name,value in [('native',fields[0]),('native_measure', '|'.join(fields[:2])),
                           ('native_measure_change','|'.join(fields[:3])),('event_contract','|'.join(fields))]:
            k=token+'_'+name+'='+value
            base[k]=base.get(k,0.)+1.
        for name,value in [('counterevidence',bool(e['counterevidence'])),('document_chain',e['document_level'])]:
            k=token+'_'+name;base[k]=max(base.get(k,0.),float(value))
    return base

def bags(p,identity,lock=False,only_model=None):
    from .learning import choose,predict_binary
    chosen={m['key']:choose(m,identity,lock=lock) for m in p['mentions']}
    groups=defaultdict(list)
    for e in p['events']:
        if only_model and e['origin']!=only_model:continue
        # The same source-derived cluster rule is used at training and prediction.
        groups[(by_cluster(chosen[e['chemical']]),by_cluster(chosen[e['gene']]))].append(e)
    result=[]
    for cluster,events in sorted(groups.items()):
        cm=[chosen[k] for k in sorted({e['chemical'] for e in events})]
        gm=[chosen[k] for k in sorted({e['gene'] for e in events})]
        identifiers=[]
        for group in (cm,gm):
            ids={m['selected_id'] for m in group if m['selected_id']}
            identifiers.append(next(iter(ids)) if len(ids)==1 else None)
        witnesses={t:[e for e in events if t in e['supported_types']] for t in TYPES}
        validity=min(max(predict_binary(identity['validity'],validity_features(m,p['document']['text'])) for m in group) for group in (cm,gm))
        result.append({'key':cluster,'ids':identifiers,'chemical_mentions':cm,'gene_mentions':gm,
            'events':events,'features':features(events),'witnesses':witnesses,
            'validity':validity,'unresolved':not all(identifiers)})
    return result

def by_cluster(m):
    # A name cluster never forces contradictory selected identities together.
    return str(m.get('cluster') or m['kind']+':'+key(m['text']))

def project(p,document_bags,model,threshold):
    from .learning import predict_binary,predict_types
    relations={};decisions=[]
    bykey={m['key']:m for m in p['mentions']}
    for b in document_bags:
        pres=predict_binary(model['presence'],b['features'])
        allscores=predict_types(model['type'],b['features'])
        supported={t:s for t,s in allscores.items() if b['witnesses'][t]}
        native=max(supported,key=lambda t:(supported[t],t)) if supported else None
        value=pres*supported[native]*b['validity'] if native else 0.
        accepted=bool(native and not b['unresolved'] and value>=threshold)
        decisions.append({'cluster':b['key'],'ids':b['ids'],'type_scores_all_four':allscores,
                          'native_type':native,'priority':value,'accepted':accepted,
                          'reason':'accepted' if accepted else 'id_unresolved' if b['unresolved'] else 'type_unresolved' if not native else 'below_training_threshold'})
        if not accepted:continue
        e=max(b['witnesses'][native],key=lambda e:(e['observation']=='observed',not e['speculative'],not e['negated'],e['key']))
        endpoints=[bykey[e[k]] for k in ('chemical','gene')]
        r={'entity1':b['ids'][0],'entity2':b['ids'][1],'predicate':native,
           'endpoints':[{k:m[k] for k in ('kind','start','end','text')} for m in endpoints],
           'evidence':e['evidence'],'evidence_event_key':e['key'],'scientific_events':b['events'],
           'priority':value,'priority_is_accuracy_probability':False,'review_status':'unreviewed',
           **{k:e[k] for k in ('negated','speculative','non_significant','observation','role_direction')}}
        if e['role_direction'] in ('chemical_to_gene','gene_to_chemical'):
            r['subject']=b['ids'][int(e['role_direction']=='gene_to_chemical')]
        token=tuple(b['ids'])
        if token not in relations or r['priority']>relations[token]['priority']:relations[token]=r
    return {'relations':sorted(relations.values(),key=lambda r:(r['entity1'],r['entity2'])),
            'decisions':decisions,'source_sha256':p['source_sha256'],'review_response_sha256':p['review_response_sha256']}
