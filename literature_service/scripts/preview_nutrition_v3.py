"""Source-only v3 preview on development nutrition papers; no heldout analysis."""
import argparse
import json
from pathlib import Path

from nutri_literature.benchmarks import _receipt, _write_immutable, verify_receipt
from nutri_literature.chemical_v3 import load_snapshot
from nutri_literature.db import digest, dumps, now, uid
from nutri_literature.dictionaries import load_dictionaries
from nutri_literature.engines import validate_source_spans
from nutri_literature.evaluation import _load_inputs
from nutri_literature.extraction_v3 import RULES_VERSION, extract
from nutri_literature.settings import Settings


def select_development_preview(documents,limit=20):
    values=sorted((d for d in documents if d['split']=='development'),key=lambda d:digest('nutrition-v3-preview:'+d['pmid']))
    topics=sorted({d['primary_topic'] for d in values})
    if limit<len(topics) or not topics:
        raise ValueError('Preview must include all development topics')
    chosen=[]
    for topic in topics:
        chosen += [d for d in values if d['primary_topic']==topic][:max(1,limit//len(topics))]
    chosen += [d for d in values if d not in chosen][:max(0,limit-len(chosen))]
    return chosen[:limit]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs',type=Path)
    parser.add_argument('--lexicon',type=Path,required=True)
    parser.add_argument('--limit',type=int,default=20)
    args=parser.parse_args()
    settings=Settings.from_env()
    inputs,documents,input_sha=_load_inputs(settings,args.inputs)
    selected=select_development_preview(list(documents.values()),args.limit)
    dictionaries=load_dictionaries(settings,engine='heuristic-v2')
    lexicon=load_snapshot(args.lexicon)
    identity={'input_sha256':input_sha,'rules_version':RULES_VERSION,'dictionary':dictionaries.version,
              'chemical_sha256':lexicon['manifest']['snapshot_sha256'],'pmids':[d['pmid'] for d in selected]}
    root=settings.home/'experiments/v3/nutrition'/digest(dumps(identity))
    root.mkdir(parents=True,exist_ok=True)
    cases=[]
    for doc in selected:
        case_id=digest(dumps({'identity':identity,'pmid':doc['pmid']}))
        path=root/(case_id+'.json')
        if path.exists():
            case=json.loads(path.read_bytes())
            if not verify_receipt(settings,'nutrition_v3_preview_case',case):
                raise ValueError('Nutrition preview receipt changed')
        else:
            result=extract(doc['text'],doc['sections'],dictionaries,{**doc['metadata'],'_chemical_lexicon':lexicon})
            validate_source_spans(result,doc['text'])
            case={'schema_version':'literature-nutrition-v3-preview-case-v1','id':case_id,'at':now(),
                  'pmid':doc['pmid'],'paper_id':doc['metadata']['paper_id'],'title':doc['title'],
                  'topic':doc['primary_topic'],'split':'development','source_hash':doc['source_hash'],
                  'source_url':doc['source_url'],'source_level':doc['source_level'],'input_sha256':input_sha,
                  'rules_version':RULES_VERSION,'result':result,'promotion_eligible':False,
                  'source_spans_valid':True,'nutrition_accuracy':'unmeasured','review_status':'unreviewed'}
            _write_immutable(path,case)
            _receipt(settings,'nutrition_v3_preview_case',case_id,case)
        cases.append({k:case[k] for k in ['id','pmid','paper_id','title','topic','source_hash','source_level','review_status']})
        cases[-1].update({'path':str(path.relative_to(settings.home)),'status':case['result']['status'],
                         'events':len(case['result']['events']),'abstentions':len(case['result']['abstentions'])})
    index={'schema_version':'literature-nutrition-v3-preview-index-v1','id':uid(),'at':now(),'identity':identity,
           'split':'development','items':cases,'promotion_eligible':False,'sealed_test_reused':False,
           'nutrition_accuracy':'unmeasured','review_time_saved':'unmeasured'}
    _receipt(settings,'nutrition_v3_preview_index',index['id'],index)
    temporary=settings.home/'experiments/v3/nutrition-preview.tmp'
    temporary.write_text(dumps(index),encoding='utf-8')
    temporary.replace(settings.home/'experiments/v3/nutrition-preview.json')
    print(dumps({'preview_papers':len(cases),'topics':sorted({c['topic'] for c in cases}),
                 'completed':sum(c['status']=='complete' for c in cases),'index':str(settings.home/'experiments/v3/nutrition-preview.json')}),flush=True)


if __name__=='__main__':
    main()
