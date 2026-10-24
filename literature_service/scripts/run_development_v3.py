"""Audited v3 development experiments; rejects all protected test manifests."""
import argparse
import hashlib
import json
from pathlib import Path

from nutri_literature import benchmarks as scoring
from nutri_literature.chemical_v3 import build_snapshot, load_snapshot
from nutri_literature.db import digest, dumps, now, uid
from nutri_literature.dictionaries import load_dictionaries
from nutri_literature.engines import validate_source_spans
from nutri_literature.extraction_v3 import RULES_VERSION, extract
from nutri_literature.extraction_v2 import RULES_VERSION as V2_RULES_VERSION
from nutri_literature.settings import Settings


def development_inputs(settings, corpus):
    index=json.loads((settings.home/'public_benchmarks/v2-measurement-index.json').read_bytes())
    prepared=index['steps']['prepare:development:'+corpus+':abstract']
    inputs,receipt,sha=scoring._load_inputs(settings,prepared['inputs_path'])
    if inputs['split'] != 'development' or receipt['split'] != 'development':
        raise ValueError('V3 experiments accept development data only; protected tests are prohibited')
    return index,prepared,inputs,receipt,sha


def select_documents(inputs,limit):
    if inputs['split'] != 'development':
        raise ValueError('Protected test prediction is prohibited')
    ordered=sorted(inputs['documents'],key=lambda d:digest('v3-development-20261003:'+d['pmid']))
    return ordered if limit is None else ordered[:limit]


