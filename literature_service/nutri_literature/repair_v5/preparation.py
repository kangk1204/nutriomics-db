import copy,json
from collections import Counter
from .common import *
from .retrieval import Search,build_index
from ..repair_v4 import learning as previous

def make_payload(p):
    from ..text import sentence_spans
    source=p['document']['text']
    for cap in (20,10,5):
        value={'source':source,'source_sha256':sha(source),'schema_version':'repair-v5-source',
            'sentences':[{'key':'s'+str(i),**s} for i,s in enumerate(sentence_spans(source))],
            'mentions':[{**{k:m[k] for k in ('key','kind','text','start','end')},
                'candidates':[{k:r.get(k) for k in ('native_id','taxon','names')} for r in sorted(m['candidates'][:cap],key=lambda r:r['native_id'])]} for m in p['mentions']],
            'id_menu_limit':cap,'internal_candidate_limit':200,'gold_annotations_provided':False,'prior_answers_supplied':False}
        if len(canonical(value).encode())<=180000:return value
    if len(canonical(value).encode())>350000:raise ValueError('입력 크기 상한 초과; 임의 문헌 제외 금지')
    return value

def prepare():
    from ..repair_v4.pipeline import verify_freeze
    verify_freeze();build_index()
    if not (HOME/'audit.json').exists():
        comp=read(V4/'comparison.json')
        for arm,tp,fp,fn in [('identity_only',65,63,89),('v2',64,64,90),('v3',63,57,91),('combined',50,54,104)]:
            assert tuple(comp['arms'][arm]['metrics'][k] for k in ('tp','fp','fn'))==(tp,fp,fn)
        write('audit.json',{'budget_at_start':budget_status(),'old_comparison_sha256':filehash(V4/'comparison.json'),
            'previous_freeze_sha256':filehash(V4/'freeze.json'),'source_sha256':sources(),
            'goals':{'tp':65,'recall':FLOOR,'precision':.85},'sealed_test_opened':False,'default_engine':'heuristic-v1',
            'upstream_history':'v2 fixed corrector had training exposure; development repeatedly exposed',
            'baseline_counts':{a:comp['arms'][a]['metrics'] for a in ('identity_only','v2','v3','combined')}})
    search=Search();legacy_policy=read(V4/'selection.json')['model']['identity'];ranks={};artifacts={}
    for split in ('train','development'):
        rank=[]
        for i,p in enumerate(cached(split)):
            path=HOME/'rebuilt'/split/('paper-'+p['pmid']+'.json')
            if path.exists():value=read(path)
            else:
                rawbase=baseline(p['pmid'],split)
                score=Counter()
                for m in p['mentions']:
                    previous_choice=previous.choose(m,legacy_policy)
                    old_menu={r['native_id'] for r in m['candidates']}
                    probe={**m,'old_ids':list(set(m['old_ids'])|({previous_choice['selected_id']} if previous_choice['selected_id'] else set()))}
                    rows=search.retrieve(probe,p['document']['text']);m['candidate_pool200']=[r['native_id'] for r in rows]
                    m['candidates']=rows[:50]
                    score['changed_menu']+=int(bool(set(m['candidate_pool200'])-old_menu))
                    score['nil']+=int(previous_choice['selected_id'] is None)
                    votes={r.get('selected_id') for r in m['reviews'].values()}
                    score['model_id_disagreement']+=int(len(votes)>1)
                score['uncertain_events']=sum(e['decision']!='supported' or not e['supported_types'] for e in p['events'])
                p['targeting_features']=dict(score);p['baseline_raw']=rawbase
                value={k:v for k,v in p.items() if k not in ('document','sample_weight')}
                value['mentions']=[{k:v for k,v in m.items() if k!='_source'} for m in p['mentions']]
                write('rebuilt/'+split+'/paper-'+p['pmid']+'.json',value)
            score=value['targeting_features']
            priority=3*min(score.get('changed_menu',0),10)+2*min(score.get('model_id_disagreement',0),10)+min(score.get('nil',0),10)+min(score.get('uncertain_events',0),10)
            rank.append({'pmid':p['pmid'],'priority':priority,'features':score,'tie':sha(str(SEED)+p['pmid'])})
            artifacts[str(path.relative_to(HOME))]=filehash(path)
            if i%20==0:log('rebuild-progress',split=split,papers=i+1)
        ranks[split]=sorted(rank,key=lambda r:(-r['priority'],r['tie']))
    search.close()
    selected={s:[r['pmid'] for r in ranks[s][:n]] for s,n in [('train',8),('development',3)]}
    write('targeted-cohort.json',{**selected,'ranking':ranks,'source_only_selection':True,
        'no_known_errors_or_gold_ids_used':True,'limits':{'train':16,'development':6,'retry':3}})
    for split,ids in selected.items():
        docs={d['pmid']:d for d in data(split)}
        for pmid in ids:
            p=read(HOME/'rebuilt'/split/('paper-'+pmid+'.json'));p['document']=docs[pmid]
            write('payloads/'+split+'/paper-'+pmid+'.json',make_payload(p))
    write('prepared.json',{'artifacts':artifacts,'index_sha256':read(HOME/'knowledge/index.json')['index_sha256'],
        'targeted_pmids':selected,'frozen_v4_responses_preserved':True,'new_requests_so_far':budget_status()['attempts']-read(HOME/'audit.json')['budget_at_start']['attempts']})
    log('prepared',selected=selected,budget=budget_status())
