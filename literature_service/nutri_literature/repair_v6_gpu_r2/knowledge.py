"""Official identity records only. No database relationship becomes a model label."""
import csv,gzip,json,re,sqlite3,unicodedata
from functools import lru_cache
from .common import *
from .text import species,definitions,normal

GREEK=str.maketrans({'α':' alpha ','β':' beta ','γ':' gamma ','δ':' delta ','κ':' kappa ','μ':' mu ','ω':' omega '})
def namekey(s):return normal(unicodedata.normalize('NFKC',s).translate(GREEK))
def forms(s):
 s=namekey(s);return sorted({s,s.replace(' ',''),' '.join(sorted(s.split()))}-{''})
def download(url,path):
 import requests,time
 path=Path(path);receipt=path.with_suffix(path.suffix+'.source.json')
 if receipt.exists():
  v=read(receipt)
  if not path.exists() or filehash(path)!=v['sha256']:raise ValueError('공식 자료 캐시 변경')
  return v
 path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix(path.suffix+'.part')
 for attempt in range(3):
  try:
   with requests.get(url,stream=True,timeout=(20,180)) as response:
    response.raise_for_status()
    with temp.open('wb') as f:
     for block in response.iter_content(2**20):f.write(block)
     f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    v={'url':url,'sha256':filehash(path),'bytes':path.stat().st_size,'etag':response.headers.get('ETag'),
     'last_modified':response.headers.get('Last-Modified'),'retrieved_unix':time.time()};atomic(receipt,v,True);return v
  except requests.RequestException:
   if attempt==2:raise
   time.sleep(2**attempt)

