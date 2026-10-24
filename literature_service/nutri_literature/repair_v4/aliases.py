"""Full official aliases for identity features; display truncation never changes identity."""
from difflib import SequenceMatcher
from functools import lru_cache
import re
from .common import *
from ..chatgpt_grounding import Catalog,key,definitions,species
from ..repair_v3.identity import Retriever

class CanonicalRetriever(Retriever):
    @lru_cache(maxsize=60000)
    def full_names(self,kind,identifier):
        return tuple(sorted({n for record in self.by_id.get((kind,identifier),[]) for n in record['names']}))

    @lru_cache(maxsize=200000)
    def matching(self,kind,identifier,surface,expansions):
        names=self.full_names(kind,identifier)
        normalized={key(n) for n in names};expansion=set(expansions)
        exact=key(surface) in normalized
        return (float(exact),float(surface in names),float(bool(expansion&normalized)),
                1. if exact else max((SequenceMatcher(None,key(surface),n).ratio() for n in normalized),default=0.))

    def enrich(self,m,source):
        expansion={key(v['long']) for v in definitions(source).get(m['text'],[])}
        local=source[max(0,m['start']-140):m['end']+140]
        context=set(re.findall(r'\w+',key(local)))-set(re.findall(r'\w+',key(m['text'])))
        for r in m['candidates']:
            names=set(self.full_names(m['kind'],r['native_id']))
            if names:
                exact,case,expanded,similarity=self.matching(m['kind'],r['native_id'],m['text'],tuple(sorted(expansion)))
            else:
                names=set(r.get('names',[]));normalized={key(n) for n in names}
                exact=float(key(m['text']) in normalized);case=float(m['text'] in names)
                expanded=float(bool(expansion&normalized))
                similarity=max((SequenceMatcher(None,key(m['text']),n).ratio() for n in normalized),default=0.)
            normalized={key(n) for n in names}
            r['features']={k:v for k,v in r.get('features',{}).items() if k not in ('old_id','historical')}
            r['features'].update(exact_alias=exact,exact_case=case,expansion=expanded,ngram_similarity=similarity,
                context_overlap=max((len(set(re.findall(r'\w+',n))&context)/max(1,len(n.split())) for n in normalized),default=0.))
            # Put the actual matched aliases first; retain scientific names without pretending fragments are names.
            ordered=sorted(names,key=lambda n:(key(n)!=key(m['text']),key(n) not in expansion,len(n),n))
            r['names']=ordered[:6]
            r['official_alias_count']=len(names)
            r['official_aliases_sha256']=sha(canonical(sorted(names)))
        return m

    def retrieve(self,mention,source,old_ids):
        rows=super().retrieve(mention,source,old_ids)
        return self.enrich({**mention,'candidates':rows},source)['candidates']

def load_retriever():
    return CanonicalRetriever(Catalog.load(ROOT/'runtime/chatgpt-inputs/official-id-dictionary.json.gz'))

def prepare_canonical():
    retriever=load_retriever();cohort=read(HOME/'cohort.json');artifacts={}
    for split,ids in [('train',cohort['pmids']),('development',[d['pmid'] for d in data('development')])]:
        documents={d['pmid']:d for d in data(split)}
        for pmid in ids:
            p=read(V3/'rebuilt'/split/('paper-'+pmid+'.json'));p['document']=documents[pmid]
            for m in p['mentions']:retriever.enrich(m,p['document']['text'])
            value=payload(p);value['id_menu_profile']='canonical-full-official-aliases-v2'
            name='payloads-canonical/'+split+'/paper-'+pmid+'.json';write(name,value)
            artifacts[name]=sha((HOME/name).read_bytes())
            name='canonical-mentions/'+split+'/paper-'+pmid+'.json'
            write(name,{'source_sha256':p['source_sha256'],'mentions':p['mentions'],'dictionary_sha256':retriever.catalog.sha256})
            artifacts[name]=sha((HOME/name).read_bytes())
        log('canonical-inputs',split=split,papers=len(ids))
    legacy=[*sorted((HOME/'inference').glob('*/*/*/*.json')),*sorted((HOME/'payloads').glob('*/*.json'))]
    write('canonical-inputs.json',{'schema':'repair-v4-canonical-menu-revision','dictionary_sha256':retriever.catalog.sha256,
        'artifacts_sha256':artifacts,'legacy_technical_artifacts_sha256':{str(p.relative_to(HOME)):sha(p.read_bytes()) for p in legacy},
        'source_aliases_used_for_features':'all official aliases, before display selection',
        'old_responses_not_relabelled_as_new_prompt_or_menu':True,
        'budget':budget_status(),'frozen_before_fresh_training_requests':True})
