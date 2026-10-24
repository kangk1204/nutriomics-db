"""Streaming disk index over every official alias; historical identity is explicit."""
import gzip,json,re,sqlite3,unicodedata
from functools import lru_cache
import ijson
from rapidfuzz.fuzz import WRatio,ratio,token_set_ratio
from .common import *
from ..chatgpt_grounding import definitions,species,key as original_key

key=lru_cache(maxsize=100000)(original_key)

GREEK=str.maketrans({'α':' alpha ','β':' beta ','γ':' gamma ','δ':' delta ','κ':' kappa ','μ':' mu ','ω':' omega '})
SUPPORTED={9606,10090,10116}

@lru_cache(maxsize=100000)
def normal(value):
    value=unicodedata.normalize('NFKC',value).casefold().translate(GREEK)
    value=re.sub(r'(?<=[a-z])\((\d+)\)',r'\1',value)
    words=re.sub('[^a-z0-9]+',' ',value).split()
    return ' '.join(w[:-1] if len(w)>5 and w.endswith('receptors') else w for w in words)

@lru_cache(maxsize=100000)
def variants(value):
    n=normal(value)
    return {n,n.replace(' ',''),' '.join(sorted(n.split()))}-{''}

class Search:
    def __init__(self,path=None):
        self.path=Path(path or HOME/'knowledge/aliases.sqlite3')
        self.db=sqlite3.connect(str(self.path));self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA cache_size=-48000');self.db.execute('PRAGMA temp_store=FILE')

    def close(self):self.db.close()

    def create(self):
        self.db.executescript('''CREATE TABLE IF NOT EXISTS records(rid INTEGER PRIMARY KEY,kind TEXT,native_id TEXT,taxon INTEGER,names TEXT,source TEXT,current_id TEXT);
        CREATE INDEX IF NOT EXISTS ids ON records(kind,native_id);
        CREATE TABLE IF NOT EXISTS aliases(norm TEXT,kind TEXT,rid INTEGER,UNIQUE(norm,kind,rid));
        CREATE INDEX IF NOT EXISTS alias_lookup ON aliases(kind,norm);
        CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts5(text,content='');
        CREATE VIRTUAL TABLE IF NOT EXISTS grams USING fts5(text,content='',tokenize='trigram');
        CREATE VIRTUAL TABLE IF NOT EXISTS gram_vocab USING fts5vocab(grams,'row');''')

    def add(self,r,source='official-current',current_id=None):
        names=sorted({n for n in r['names'] if isinstance(n,str) and n})
        if not names:return
        cur=self.db.execute('INSERT INTO records(kind,native_id,taxon,names,source,current_id) VALUES(?,?,?,?,?,?)',
            (r['kind'],str(r['native_id']),r.get('taxon'),canonical(names),source,current_id))
        rid=cur.lastrowid;forms={v for n in names for v in variants(n)}
        self.db.executemany('INSERT OR IGNORE INTO aliases VALUES(?,?,?)',[(v,r['kind'],rid) for v in forms])
        self.db.execute('INSERT INTO words(rowid,text) VALUES(?,?)',(rid,' '.join(normal(n) for n in names)))
        self.db.execute('INSERT INTO grams(rowid,text) VALUES(?,?)',(rid,' '.join(normal(n).replace(' ','') for n in names)))

    @lru_cache(maxsize=12000)
    def record(self,rid):
        r=dict(self.db.execute('SELECT * FROM records WHERE rid=?',(rid,)).fetchone());r['names']=json.loads(r['names']);return r

    @lru_cache(maxsize=12000)
    def by_id(self,kind,identifier):
        return tuple(self.record(r[0]) for r in self.db.execute('SELECT rid FROM records WHERE kind=? AND native_id=?',(kind,identifier)))

    @lru_cache(maxsize=16000)
    def search(self,kind,surface,expansions):
        terms=(surface,*expansions);found=set()
        for term in terms:
            for v in variants(term):found.update(r[0] for r in self.db.execute('SELECT rid FROM aliases WHERE kind=? AND norm=?',(kind,v)))
        tokens=sorted({w for t in terms for w in normal(t).split() if len(w)>=3 and w not in {'the','and','protein','gene','human','mouse','rat'}},key=lambda s:(-len(s),s))[:8]
        if tokens:
            self.db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS temp.word_vocab USING fts5vocab(main,words,row)')
            marks=','.join('?' for _ in tokens)
            tokens=[r[0] for r in self.db.execute('SELECT term FROM temp.word_vocab WHERE term IN ('+marks+') AND doc>0 ORDER BY doc,term LIMIT 4',tokens)]
        if tokens:
            q=' OR '.join('"'+t+'"' for t in tokens)
            found.update(r[0] for r in self.db.execute('SELECT words.rowid FROM words JOIN records r ON r.rid=words.rowid WHERE words MATCH ? AND r.kind=? ORDER BY rank LIMIT 400',(q,kind)))
        compact=normal(surface).replace(' ','');gs=sorted({compact[i:i+3] for i in range(len(compact)-2)})
        if gs:
            # Ignore empty postings; every alias was indexed, without lexical truncation.
            marks=','.join('?' for _ in gs)
            rare=[r[0] for r in self.db.execute('SELECT term FROM gram_vocab WHERE term IN ('+marks+') AND doc>0 ORDER BY doc,term LIMIT 8',gs)]
            if rare:
                q=' OR '.join('"'+t+'"' for t in rare)
                found.update(r[0] for r in self.db.execute('SELECT grams.rowid FROM grams JOIN records r ON r.rid=grams.rowid WHERE grams MATCH ? AND r.kind=? ORDER BY rank LIMIT 500',(q,kind)))
        scored=[]
        for rid in found:
            r=self.record(rid);names=r['names']
            val=max(max(WRatio(normal(t),normal(n)),token_set_ratio(normal(t),normal(n))) for t in terms for n in names)/100
            exact=any(variants(t)&variants(n) for t in terms for n in names)
            scored.append((float(exact),val,rid))
        return tuple(rid for _,_,rid in sorted(scored,key=lambda z:(-z[0],-z[1],z[2]))[:600])

    def retrieve(self,m,source):
        expansion=tuple(v['long'] for v in definitions(source).get(m['text'],[]))
        rows={}
        for rid in self.search(m['kind'],m['text'],expansion):
            r=self.record(rid);rows.setdefault(r['native_id'],r)
        for identifier in m.get('old_ids',[]):
            for r in self.by_id(m['kind'],identifier):rows[identifier]=r
        # Previous candidates remain available even when their old record is absent.
        for r in m.get('candidates',[]):
            if r['native_id'] not in rows:rows[r['native_id']]={**r,'source':'prior-menu','current_id':None}
        near=source[max(0,m['start']-140):m['end']+140];local_tax=species(near);doc_tax=species(source)
        context=set(normal(near).split())-set(normal(m['text']).split());result=[]
        for identifier,r in rows.items():
            names=r['names'];taxon=r.get('taxon');s=m['text']
            exact=float(any(key(s)==key(n) for n in names));exp=float(any(key(t)==key(n) for t in expansion for n in names))
            normexact=float(any(variants(s)&variants(n) for n in names))
            similarity=max((ratio(normal(s),normal(n))/100 for n in names),default=0.)
            token=max((token_set_ratio(normal(s),normal(n))/100 for n in names),default=0.)
            f={'chemical':float(m['kind']=='chemical'),'exact_alias':exact,'exact_case':float(s in names),
                'expansion':exp,'normalized_exact':normexact,'ngram_similarity':similarity,'token_similarity':token,
                'context_overlap':max((len(set(normal(n).split())&context)/max(1,len(normal(n).split())) for n in names),default=0.),
                'local_tax_match':float(taxon in local_tax),'local_tax_conflict':float(m['kind']=='gene' and bool(local_tax) and taxon not in local_tax),
                'doc_tax_match':float(taxon in doc_tax),'doc_tax_conflict':float(m['kind']=='gene' and bool(doc_tax) and taxon not in doc_tax),
                'taxon_unknown':float(not doc_tax),'supported_species':float(m['kind']=='chemical' or taxon in SUPPORTED),
                'short':float(len(s)<=3),'candidate_ambiguity':0.,'nil':0.}
            for model,label in MODELS:
                rev=m.get('reviews',{}).get(model,{});k='sol' if 'sol' in model else 'luna'
                f['id_vote_'+k]=float(rev.get('selected_id')==identifier and rev.get('valid_id_evidence') is True)
                f['id_vote_eligible_'+k]=float(rev.get('selected_id')==identifier and rev.get('eligible') is True)
                f['id_nil_'+k]=float(rev.get('selected_id') is None)
            display=sorted(names,key=lambda n:(-(key(n)==key(s)),-token_set_ratio(normal(s),normal(n)),len(n),n))[:6]
            result.append({'native_id':identifier,'taxon':taxon,'names':display,'features':f,
                'retrieval_source':r.get('source'),'current_id':r.get('current_id'),'all_aliases_sha256':sha(canonical(names))})
        result.sort(key=lambda r:(-int(r['native_id'] in m.get('old_ids',[])),-r['features']['expansion'],-r['features']['normalized_exact'],
            -r['features']['local_tax_match'],r['features']['local_tax_conflict'],-r['features']['ngram_similarity'],-r['features']['token_similarity'],r['native_id']))
        import math
        for r in result:r['features']['candidate_ambiguity']=math.log1p(len(result))
        return result[:max(200,len(m.get('old_ids',[])))]

