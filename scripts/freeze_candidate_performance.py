"""Freeze baseline plus high-degree prediction/text cases before final measurement."""
import argparse
import json
import math
from pathlib import Path
from scripts.freeze_rebuilt_performance import freeze
from scripts.benchmark_explorer_performance import digest
from nutrigenomics.explorer_build import read_only
from nutrigenomics.source_updates import atomic_json

def freeze_candidates(release,output,public=False):
    release,output=map(Path,(release,output))
    spec=freeze(release,output,public)
    def add(name,route,params=None,target=1):
        spec['cases'].append({'id':name,'method':'GET','path':'/api/v1/explorer/'+route,
            'params':params or {},'target_s':target})
    with read_only(release/('nutriomics.sqlite3' if public else 'explorer.sqlite')) as db:
        high=db.execute("SELECT object,count(*) n FROM edge WHERE tier='predicted' GROUP BY object ORDER BY n DESC,object LIMIT 1").fetchone()
        seed=high['object']
        edge=db.execute("SELECT id,subject FROM edge WHERE tier='predicted' ORDER BY id LIMIT 1").fetchone()
        total=db.execute("SELECT count(*) FROM workbench_evidence WHERE layer='regulatory_predicted'").fetchone()[0]
        add('predicted-gene-search','entities',{'kind':'gene','q':seed.split(':',1)[-1]})
        add('predicted-region-search','entities',{'kind':'regulatory_region','q':edge['subject'].split(':',1)[-1]})
        add('predicted-high-degree-graph','graph',{'seed_id':seed})
        add('predicted-maximum-graph','graph',{'seed_id':seed,'max_nodes':250,'max_edges':750},3)
        add('predicted-edge-detail','edges/'+edge['id'])
        add('predicted-tissue-filter','evidence',{'seed_id':seed,'layer':'regulatory_predicted','tissue':'K562'})
        for label,page in [('first',1),('middle',max(1,math.ceil(total/25)//2)),('last',math.ceil(total/25))]:
            add('predicted-evidence-'+label,'evidence',{'layer':'regulatory_predicted','page':page,'page_size':25})
        lit=db.execute("SELECT id,subject FROM edge WHERE tier='extracted' ORDER BY id LIMIT 1").fetchone()
        if lit:
            add('literature-explicit-search','entities',{'kind':'food','tier':'extracted','include_extracted':True})
            add('literature-candidate-graph','graph',{'seed_id':lit['subject'],'include_extracted':True,'tier':'extracted'})
            add('literature-default-exclusion','graph',{'seed_id':lit['subject'],'include_extracted':False})
            add('literature-edge-detail','edges/'+lit['id'])
            add('literature-candidate-evidence','evidence',{'layer':'literature_candidate'})
        else:
            add('public-literature-withheld','evidence',{'layer':'literature_candidate'})
        add('candidate-document-register','candidate-documents',{'page':1,'page_size':25})
        spec['candidate_scope']={'prediction_rows':total,'prediction_high_degree_seed':seed,
            'prediction_high_degree':high['n'],'literature_present':bool(lit),'expert_approved':0}
    spec['case_sha256']=digest(spec['cases'])
    atomic_json(output,spec);return spec

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--public',action='store_true');a=p.parse_args()
    result=freeze_candidates(a.release,a.output,a.public)
    print(json.dumps({'cases':len(result['cases']),'case_sha256':result['case_sha256']}))
