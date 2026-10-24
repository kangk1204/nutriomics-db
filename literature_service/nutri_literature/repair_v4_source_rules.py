"""Literal nominal target roles, grounded in the pinned BioRED inhibitor guideline.

This module does not inspect model type votes, scores, IDs, PMIDs or annotations.
It supplies an alternative type witness only for an explicit source statement.
"""
import re

RULE_VERSION='biored-nominal-target-role-v1'

def nominal_inhibition_witness(event,chemical,gene):
    if event.get('decision')!='supported' or event.get('negated') or event.get('observation')=='planned':
        return None
    if max(chemical['start'],gene['start'])<min(chemical['end'],gene['end']):return None
    modifiers=r'(?:(?:direct|selective|specific|potent|competitive|noncompetitive|reversible|irreversible)\s+){0,3}'
    cue=r'(?:inhibitor|antagonist|blocker)\b(?!\s*[-‐‑]?\s*\d)'
    patterns=[
        r'CHEM\s+(?:is|was|acts?\s+as)\s+(?:an?\s+)?'+modifiers+r'GENE\s+'+cue,
        r'CHEM\s*,\s*(?:an?\s+)?'+modifiers+r'GENE\s+'+cue,
        r'GENE\s+'+cue+r'\s*,?\s+CHEM\b',
        r'CHEM\s+(?:is|was|acts?\s+as)\s+(?:an?\s+)?'+modifiers+cue+r'\s+of\s+GENE\b',
    ]
    for sentence in event.get('evidence',[]):
        if not all(sentence['start']<=m['start']<m['end']<=sentence['end'] for m in (chemical,gene)):continue
        text=sentence['text']
        for mention,token in sorted([(chemical,' CHEM '),(gene,' GENE ')],key=lambda pair:-pair[0]['start']):
            a,b=mention['start']-sentence['start'],mention['end']-sentence['start']
            text=text[:a]+token+text[b:]
        if '?' in text:continue
        # A mixed agonist/antagonist sentence needs its individual scope resolved.
        if re.search(r'\b(?:agonist|activator)\b',text,re.I):continue
        for pattern in patterns:
            match=re.search(pattern,text,re.I)
            if match is None:continue
            prefix=text[:match.start()]
            prefix=prefix[max(prefix.rfind(';'),prefix.rfind(','))+1:]
            if re.search(r'\b(?:not|no|neither|without|unlike|whether|if|potential|putative|suspected|proposed)\b|rather\s+than|compared\s+(?:with|to)',prefix,re.I):continue
            return {'native_type':'Negative_Correlation','supported_types':['Negative_Correlation'],
                'measurement':'target_inhibition_role','change':'unspecified','role_direction':'chemical_to_gene',
                'decision':'supported','condition':None,'non_significant':False,'observation':'background',
                'evidence':[sentence],'type_rule':RULE_VERSION,
                'parent_condition_not_transferred':True,'interpretation':'explicit inhibitory target role; not a measured clinical effect'}
    return None
