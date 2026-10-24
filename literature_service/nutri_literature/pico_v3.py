"""Dependency noun phrases for PICO candidates, retaining exact source offsets."""
from .extraction_v2 import _children, _descendants
import re

PARTICIPANTS={'patient','participant','subject','volunteer','adult','child','infant','woman','man','adolescent','newborn','individual','person'}
MEASURE={'measure','assess','evaluate','record','monitor','determine'}
INTERVENTION={'receive','administer','prescribe','treat','supplement','randomize','randomise','allocate','assign'}
CLINICAL_MEASURE=re.compile(r'\b(?:pressure|weight|BMI|HbA1c|glucose|insulin|cholesterol|mortality|survival|risk|incidence|prevalence|pain|symptoms?|scores?|scales?|function|performance|recovery|hospitali[sz]\w*|quality of life|fatigue|concentrations?|levels?)\b',re.I)
MOLECULAR_ASSAY=re.compile(r'\b(?:gene|mRNA|transcript|promoter|genotype|polymorphism|PCR|sequencing)\b',re.I)


def _phrase(sentence,tokens,root,label):
    indexes=_descendants(tokens,root['i'],{'conj','cc','acl','relcl','ccomp','advcl','parataxis','punct'})
    start=min(tokens[i]['start'] for i in indexes)
    end=max(tokens[i]['end'] for i in indexes)
    return {'label':label,'start':start,'end':end,'text':sentence['text'][start-sentence['start']:end-sentence['start']],
            'rule_id':'pico-noun-phrase-v3','status':'candidate'}


def sentence_spans(sentence,tokens):
    result=[]
    if re.search(r'references?|bibliograph|introduction|background',sentence['label'],re.I):
        return result
    for token in tokens:
        lemma=token['lemma']
        if lemma in PARTICIPANTS and token.get('pos') in {'NOUN','PROPN'}:
            result.append(_phrase(sentence,tokens,token,'participants'))
        label='interventions' if lemma in INTERVENTION else 'outcomes' if lemma in MEASURE else None
        if label:
            for child in _children(tokens,token['i']):
                if child['dep'] in {'dobj','obj','nsubjpass','attr'} and child['lemma'] not in PARTICIPANTS:
                    result.append(_phrase(sentence,tokens,child,label))
                if child['dep']=='prep' and child['text'].casefold() in {'with','to'}:
                    result += [_phrase(sentence,tokens,n,label) for n in _children(tokens,child['i']) if n['dep']=='pobj']
        if lemma in {'outcome','endpoint'}:
            for child in _children(tokens,token['head']):
                if child['dep'] in {'attr','dobj','obj'} and child['i'] != token['i']:
                    result.append(_phrase(sentence,tokens,child,'outcomes'))
    filtered=[]
    for value in result:
        if value['label']=='outcomes':
            if not CLINICAL_MEASURE.search(value['text']) or MOLECULAR_ASSAY.search(value['text']):
                # Retain broad assay targets for review without labelling them
                # clinically meaningful outcomes automatically.
                value={**value,'label':'unresolved_measurement','status':'needs_review'}
            elif re.search(r'methods?|materials?|design|objective',sentence['label'],re.I):
                value={**value,'label':'planned_outcomes','phase':'planned','status':'needs_review'}
            else:
                value={**value,'phase':'observed_or_unspecified'}
        filtered.append(value)
    return list({(r['label'],r['start'],r['end']):r for r in filtered}.values())