def run(settings,corpus,mode,lexicon_path,limit=None):
    index,prepared,inputs,receipt,input_sha=development_inputs(settings,corpus)
    if V2_RULES_VERSION != 'rules-sha256:11b4f00cf76a1b56b5cc273bd150474e021b4ba400f274b8b168f6f9486eae66':
        raise ValueError('Frozen v2 scientific source changed')
    dictionaries=load_dictionaries(settings,engine='heuristic-v2')
    lexicon=load_snapshot(lexicon_path)
    mapping,_=scoring._crosswalk(settings,dictionaries)
    base_mapping={key:set(value) for key,value in mapping.items()}
    snapshot=json.loads(Path(lexicon_path).read_bytes())
    for record in snapshot['records']:
        mapping[('component',record['id'])]={record['native_id']}
    documents=select_documents(inputs,limit)
    baseline_entry=index['steps']['predict:development:'+corpus+':abstract:'+mode+':heuristic-v2']
    baseline_run=json.loads(Path(baseline_entry['prediction_run']).read_bytes())
    if baseline_run['split'] != 'development' or not scoring.verify_receipt(settings,'public_benchmark_prediction',baseline_run):
        raise ValueError('Baseline must be an audited unchanged development prediction')
    baseline={d['pmid']:d for d in baseline_run['documents']}
    oracle={}
    if mode=='gold_entities':
        raw=Path(prepared['inputs_path']).with_name('oracle_entities.json').read_bytes()
        if digest(raw) != receipt['oracle_entities_sha256']:
            raise ValueError('Entity oracle hash changed')
        oracle={d['pmid']:d for d in json.loads(raw)['documents']}
        if any(set(d)!={'pmid','source_hash','entities'} for d in oracle.values()):
            raise ValueError('Oracle cannot contain relation or scope labels')
    identity={'rules_version':RULES_VERSION,'input_sha256':input_sha,'corpus':corpus,'mode':mode,
              'dictionary':dictionaries.version,'chemical_snapshot_sha256':lexicon['manifest']['snapshot_sha256'],
              'selected_pmids':[d['pmid'] for d in documents],
              'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'evaluator_source_hashes':scoring.evaluator_source_hashes()}
    directory=settings.home/'experiments/v3'/digest(dumps(identity))
    directory.mkdir(parents=True,exist_ok=True)
    scoring._write_immutable(directory/'identity.json',identity) if not (directory/'identity.json').exists() else None
    if json.loads((directory/'identity.json').read_bytes())!=identity:
        raise ValueError('Experiment identity changed')
    if (directory/'report.json').exists():
        completed=json.loads((directory/'report.json').read_bytes())
        if completed['identity'] != identity or not scoring.verify_receipt(settings,'public_v3_development_experiment',completed):
            raise ValueError('Completed experiment receipt changed')
        print(dumps({'reused':True,'report':completed['output_path']}),flush=True)
        return completed
    predictions={}
    for document in documents:
        path=directory/('paper-'+document['pmid']+'.json')
        if path.exists():
            saved=json.loads(path.read_bytes())
            if saved['source_hash'] != document['source_hash'] or saved['manifest']['rules_version'] != RULES_VERSION:
                raise ValueError('Resume source or rules changed')
        else:
            metadata={'_chemical_lexicon':lexicon}
            if mode=='gold_entities':
                if oracle[document['pmid']]['source_hash']!=document['source_hash']:
                    raise ValueError('Oracle source changed')
                metadata['_gold_entities']=[e for e in oracle[document['pmid']]['entities'] if e['kind'] in {'gene','component'}]
            result=extract(document['text'],document['sections'],dictionaries,metadata)
            if result['status']!='complete' or result['manifest']['parser']['mode']!='production':
                raise ValueError('Actual pinned CPU parser required')
            validate_source_spans(result,document['text'])
            labels={'entities':oracle.get(document['pmid'],{}).get('entities',[])}
            projected=[p for event in result['events'] if (p:=scoring._project_event(event,labels,mapping,mode=='gold_entities'))]
            saved={'pmid':document['pmid'],'group_id':document['group_id'],'source_hash':document['source_hash'],
                   'source_level':document['source_level'],'entities':result['mentions'],'events':result['events'],
                   'relations':projected,'scopes':result['benchmark_scopes'],'annotated_spans':result['annotated_spans'],
                   'manifest':result['manifest'],'abstentions':result['abstentions']}
            scoring._write_immutable(path,saved)
        predictions[document['pmid']]=saved
    # Gold is opened only after every source-only prediction is persisted.
    gold,_,gold_sha=scoring._load_gold(settings,prepared['gold_path'])
    tasks,_=scoring._paper_annotation_tasks(gold,receipt,prepared['gold_path'])
    labels={d['pmid']:d for d in gold['documents']}
    rows={engine:[] for engine in ['heuristic-v2','heuristic-v3']}
    for document in documents:
        for engine,prediction,ids in [('heuristic-v2',baseline[document['pmid']],base_mapping),('heuristic-v3',predictions[document['pmid']],mapping)]:
            counts=scoring._paper_counts(labels[document['pmid']],prediction,ids,mode,annotated_tasks=tasks[document['pmid']])
            rows[engine].append({'pmid':document['pmid'],'group_id':document['group_id'],**counts})
    endpoints=sorted({k for row in rows['heuristic-v3'] for k,v in row.items() if isinstance(v,dict) and {'tp','fp','fn'}<=set(v)})
    metrics={engine:{endpoint:scoring._aggregate(values,endpoint) for endpoint in endpoints} for engine,values in rows.items()}
    maps={engine:{p['pmid']:p for p in values} for engine,values in rows.items()}
    comparisons={endpoint:scoring.paired_bootstrap(sorted(predictions),maps['heuristic-v2'],maps['heuristic-v3'],endpoint,2000,20261003) for endpoint in endpoints}
    report={'schema_version':'literature-v3-development-experiment-v1','id':uid(),'at':now(),'identity':identity,
            'corpus':corpus,'mode':mode,'split':'development','papers':len(documents),'available_development_papers':len(inputs['documents']),
            'complete_development_partition':len(documents)==len(inputs['documents']),
            'status':'exploratory_development_only','metrics':metrics,'comparisons':comparisons,'gold_sha256':gold_sha,
            'chemical_lexicon':lexicon['manifest'],'baseline_prediction_id':baseline_run['id'],
            'provenance_valid':all(r['provenance_valid']==r['provenance_total'] for r in rows['heuristic-v3']),
            'parser':next(iter(predictions.values()))['manifest']['parser'],
            'promotion_eligible':False,'nutrition_accuracy':'unmeasured','review_time_saved':'unmeasured',
            'sealed_test_reused':False,'output_path':str(directory/'report.json')}
    scoring._write_immutable(directory/'report.json',report)
    scoring._receipt(settings,'public_v3_development_experiment',report['id'],report)
    print(dumps({'report':report['output_path'],'papers':report['papers'],'metrics':metrics,'provenance_valid':report['provenance_valid']}),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-chemical',action='store_true')
    parser.add_argument('--corpus',choices=['bioredirect','bioscope','ebmnlp','evidenceoutcomes'])
    parser.add_argument('--mode',choices=['end_to_end','gold_entities'],default='end_to_end')
    parser.add_argument('--lexicon',type=Path)
    parser.add_argument('--limit',type=int)
    args=parser.parse_args()
    settings=Settings.from_env()
    if args.prepare_chemical:
        print(dumps(build_snapshot(settings.home/'experiments/v3/chemical-source')),flush=True)
        return
    if not args.corpus or not args.lexicon or (args.limit is not None and args.limit<1):
        parser.error('corpus and lexicon required; limit must be positive')
    run(settings,args.corpus,args.mode,args.lexicon,args.limit)


if __name__=='__main__':
    main()
