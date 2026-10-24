"""Whole official aliases restore mentions; chemical categories are not inferred from D/C prefixes."""
import gzip,re,json
from .common import *
from .knowledge import namekey
from .text import definitions,normal

def export_categories():
 from defusedxml.ElementTree import iterparse
 source=ROOT/'runtime/v6/models/mesh';allowed=set();counts={};hashes={}
 for name,tag,key in [('desc2026.gz','DescriptorRecord','DescriptorUI'),('supp2026.gz','SupplementalRecord','SupplementalRecordUI')]:
  path=source/name;hashes[name]=filehash(path);total=0;kept=0
  with gzip.open(path,'rb') as f:
   for _,node in iterparse(f,events=['end']):
    if node.tag!=tag:continue
    total+=1;identifier=node.findtext(key)
    valid=any((t.text or '').startswith('D') for t in node.findall('./TreeNumberList/TreeNumber')) if tag=='DescriptorRecord' else node.attrib.get('SCRClass')=='1'
    if valid:allowed.add(identifier);kept+=1
    node.clear()
  counts[name]={'total':total,'chemical_category':kept}
 value={'chemical_ids':sorted(allowed),'counts':counts,'source_sha256':hashes,'definition':'Descriptor tree branch D, or SupplementalRecord SCRClass=1. No ID equality/parent inference.',
  'selected_using_evaluation_answers':False}
 atomic(HOME/'knowledge/chemical-categories.json',value,True);return {k:v for k,v in value.items() if k!='chemical_ids'}

def extract(text,kb):
 allowed=set(read(HOME/'knowledge/chemical-categories.json')['chemical_ids']);tokens=list(re.finditer(r'\w+(?:[-\u2010-\u2013]\w+)*',text));candidates={};abbreviations=definitions(text)
 for i,first in enumerate(tokens):
  for j in range(i,min(len(tokens),i+8)):
   start,end=first.start(),tokens[j].end();surface=text[start:end]
   if len(surface)>120:break
   if re.search(r'[.;:!?\n]',surface):break
   if len(surface)<2:continue
   candidates.setdefault(namekey(surface),[]).append((start,end,surface))
   expansion=abbreviations.get(normal(surface),set())
   if len(expansion)==1:candidates.setdefault(namekey(next(iter(expansion))),[]).append((start,end,surface))
 keys=sorted(candidates);matches={}
 for database in (kb.base,kb.overlay):
  for begin in range(0,len(keys),250):
   subset=keys[begin:begin+250]
   if not subset:continue
   query='SELECT a.norm,a.kind,r.native_id,r.names FROM aliases a JOIN records r ON a.rid=r.rid WHERE a.kind=? AND a.norm IN ('+','.join('?'*len(subset))+')'
   rows=(row for scope in ('gene','chemical') for row in database.execute(query,[scope,*subset]))
   for key,kind,identifier,names in rows:
    if kind=='chemical' and identifier not in allowed:continue
    aliases=json.loads(names)
    # Sorted-word search forms are retrieval aids, not proof of a literal entity boundary.
    exact=[name for name in aliases if namekey(name)==key]
    if not exact:continue
    for a,b,surface in candidates[key]:
     abbreviation=key!=namekey(surface)
     if kind=='gene' and not abbreviation and len(re.sub(r'\W','',surface))<=3 and surface not in exact:continue
     token=(kind,a,b);matches[token]={'kind':kind,'start':a,'end':b,'text':surface,'ner_score':0.,'source':'official-abbreviation-union' if abbreviation else 'official-alias-union','dictionary_match_is_automatic_approval':False}
 return [matches[k] for k in sorted(matches)]
