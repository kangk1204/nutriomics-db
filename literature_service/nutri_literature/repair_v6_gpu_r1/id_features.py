"""One source-only menu and feature definition for ID training and inference."""
import math
from functools import lru_cache
from .common import POLICY, exact
from .text import definitions, normal
from .knowledge import namekey

MENTION_NAMES=('gene','ner_score','source_ner','source_alias','source_abbreviation','short3',
               'upper','digits','tokens','generic','left_inside_word','right_inside_word',
               'species_unique','species_unknown','species_mixed','abbreviation_defined')
PAIR_NAMES=('ID_bias','NIL_bias','rank','lexical_rank','dense_rank','exact_retrieval',
            'old_ID','alias_ratio','alias_exact','alias_case_exact','alias_token_overlap',
            'description_context_overlap','species_match','species_mismatch','species_supported',
            'chemical_category','gene_alias_exact','chemical_alias_exact','abbreviation_alias_exact')
FEATURES=PAIR_NAMES+tuple('NIL_'+n for n in MENTION_NAMES)+tuple('ID_'+n for n in MENTION_NAMES)
GENERIC={'expression','protein','proteins','receptor','receptors','activity','level','levels','cells','results','study','patients','significant'}


def menu(mention,kb):
    # The original source-only ID competes within the same fifty slots; no truth injection.
    identifiers=list(dict.fromkeys(mention['candidate_ids']))[:POLICY['id_candidate_limit']]
    old=mention.get('previous_id')
    if old and old not in identifiers:identifiers=identifiers[:POLICY['id_candidate_limit']-1]+[old]
    valid=[(identifier,kb.by_id(mention['kind'],identifier)) for identifier in identifiers]
    return [(None,None)]+[(identifier,r) for identifier,r in valid if r is not None]


class Builder:
    def __init__(self,kb):self.kb=kb

    @lru_cache(maxsize=80000)
    def aliases(self,kind,identifier,surface):
        from rapidfuzz.fuzz import ratio
        record=self.kb.by_id(kind,identifier);names=record['names'];key=namekey(surface);tokens=set(key.split())
        best=case=overlap=0.
        for n in names:
            k=namekey(n);best=max(best,ratio(key,k)/100);case=max(case,float(surface==n))
            words=set(k.split());overlap=max(overlap,len(tokens&words)/max(1,len(tokens|words)))
        return best,float(any(key==namekey(n) for n in names)),case,overlap,frozenset(namekey(record.get('description','')).split())

    def encode(self,document,mention):
        text=document['text'];m=mention
        if not exact(text,m):raise ValueError('ID 특징의 원문 개체 위치 불일치')
        candidates=menu(m,self.kb);surface=m['text'];aliases=definitions(text).get(normal(surface),set())
        taxa=m.get('supported_taxa',[]);source=m.get('source','');gene=m['kind']=='gene'
        base=[float(gene),float(m.get('ner_score',0.)),float(source=='independent-ner'),
              float('alias' in source),float('abbreviation' in source),float(len(surface)<=3),
              sum(c.isupper() for c in surface)/max(1,len(surface)),sum(c.isdigit() for c in surface)/max(1,len(surface)),
              min(len(surface.split()),8)/8,float(normal(surface) in GENERIC),
              float(m['start']>0 and text[m['start']-1].isalnum()),float(m['end']<len(text) and text[m['end']].isalnum()),
              float(len(taxa)==1),float(not taxa),float(len(taxa)>1),float(bool(aliases))]
        lex={v:i for i,v in enumerate(m.get('lexical_ids',[]))};dense={v:i for i,v in enumerate(m.get('dense_ids',[]))}
        context=set(namekey(text[max(0,m['start']-180):m['start']]+' '+text[m['end']:m['end']+180]).split())-set(namekey(surface).split())
        rows=[]
        for i,(identifier,record) in enumerate(candidates):
            if identifier is None:rows.append([0.,1.]+[0.]*(len(PAIR_NAMES)-2)+base+[0.]*len(base));continue
            similarity,identical,case,overlap,words=self.aliases(m['kind'],identifier,surface)
            expansion=float(any(namekey(n) in aliases for n in record['names']))
            tax=record.get('taxon');match=gene and len(taxa)==1 and tax in taxa
            chemical=not gene and identifier in (self.kb.chemical_categories or set())
            pair=[1.,0.,math.log1p(i)/math.log(52),1/(1+lex[identifier]) if identifier in lex else 0.,
                  1/(1+dense[identifier]) if identifier in dense else 0.,float(identifier in m.get('exact_ids',[])),
                  float(identifier==m.get('previous_id')),similarity,identical,case,overlap,
                  len(context&words)/max(1,len(words)),float(match),float(gene and len(taxa)==1 and not match),
                  float(gene and tax in POLICY['supported_taxa']),float(chemical),float(gene)*identical,float(not gene)*identical,expansion]
            rows.append(pair+[0.]*len(base)+base)
        return candidates,rows
