"""Literal same-event predicates; no labels, standard IDs or model scores as input."""
import re

def witness(event,chemical,gene):
    if event.get('decision')!='supported' or event.get('negated') or event.get('observation')=='planned':return None
    for span in event.get('evidence',[]):
        if not all(span['start']<=m['start']<m['end']<=span['end'] for m in (chemical,gene)):continue
        text=span['text']
        for m,token in sorted([(chemical,' CHEM '),(gene,' GENE ')],key=lambda z:-z[0]['start']):
            a,b=m['start']-span['start'],m['end']-span['start'];text=text[:a]+token+text[b:]
        verbs=r'(up[- ]?regulat\w*|down[- ]?regulat\w*|increas\w*|decreas\w*|enhanc\w*|reduc\w*|suppress\w*)'
        patterns=[r'CHEM\s+(?:treatment\s+)?(?:significantly\s+|markedly\s+|directly\s+)?'+verbs+r'\s+(?:the\s+)?(?:(?:mRNA\s+)?(?:expression|activity)\s+of\s+)?GENE(?:\s+(?:mRNA\s+)?(?:expression|activity))',
            r'(?:expression|activity)\s+of\s+GENE\s+(?:was\s+|were\s+|is\s+)?'+verbs+r'\s+(?:by|after)\s+(?:treatment\s+with\s+)?CHEM',
            r'GENE\s+(?:mRNA\s+)?(?:expression|activity)\s+(?:was\s+|were\s+|is\s+)?'+verbs+r'\s+(?:by|after)\s+(?:treatment\s+with\s+)?CHEM']
        for pattern in patterns:
            match=re.search(pattern,text,re.I)
            if not match:continue
            prefix=text[max(text.rfind(';',0,match.start()),text.rfind(',',0,match.start()))+1:match.start()]
            if re.search(r'\b(?:whether|will|aim|purpose|not|without)\b',prefix,re.I):continue
            verb=match[1];change='increase' if re.match(r'up|increas|enhanc',verb,re.I) else 'decrease'
            measurement='expression' if re.search('expression',match[0],re.I) else 'activity'
            return {'native_type':'Positive_Correlation' if change=='increase' else 'Negative_Correlation',
                'measurement':measurement,'change':change,'evidence':[span],'type_rule':'literal-measurement-role-v5'}
        match=re.search(r'CHEM\s+binding\s+to\s+GENE|GENE\s+(?:is|was)\s+(?:a\s+|the\s+)?receptor\s+for\s+CHEM',text,re.I)
        if match and not re.search(r'\b(?:not|without|whether)\b',text[:match.start()],re.I):
            return {'native_type':'Bind','measurement':'binding','change':'unspecified','evidence':[span],'role_direction':'undirected','type_rule':'literal-binding-role-v5'}
    return None
