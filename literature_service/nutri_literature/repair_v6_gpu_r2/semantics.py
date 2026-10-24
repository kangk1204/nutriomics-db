"""Source flags remain separate from benchmark labels and causal direction."""
import re

MEASURES={'expression':r'\bexpress(?:ion|ed|es)?\b','activity':r'\bactivit(?:y|ies)\b','phosphorylation':r'\bphosphorylat\w*','ubiquitination':r'\bubiquitinat\w*',
 'binding':r'\bbind(?:ing|s)?\b','drug_response':r'\b(?:resistan\w*|sensitiv\w*|response)\b','concentration':r'\b(?:concentration|level)\w*'}
def context(views):
 events=[]
 for v in views:
  text=' '.join(s['text'] for s in v['segments']);planned=bool(re.search(r'\b(?:will|plan(?:ned)? to|aim(?:ed)? to|hypothes\w*)\b',text,re.I))
  background=bool(re.search(r'\b(?:previous(?:ly)?|prior studies|has been shown|earlier reports|reported by)\b',text,re.I))
  measured=bool(re.search(r'\b(?:measured|observed|reduced|decreased|increased|inhibited|enhanced|found|showed|was associated|were associated)\b',text,re.I))
  state='planned' if planned else 'background' if background else 'observed_candidate' if measured else 'unknown'
  clauses=[]
  for segment in v['segments']:
   raw=segment['text'];boundaries=[0]+[m.end() for m in re.finditer(r';\s*|\b(?:but|whereas|however)\b\s*',raw,re.I)]+[len(raw)]
   for start,end in zip(boundaries,boundaries[1:]):
    value=raw[start:end]
    clauses.append({'start':segment['start']+start,'end':segment['start']+end,'text':value,
     'negated':bool(re.search(r'\b(?:no|not|without|failed)\b',value,re.I)),
     'speculative':bool(re.search(r'\b(?:may|might|could|suggest)\w*',value,re.I)),
     'non_significant':bool(re.search(r'\b(?:not significant|non-significant|nonsignificant)\b',value,re.I)),
     'measurements':[k for k,p in MEASURES.items() if re.search(p,value,re.I)]})
  events.append({'source_intervals':v['segments'],'endpoints':v['endpoints'],'measurement_candidates':[k for k,p in MEASURES.items() if re.search(p,text,re.I)],
   'clauses':clauses,'observation_state':state,
   'planned_cue_present':planned,'observed_result_confirmed':False,'negation_cue_present':bool(re.search(r'\b(?:not|no|without|failed)\b',text,re.I)),
   'non_significant_cue_present':bool(re.search(r'\b(?:not significant|non-significant|nonsignificant)\b',text,re.I)),
   'speculation_cue_present':bool(re.search(r'\b(?:may|might|could|suggest)\w*',text,re.I)),'scope_status':'clause-scoped candidates; full syntactic roles remain under review',
   'auto_approved_evidence':False})
 return events