class Knowledge:
 def __init__(self,home=HOME):
  self.home=home;self.base=sqlite3.connect((home/'aliases.sqlite3').resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
  self.base.row_factory=sqlite3.Row;self.base.execute('PRAGMA cache_size=-32000')
  self.overlay=sqlite3.connect((home/'knowledge/overlay.sqlite3').resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
  self.overlay.row_factory=sqlite3.Row;self.overlay.execute('PRAGMA cache_size=-16000')
  self.chemical_categories=set(read(home/'knowledge/chemical-categories.json')['chemical_ids']) if (home/'knowledge/chemical-categories.json').exists() else None
 def close(self):self.by_id.cache_clear();self.lexical.cache_clear();self.base.close();self.overlay.close()
 @lru_cache(maxsize=12000)
 def by_id(self,kind,identifier):
  out=[]
  for db in (self.base,self.overlay):
   for r in db.execute('SELECT * FROM records WHERE kind=? AND native_id=?',(kind,str(identifier))):
    v=dict(r);v['names']=json.loads(v['names']);out.append(v)
  if not out:return None
  best=out[-1];best['names']=sorted(set(n for r in out for n in r['names']));best['description']=best.get('description') or '; '.join(best['names'][:4]);return best
 @lru_cache(maxsize=12000)
 def lexical(self,kind,surface,expansions=()):
  from rapidfuzz.fuzz import WRatio,token_set_ratio
  result={};terms=(surface,*expansions)
  for db in (self.base,self.overlay):
   found=set()
   for term in terms:
    for form in forms(term):found.update(r[0] for r in db.execute('SELECT rid FROM aliases WHERE kind=? AND norm=?',(kind,form)))
   words=sorted({w for t in terms for w in namekey(t).split() if len(w)>2 and w not in {'the','and','gene','protein','human','mouse','rat'}},key=lambda w:(-len(w),w))[:6]
   if words:
    query=' OR '.join('"'+w+'"' for w in words)
    found.update(r[0] for r in db.execute('SELECT words.rowid FROM words JOIN records r ON r.rid=words.rowid WHERE words MATCH ? AND r.kind=? ORDER BY rank LIMIT 500',(query,kind)))
   compact=namekey(surface).replace(' ','');grams=sorted({compact[i:i+3] for i in range(len(compact)-2)})
   if grams:
    marks=','.join('?'*len(grams));rare=[r[0] for r in db.execute('SELECT term FROM gram_vocab WHERE term IN ('+marks+') AND doc>0 ORDER BY doc LIMIT 6',grams)]
    if rare:
     query=' OR '.join('"'+w+'"' for w in rare)
     found.update(r[0] for r in db.execute('SELECT grams.rowid FROM grams JOIN records r ON r.rid=grams.rowid WHERE grams MATCH ? AND r.kind=? ORDER BY rank LIMIT 400',(query,kind)))
   for rid in found:
    r=dict(db.execute('SELECT * FROM records WHERE rid=?',(rid,)).fetchone());names=json.loads(r['names'])
    score=max(max(WRatio(namekey(t),namekey(n)),token_set_ratio(namekey(t),namekey(n))) for t in terms for n in names)/100
    exact=max(int(bool(set(forms(t))&set(forms(n)))) for t in terms for n in names)
    old=result.get(r['native_id'])
    if old is None or (exact,score)>old[:2]:result[r['native_id']]=(exact,score)
  return tuple((identifier,*score) for identifier,score in sorted(result.items(),key=lambda v:(-v[1][0],-v[1][1],v[0]))[:200])
 def cell_species(self,text):
  tokens=list(re.finditer(r'[\w-]+',text));candidates={}
  for i in range(len(tokens)):
   for j in range(i,min(i+5,len(tokens))):
    a,b=tokens[i].start(),tokens[j].end();s=text[a:b]
    if len(s)>=3:candidates.setdefault(namekey(s),(a,b,s))
  found=[];keys=list(candidates)
  for i in range(0,len(keys),300):
   chunk=keys[i:i+300]
   for r in self.overlay.execute('SELECT name,taxon,accession FROM cells WHERE name IN ('+','.join('?'*len(chunk))+')',chunk):
    a,b,s=candidates[r[0]];found.append({'taxon':r[1],'cell_id':r[2],'start':a,'end':b,'text':s})
  return found
 def candidate_text(self,record,surface=None):
  tax={9606:'Homo sapiens human',10090:'Mus musculus mouse',10116:'Rattus norvegicus rat'}.get(record.get('taxon'),'organism unknown')
  # Native IDs are keys/targets only, never token features.
  names=record['names']
  if surface:
   from rapidfuzz.fuzz import WRatio
   names=sorted(names,key=lambda n:(-WRatio(namekey(surface),namekey(n)),len(n),n))
  category=''
  if surface and record['kind']=='chemical' and getattr(self,'chemical_categories',None) is not None:
   category='; MeSH chemical-category confirmed' if record['native_id'] in self.chemical_categories else '; MeSH chemical category unconfirmed; benchmark annotation may differ'
  return record['kind']+'; '+tax+'; '+'; '.join(names[:8])+'; '+record.get('description','')+category

def build():
 home=HOME/'knowledge';receipt=home/'manifest.json'
 if receipt.exists():
  v=read(receipt)
  if filehash(HOME/'aliases.sqlite3')!=v['base_sha256'] or filehash(home/'overlay.sqlite3')!=v['overlay_sha256']:raise ValueError('지식베이스 변경')
  return v
 home.mkdir(parents=True,exist_ok=True);path=home/'overlay.sqlite3'
 if path.exists():path.rename(home/('overlay.incomplete.'+str(time.time_ns())+'.sqlite3'))
 db=sqlite3.connect(path);db.executescript('''CREATE TABLE records(rid INTEGER PRIMARY KEY,kind TEXT,native_id TEXT,taxon INTEGER,names TEXT,source TEXT,current_id TEXT,description TEXT);
 CREATE INDEX ids ON records(kind,native_id);CREATE TABLE aliases(norm TEXT,kind TEXT,rid INTEGER,UNIQUE(norm,kind,rid));CREATE INDEX alias_lookup ON aliases(kind,norm);
 CREATE VIRTUAL TABLE words USING fts5(text,content='');CREATE VIRTUAL TABLE grams USING fts5(text,content='',tokenize='trigram');CREATE VIRTUAL TABLE gram_vocab USING fts5vocab(grams,'row');
 CREATE TABLE cells(name TEXT,taxon INTEGER,accession TEXT,UNIQUE(name,taxon,accession));CREATE INDEX cell_lookup ON cells(name);''')
 sources=[];count=0
 for species_name in ('Homo_sapiens','Mus_musculus','Rattus_norvegicus'):
  url='https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/'+species_name+'.gene_info.gz';p=home/(species_name+'.gene_info.gz');sources.append(download(url,p))
  with gzip.open(p,'rt',encoding='utf8') as f:
   for row in csv.reader(f,delimiter='\t'):
    if not row or row[0].startswith('#') or len(row)<14:continue
    names=sorted({row[2],row[8],row[10],row[11],*row[4].split('|'),*row[13].split('|')}-{'-',''})
    cur=db.execute('INSERT INTO records(kind,native_id,taxon,names,source,description) VALUES(?,?,?,?,?,?)',('gene',row[1],int(row[0]),canonical(names),'NCBI-gene-info',row[8]));rid=cur.lastrowid
    keys={v for n in names for v in forms(n)};db.executemany('INSERT OR IGNORE INTO aliases VALUES(?,?,?)',[(v,'gene',rid) for v in keys])
    value=' '.join(namekey(n) for n in names);db.execute('INSERT INTO words(rowid,text) VALUES(?,?)',(rid,value));db.execute('INSERT INTO grams(rowid,text) VALUES(?,?)',(rid,value.replace(' ','')));count+=1
    if count%10000==0:db.commit();log('kb-gene-records',records=count)
 db.commit()
 hgnc=home/'hgnc.tsv';sources.append(download('https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt',hgnc));hgnc_count=0
 with hgnc.open(encoding='utf8') as f:
  for r in csv.DictReader(f,delimiter='\t'):
   if r.get('status')!='Approved' or not r.get('entrez_id'):continue
   names=sorted({r['symbol'],r.get('name',''),*r.get('alias_symbol','').split('|'),*r.get('prev_symbol','').split('|'),*r.get('alias_name','').split('|')}-{''})
   cur=db.execute('INSERT INTO records(kind,native_id,taxon,names,source,description) VALUES(?,?,?,?,?,?)',('gene',r['entrez_id'],9606,canonical(names),'HGNC-approved',r.get('name','')));rid=cur.lastrowid
   db.executemany('INSERT OR IGNORE INTO aliases VALUES(?,?,?)',[(v,'gene',rid) for v in {v for n in names for v in forms(n)}])
   value=' '.join(namekey(n) for n in names);db.execute('INSERT INTO words(rowid,text) VALUES(?,?)',(rid,value));db.execute('INSERT INTO grams(rowid,text) VALUES(?,?)',(rid,value.replace(' ','')));hgnc_count+=1
 db.commit()
 # The ontology remains separate: parent, role and cross-reference edges are NOT identity edges.
 db.executescript('CREATE TABLE ontology(identifier TEXT PRIMARY KEY,name TEXT,definition TEXT,parents TEXT,xrefs TEXT);CREATE INDEX ontology_name ON ontology(name);')
 chebi=home/'chebi.obo.gz';sources.append(download('https://ftp.ebi.ac.uk/pub/databases/chebi/ontology/chebi.obo.gz',chebi));term={};ontology_count=0
 def insert_term(term):
  nonlocal ontology_count
  if term.get('id') and not term.get('obsolete'):
   db.execute('INSERT OR REPLACE INTO ontology VALUES(?,?,?,?,?)',(term['id'],term.get('name',''),term.get('def',''),canonical(term.get('parents',[])),canonical(term.get('xrefs',[]))));ontology_count+=1
 with gzip.open(chebi,'rt',encoding='utf8') as f:
  for line in f:
   line=line.rstrip()
   if line=='[Term]':insert_term(term);term={}
   elif line.startswith('id: '):term['id']=line[4:]
   elif line.startswith('name: '):term['name']=line[6:]
   elif line.startswith('def: '):term['def']=line[5:]
   elif line.startswith('is_a: '):term.setdefault('parents',[]).append(line[6:].split(' ! ')[0])
   elif line.startswith('xref: '):term.setdefault('xrefs',[]).append(line[6:])
   elif line=='is_obsolete: true':term['obsolete']=True
 insert_term(term);db.commit()
 cells=home/'cellosaurus.txt';sources.append(download('https://ftp.expasy.org/databases/cellosaurus/cellosaurus.txt',cells));record={};cell_count=0
 def insert_cell(record):
  nonlocal cell_count
  for taxon in record.get('taxa',[]):
   for name in record.get('names',[]):
    if len(name)>=3:db.execute('INSERT OR IGNORE INTO cells VALUES(?,?,?)',(namekey(name),taxon,record.get('accession')));cell_count+=1
 with cells.open(encoding='utf8') as f:
  for line in f:
   if line.startswith('ID   '):record={'names':[line[5:].strip()]}
   elif line.startswith('AC   '):record['accession']=line[5:].strip()
   elif line.startswith('SY   '):record.setdefault('names',[]).extend(line[5:].strip().split('; '))
   elif line.startswith('OX   '):record.setdefault('taxa',[]).extend(int(v) for v in re.findall(r'NCBI_TaxID=(\d+)',line))
   elif line.startswith('//'):insert_cell(record);record={}
 db.commit();db.close()
 v={'base_sha256':filehash(HOME/'aliases.sqlite3'),'overlay_sha256':filehash(path),'gene_records':count,'hgnc_records':hgnc_count,'chebi_records':ontology_count,'cell_alias_records':cell_count,
  'sources':sources,'MeSH':'pinned full D/C alias index; raw IDs retained','external_relationships_used':False,
  'Cellosaurus_license':'CC-BY-4.0','source_sha256':source_hashes()}
 atomic(receipt,v,True);return v
