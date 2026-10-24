from pathlib import Path
import json
import re
from ..contract_v7 import canonical, sha, immutable
from ..repair_v3.common import ROOT, PRIOR, SEED, FLOOR, budget_status, data, baseline, native_metrics

HOME = ROOT/'runtime/chatgpt-repair-v4'
PAYLOAD_HOME = HOME/'payloads-canonical'
INFERENCE_HOME = HOME/'inference-canonical'
V3 = ROOT/'runtime/chatgpt-repair-v3'
MODELS = [('gpt-6.1-sol', 'medium'), ('gpt-6-luna', 'max')]

def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))

def write(name, value):
    immutable(HOME/name, value)
    return value

def log(stage, **values):
    print(canonical({'stage': stage, **values}), flush=True)

def source_files():
    paths = [*sorted(Path(__file__).parent.glob('*.py')), ROOT/'scripts/chatgpt_repair_v4.py', ROOT/'tests/test_repair_v4.py',
             ROOT/'nutri_literature/repair_v4_source_rules.py', ROOT/'tests/test_repair_v4_source_rules.py']
    return {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in paths if p.exists()}

def paper(pmid, split):
    p = read(V3/'rebuilt'/split/('paper-'+pmid+'.json'))
    d = next(d for d in data(split) if str(d['pmid']) == pmid)
    p['document'] = d
    return p

def payload(p):
    from ..text import sentence_spans
    source = p['document']['text']
    return {'schema_version': 'repair-v4-source', 'source': source, 'source_sha256': sha(source),
        'sentences': [{'key': 's'+str(i), **s} for i,s in enumerate(sentence_spans(source))],
        'mentions': [{**{k:m[k] for k in ('key','kind','text','start','end')},
            'candidates': [{'native_id':r['native_id'], 'taxon':r.get('taxon'),
                            'names':r.get('names',[])[:6]} for r in sorted(m['candidates'],key=lambda r:r['native_id'])]}
                     for m in p['mentions']],
        'gold_annotations_provided': False, 'prior_labels_or_current_id_flags_provided': False}

def prepare():
    from .. import chatgpt_tuning as old
    from ..evaluation_v6 import relation_sets
    from ..repair_v3.common import verify_source_freeze
    from ..repair_v3.runner import gold
    verify_source_freeze()
    docs, digest = gold()
    v2 = native_metrics(docs, {d['pmid']:baseline(d['pmid'],'development') for d in docs})
    v3 = native_metrics(docs, {d['pmid']:read(V3/'predictions/combined'/('paper-'+d['pmid']+'.json')) for d in docs})
    for result,expected in [(v2,(64,64,90)),(v3,(63,57,91))]:
        assert tuple(result['metrics'][k] for k in ('tp','fp','fn')) == expected
    train = data('train')
    positive, risk, other = [], [], []
    for d in train:
        p = paper(str(d['pmid']),'train')
        if relation_sets(d,{'relations':[]})[0]:
            positive.append(d['pmid'])
        elif any(e.get('native_type') == 'Association' for e in p['events']) or re.search(r'residue|substitution|codon|missense', d['text'], re.I):
            risk.append(d['pmid'])
        else:
            other.append(d['pmid'])
    order = lambda values: sorted(values,key=lambda v:sha(str(SEED)+str(v)))
    assert len(positive) == 142 and len(risk)+len(other) == 242
    selected = order(positive)+order(risk)[:29]+order(other)[:29]
    assert len(set(selected)) == 200
    weights = {str(v):1. if v in positive else len(risk)/29 if v in risk else len(other)/29 for v in selected}
    cohort = {'schema':'repair-v4-training-cohort','pmids':selected,'weights':weights,
        'strata':{'positive':len(positive),'risk_negative':len(risk),'other_negative':len(other)},
        'sample':{'positive':142,'risk_negative':29,'other_negative':29},'seed':SEED,
        'all_training_gold_relations':sum(len(relation_sets(d,{'relations':[]})[0]) for d in train),
        'not_a_random_384_paper_precision_estimate':True}
    write('cohort.json',cohort)
    for split, ids in [('train',selected),('development',[d['pmid'] for d in data('development')])]:
        for pmid in ids:
            write('payloads/'+split+'/paper-'+str(pmid)+'.json',payload(paper(str(pmid),split)))
    protected = [*V3.glob('*.json'), *V3.glob('*.md'), *V3.glob('*.csv'),
                 *V3.glob('predictions/*/*.json'), *V3.glob('analysis/*.json'),
                 *PRIOR.glob('predictions/*/*.json'), PRIOR/'selection.json',
                 *[ROOT/p for p in read(V3/'freeze.json')['implementation_sha256']]]
    write('audit.json',{'schema':'repair-v4-audit','baseline_v2':v2,'baseline_v3':v3,
        'gold_sha256':digest,'budget_at_start':budget_status(),
        'preserved_sha256':{str(p.relative_to(ROOT)):sha(p.read_bytes()) for p in protected},
        'training_audit_sha256':sha((old.HOME/'inputs/training-audit.json').read_bytes()),
        'training_targets':'official BioRED relation training only; full train annotations',
        'development_not_independent':True,'sealed_test_opened':False,
        'missing_pmcid_and_pretraining_overlap':'existing audit limitations retained',
        'scope':'batch only; production and existing server jobs unchanged'})
    log('prepared',training_papers=200,development_papers=100,budget=budget_status())
