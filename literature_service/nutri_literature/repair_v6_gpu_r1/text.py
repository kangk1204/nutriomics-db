"""Exact offsets, document-local abbreviations, and bounded mention-pair views."""
import re,unicodedata
from .common import sha,exact

def normal(s):
 s=unicodedata.normalize('NFKC',s).casefold()
 return ' '.join(re.sub(r'[^\w]+',' ',s).split())
def sentences(text):
 out=[];a=0
 for m in re.finditer(r'(?<=[.!?])\s+(?=[A-Z])',text):
  if a<m.start():out.append({'start':a,'end':m.start(),'text':text[a:m.start()]})
  a=m.end()
 if a<len(text):out.append({'start':a,'end':len(text),'text':text[a:]})
 return out
def definitions(text):
 result={}
 greek={'α':'a','β':'b','γ':'g','δ':'d','κ':'k','μ':'m','ω':'o'}
 def letter(c):return greek.get(c.casefold(),c.casefold())
 for m in re.finditer(r'\(([^()\s]{2,15})\)',text):
  short=m.group(1)
  if not any(c.isalpha() for c in short) or short[0].isdigit():continue
  before=text[max(0,m.start()-160):m.start()].rstrip();letters=[letter(c) for c in short if c.isalnum()]
  if not letters:continue
  j=len(before)-1;first=None
  for index in range(len(letters)-1,-1,-1):
   c=letters[index]
   while j>=0 and (letter(before[j])!=c or (index==0 and j>0 and before[j-1].isalnum())):j-=1
   if j<0:break
   first=j;j-=1
  else:
   while first and before[first-1].isalnum():first-=1
   long=before[first:]
   if 1<=len(long.split())<=min(len(letters)+5,2*len(letters)) and len(long)>len(short):
    result.setdefault(normal(short),set()).add(normal(long))
 return result
SPECIES={9606:r'\b(?:human|humans|patients?|participants?|Homo sapiens)\b',10090:r'\b(?:mice|mouse|murine|Mus musculus)\b',10116:r'\b(?:rat|rats|rattus norvegicus)\b'}
def species(text):return sorted(t for t,p in SPECIES.items() if re.search(p,text,re.I))
def clusters(mentions,text):
 aliases=definitions(text);result={}
 for m in mentions:
  if not exact(text,m):raise ValueError('개체 위치 불일치')
  name=normal(m['text']);long=aliases.get(name,set());key=next(iter(long)) if len(long)==1 else name
  local=species(text[max(0,m['start']-100):m['end']+100]);tax=local[0] if len(local)==1 else None
  ambiguous=m['kind']=='gene' and (len(local)>1 or (not local and len(species(text))>1))
  token=(m['kind'],key,tax if m['kind']=='gene' else None,m['start'] if ambiguous else None)
  result.setdefault(token,[]).append({**m,'local_taxa':local,'key':sha(str((m['kind'],m['start'],m['end'])))[:20]})
 return [{'key':sha(str(k))[:20],'kind':k[0],'name':k[1],'taxon':k[2],'mentions':sorted(v,key=lambda m:m['start'])} for k,v in sorted(result.items(),key=lambda x:str(x[0]))]
def marked(text,c,g):
 positions=sorted([(c['start'],c['end'],'[C]','[/C]'),(g['start'],g['end'],'[G]','[/G]')])
 if positions[0][1]>positions[1][0]:return None
 out=[];a=0
 for start,end,left,right in positions:out.extend([text[a:start],left,text[start:end],right]);a=end
 out.append(text[a:]);return ''.join(out)
def pair_views(text,chemical,gene,max_views=3):
 pairs=sorted(((c,g) for c in chemical['mentions'] for g in gene['mentions']),key=lambda cg:(abs(cg[0]['start']-cg[1]['start']),cg[0]['start'],cg[1]['start']))
 spans=sentences(text);out=[];seen=set()
 for c,g in pairs:
  if c['start']<g['end'] and g['start']<c['end']:continue
  matched=[s for s in spans if any(s['start']<=m['start']<s['end'] for m in (c,g))]
  if not matched:continue
  start=min(s['start'] for s in matched);end=max(s['end'] for s in matched)
  if end-start<=1800:
   segments=[{'start':start,'end':end,'text':text[start:end]}]
   value=marked(text[start:end],{**c,'start':c['start']-start,'end':c['end']-start},{**g,'start':g['start']-start,'end':g['end']-start})
  else:
   segments=[];values=[]
   for m,l,r in ((c,'[C]','[/C]'),(g,'[G]','[/G]')):
    a=max(0,m['start']-220);b=min(len(text),m['end']+220)
    segments.append({'start':a,'end':b,'text':text[a:b]});values.append(text[a:m['start']]+l+m['text']+r+text[m['end']:b])
   value=' [SEP] '.join(values)
  if value is None or value in seen:continue
  seen.add(value);out.append({'text':value,'segments':segments,'endpoints':[c,g],
   'same_sentence':any(s['start']<=min(c['start'],g['start']) and max(c['end'],g['end'])<=s['end'] for s in spans)})
  if len(out)>=max_views:break
 return out