def build_index():
    path=HOME/'knowledge/aliases.sqlite3';receipt=HOME/'knowledge/index.json'
    if receipt.exists():
        assert filehash(path)==read(receipt)['index_sha256'];return
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():raise RuntimeError('부분 색인 보존; 새 인덱스 버전 필요')
    search=Search(path);search.create();count=0
    dictionary=ROOT/'runtime/chatgpt-inputs/official-id-dictionary.json.gz'
    with gzip.open(dictionary,'rb') as f:
        for record in ijson.items(f,'records.item'):
            search.add(record);count+=1
            if count%10000==0:search.db.commit()
            if count%100000==0:log('index-current',records=count)
    search.db.commit();hist=0
    gh=PRIOR/'knowledge/gene-history.json'
    with gh.open('rb') as f:
        for identifier,v in ijson.kvitems(f,''):
            if v.get('taxon') not in SUPPORTED or not v.get('previous_symbol') or not v.get('identity_proven'):continue
            if search.db.execute('SELECT 1 FROM records WHERE kind=? AND native_id=? LIMIT 1',('gene',identifier)).fetchone():continue
            current=v.get('current_id');current_rows=search.by_id('gene',str(current)) if current else ()
            names=[v['previous_symbol'],*[n for r in current_rows for n in r['names']]]
            search.add({'kind':'gene','native_id':identifier,'taxon':v['taxon'],'names':names},'official-gene-history',current);hist+=1
            if hist%10000==0:search.db.commit()
    mh=PRIOR/'knowledge/mesh-history.json'
    for v in read(mh)['records']:
        identifier=v.get('old_id');name=v.get('old_name')
        if identifier and name and not search.db.execute('SELECT 1 FROM records WHERE kind=? AND native_id=? LIMIT 1',('chemical',identifier)).fetchone():
            search.add({'kind':'chemical','native_id':identifier,'taxon':None,'names':[name]},'official-mesh-historical-name');hist+=1
    search.db.commit();counts=search.db.execute('SELECT count(*) FROM records').fetchone()[0]
    search.db.execute("INSERT INTO words(words) VALUES('optimize')");search.db.execute("INSERT INTO grams(grams) VALUES('optimize')");search.db.commit();search.close()
    write('knowledge/index.json',{'index_sha256':filehash(path),'records':counts,'historical_records':hist,
        'sources':{str(p.relative_to(ROOT)):sha(p.read_bytes()) for p in (dictionary,gh,mh)},
        'all_official_aliases_indexed':True,'historical_chemical_replacement_is_equality':False,'memory':'streamed ijson and disk FTS5'})
    log('index-complete',records=counts,historical=hist)
