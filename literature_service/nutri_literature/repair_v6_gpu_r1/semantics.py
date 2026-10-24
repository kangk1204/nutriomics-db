"""Source flags remain separate from benchmark labels and causal direction."""
import re

MEASURES={'expression':r'\bexpress(?:ion|ed|es)?\b','activity':r'\bactivit(?:y|ies)\b','phosphorylation':r'\bphosphorylat\w*','ubiquitination':r'\bubiquitinat\w*',
 'binding':r'\bbind(?:ing|s)?\b','drug_response':r'\b(?:resistan\w*|sensitiv\w*|response)\b','concentration':r'\b(?:concentration|level)\w*'}
def context(views):
 events=[]
 for v in views:
  text=' '.join(s['text'] for s in v['segments']);planned=bool(re.search(r'\b(?:will|plan(?:ned)? to|aim(?:ed)? to|hypothes\w*)\b',text,re.I))
  events.append({'source_intervals':v['segments'],'endpoints':v['endpoints'],'measurement_candidates':[k for k,p in MEASURES.items() if re.search(p,text,re.I)],
   'planned_cue_present':planned,'observed_result_confirmed':False,'negation_cue_present':bool(re.search(r'\b(?:not|no|without|failed)\b',text,re.I)),
   'non_significant_cue_present':bool(re.search(r'\b(?:not significant|non-significant|nonsignificant)\b',text,re.I)),
   'speculation_cue_present':bool(re.search(r'\b(?:may|might|could|suggest)\w*',text,re.I)),'scope_status':'cue-only; event scope unresolved',
   'auto_approved_evidence':False})
 return events
