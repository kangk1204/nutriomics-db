"""One bound BC8 attempt; automatic predictions saved before any gold read."""
import json,os
from pathlib import Path
from .contract_v7 import immutable,sha,passes
from .freeze_v7 import reserve_attempt


def evaluate(home,spec,freeze,models,output):
    from .settings import Settings
    from .operations_v6 import components,processing_slot
    from .resources_v6 import memory_budget
    from .dictionaries import load_dictionaries
    from .registry_v7 import extract_engine
    from .mapping_v7 import native_mapping
    from . import evaluation_v6,benchmarks
    home,output=Path(home),Path(output)
    protocol,frozen=reserve_attempt(home,spec,freeze)
    head=Path(frozen['head_path'])
    if sha((head/'manifest.json').read_bytes())!=frozen['head_manifest_sha256']:
        raise ValueError('Frozen relation artifact differs')
    input_path=Path(protocol['inputs_path'])
    raw=input_path.read_bytes()
    if sha(raw)!=protocol['inputs_sha256']:raise ValueError('Protected final source differs')
    documents=json.loads(raw)['documents']
    training=set(json.loads((head/'manifest.json').read_bytes())['training_pmids'])
    if training & {d['pmid'] for d in documents}:raise ValueError('Final PMID overlaps training')
    os.environ['LITERATURE_V6_MODEL_HOME']=str(Path(models).resolve())
    settings=Settings(home,Path(__file__).parents[1]/'config/topics.json')
    with processing_slot(settings),memory_budget():
        values=components(settings,'union')
        from .extraction_v7 import load_head
        loaded=load_head(head)
        dictionaries=load_dictionaries(settings,'heuristic-v2')
        values.update(_v7_model_path=head,_v7_loaded_head=loaded)
        for d in documents:
            target=output/'end_to_end'/('paper-'+d['pmid']+'.json')
            if target.exists():continue
            result=extract_engine(d['text'],d['sections'],dictionaries,values,engine='heuristic-v7')
            relations,verified=evaluation_v6.project_hypotheses(result,native_mapping(result['mentions']),False)
            immutable(target,{'pmid':d['pmid'],'source_hash':sha(d['text']),'entities':result['mentions'],
                              'relations':relations,'verified_relations':verified,'relation_hypotheses':result['relation_hypotheses'],
                              'freeze_sha256':sha(Path(freeze).read_bytes())})
        # No gold parsing above this point. Oracle measurements start afterward.
        raw_gold=Path(protocol['gold_path']).read_bytes()
        if sha(raw_gold)!=protocol['gold_sha256']:raise ValueError('Final gold differs')
        gold={d['pmid']:d for d in json.loads(raw_gold)['documents']}
        per_mode={}
        for mode in ('end_to_end','gold_entities'):
            rows=[]
            for d in documents:
                target=output/mode/('paper-'+d['pmid']+'.json')
                if mode=='gold_entities' and not target.exists():
                    metadata={**values,'_gold_entities':gold[d['pmid']]['entities']}
                    result=extract_engine(d['text'],d['sections'],dictionaries,metadata,engine='heuristic-v7')
                    rels,verified=evaluation_v6.project_hypotheses(result,native_mapping(result['mentions']),True)
                    immutable(target,{'pmid':d['pmid'],'source_hash':sha(d['text']),'entities':result['mentions'],
                                      'relations':rels,'verified_relations':verified,'freeze_sha256':sha(Path(freeze).read_bytes())})
                pred=json.loads(target.read_bytes())
                if pred['freeze_sha256']!=sha(Path(freeze).read_bytes()):raise ValueError('Final prediction artifact differs')
                metric=evaluation_v6._metrics_for(gold[d['pmid']],pred,native_mapping(pred['entities']),mode=='gold_entities','bioredirect')
                rows.append({'pmid':d['pmid'],**metric})
            per_mode[mode]={'per_paper':rows,'relation':benchmarks._aggregate(rows,'relation')}
    report={'engine':'heuristic-v7','corpus':'bc8','papers':len(documents),'modes':per_mode,
            'scientific_targets_met':all(passes(r['relation']) for r in per_mode.values()),
            'nutrition_accuracy':'unmeasured','default_engine':'heuristic-v1','sealed_test_opened':True,
            'freeze_sha256':sha(Path(freeze).read_bytes()),'inputs_sha256':sha(raw),'gold_sha256':sha(raw_gold)}
    immutable(output/'report.json',report)
    return report
