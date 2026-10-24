"""All-mention candidate/NIL supervision and nested train-only ID selection."""
import json
from pathlib import Path
from .contract_v7 import C_GRID, canonical, immutable, sha
from .identity_v7 import ListwiseRanker, candidate_rows, choose
from .splits_v7 import grouped_folds

ID_THRESHOLDS = (.5, .6, .7, .8, .85, .9, .95, .99)
MARGINS = (0., .1, .2)


def prepare(data, audit_path, cache, models, output):
    from .candidates_v4 import GeneIndex
    from .identity_v6 import CandidateSearch
    from .text import sentence_spans
    raw = Path(data).read_bytes()
    source, audit = json.loads(raw), json.loads(Path(audit_path).read_bytes())
    if source.get('split') != 'train' or audit.get('passed') is not True or audit['audited_training_sha256'] != sha(raw):
        raise ValueError('Audited official train source required')
    cache, models, output = map(Path, (cache, models, output))
    pointer = json.loads((models / 'gene-snapshot.json').read_bytes())
    search = CandidateSearch(GeneIndex.load(models / 'genes' / pointer['filename']))
    original_retrieve = search.retrieve
    from functools import lru_cache
    search.retrieve = lru_cache(maxsize=20000)(original_retrieve)
    files = []
    for n, doc in enumerate(source['documents']):
        if doc.get('gene_annotation_complete') is False:
            continue
        destination = output / ('paper-' + doc['pmid'] + '.json')
        if not destination.exists():
            from collections import defaultdict
            gold = defaultdict(set)
            for e in doc['entities']:
                if e['kind']=='gene':gold[(e['start'],e['end'])].update(map(str,e.get('native_ids',[])))
            path = cache / destination.name
            candidates = json.loads(path.read_bytes())['entities'] if path.exists() else []
            if path.exists() and json.loads(path.read_bytes())['source_hash'] != sha(doc['text']):
                raise ValueError('Identity automatic source differs')
            automatic = {(e['start'], e['end']) for e in candidates if e['kind'] == 'gene'}
            candidates = [(e, 'automatic') for e in candidates if e['kind'] == 'gene'] + [
                (e, 'gold_augmentation') for e in doc['entities'] if e['kind'] == 'gene' and (e['start'], e['end']) not in automatic]
            sentences = sentence_spans(doc['text'], doc['sections'])
            examples = []
            for mention, origin in candidates:
                records, features, eligibility = candidate_rows(mention, doc['text'], sentences, search)
                expected = gold.get((mention['start'], mention['end']), set())
                labels = [int(r['native_id'] in expected and eligibility['eligible'] and r['taxon_id'] == eligibility['taxon_id']) for r in records]
                examples.append({'start': mention['start'], 'end': mention['end'], 'origin': origin,
                    'features': features, 'labels': labels, 'eligibility': eligibility, 'gold_ids': sorted(expected),
                    'previous_id': mention.get('normalized_id'), 'weight': 1. if origin == 'automatic' else .5,
                    'records': [{k: r[k] for k in ('id','native_id','taxon_id')} for r in records]})
            immutable(destination, {'pmid': doc['pmid'], 'text': doc['text'], 'examples': examples,
                                     'gold_mentions': sum(len(ids) for ids in gold.values()),
                                     'gold_boundary_mentions':len(gold),'automatic_available': path.exists()})
        files.append({'path': destination.name, 'sha256': sha(destination.read_bytes())})
        print(canonical({'stage': 'v7-id-candidates', 'papers': n + 1, 'total': len(source['documents'])}), flush=True)
    manifest = {'split':'train', 'files': files, 'training_sha256': sha(raw), 'audit_sha256': sha(Path(audit_path).read_bytes()),
                'dictionary_sha256': search.gene_index.manifest['records_sha256'],
                'identity_source_sha256': sha(Path(__file__).with_name('identity_v7.py').read_bytes())}
    immutable(output / 'manifest.json', manifest)
    return manifest


def metric(examples, scores, threshold, margin, gold_total):
    tp = fp = selected_count = changed = correct_change = damaged = 0
    recall10 = total_retrievable = baseline_tp = baseline_fp = cleared_correct = 0
    for e, values in zip(examples, scores):
        if e['origin'] != 'automatic':
            continue
        expected = set(e['gold_ids'])
        previous = (e.get('previous_id') or '').split(':')[-1]
        if previous:
            baseline_tp += previous in expected
            baseline_fp += previous not in expected
        if expected:
            total_retrievable += 1
            ranking = sorted(range(len(e['records'])), key=lambda i: -values[i])[:10]
            recall10 += any(e['records'][i]['native_id'] in expected for i in ranking)
        record = choose(e['records'], values, e['eligibility'], threshold, margin)
        if record:
            selected_count += 1
            correct = record['native_id'] in expected
            tp += correct
            fp += not correct
            if record['id'] != e.get('previous_id'):
                changed += 1
                correct_change += correct
                damaged += previous in expected and not correct
        elif previous in expected:
            # NIL is an ID failure in the strict endpoint, even if it raises
            # precision among the remaining links.
            damaged += 1
            cleared_correct += 1
    fn = max(0, gold_total - tp)
    p, r = tp / selected_count if selected_count else 0., tp / gold_total if gold_total else 0.
    return {'tp':tp,'fp':fp,'fn':fn,'precision':p,'recall':r, 'f1':2*p*r/(p+r) if p+r else 0.,
            'changed':changed,'correct_changes':correct_change,'damaged_old_correct':damaged,
            'cleared_old_correct':cleared_correct,'baseline_tp':baseline_tp,'baseline_fp':baseline_fp,
            'baseline_fn':max(0,gold_total-baseline_tp),
            'ranked_recall_at_10':recall10/total_retrievable if total_retrievable else 0., 'ranked_denominator':total_retrievable}


def select(rows):
    eligible = [r for r in rows if r['tp'] > 0 and r['precision'] >= .9]
    best = max(eligible or rows, key=lambda r:(r['recall'] if eligible else r['f1'], r['f1'], -r['damaged_old_correct'], -r['C']))
    return {**best, 'target_precision_met':bool(eligible)}


def fit(features, output):
    import joblib
    features, output = Path(features), Path(output)
    manifest = json.loads((features / 'manifest.json').read_bytes())
    if manifest['split'] != 'train':
        raise ValueError('Only training ID candidates may fit')
    docs, examples = [], []
    for f in manifest['files']:
        raw = (features / f['path']).read_bytes()
        if sha(raw) != f['sha256']:
            raise ValueError('ID feature hash differs')
        d = json.loads(raw)
        docs.append(d)
        examples.extend({**e, 'pmid':d['pmid']} for e in d['examples'])
    assignment = grouped_folds(docs)
    contract = {'feature_sha256':sha((features/'manifest.json').read_bytes()),
                'source_sha256':sha(Path(__file__).read_bytes()), 'identity_sha256':sha(Path(__file__).with_name('identity_v7.py').read_bytes())}
    immutable(output / 'training-contract.json',contract)
    def crossfit(records, fold_map, c, label):
        predictions = {}
        for fold in sorted(set(fold_map.values())):
            train = [e for e in records if fold_map[e['pmid']] != fold]
            held = [(i,e) for i,e in enumerate(records) if fold_map[e['pmid']] == fold]
            path = output / (label + '-C-' + str(c) + '-fold-' + str(fold) + '.joblib')
            model = joblib.load(path) if path.exists() else ListwiseRanker().fit(train,c)
            if not path.exists():
                joblib.dump(model,path)
            for i,e in held:
                predictions[i] = model.predict(e['features'])
            print(canonical({'stage':'listwise-id-fit','cohort':label,'fold':fold,'C':c}),flush=True)
        return [predictions[i] for i in range(len(records))]
    outer_values = []
    for outer in range(5):
        fit_docs = [d for d in docs if assignment[d['pmid']] != outer]
        fold_map = grouped_folds(fit_docs,3,'id-inner:'+str(outer))
        train = [e for e in examples if e['pmid'] in fold_map]
        expected = sum(d['gold_mentions'] for d in fit_docs if d['automatic_available'])
        configurations = []
        for c in C_GRID:
            values = crossfit(train,fold_map,c,'outer-'+str(outer))
            configurations.extend({'C':c,'threshold':t,'margin':m, **metric(train,values,t,m,expected)} for t in ID_THRESHOLDS for m in MARGINS)
        selection = select(configurations)
        head = ListwiseRanker().fit(train,selection['C'])
        held = [e for e in examples if assignment[e['pmid']] == outer]
        held_total = sum(d['gold_mentions'] for d in docs if assignment[d['pmid']] == outer and d['automatic_available'])
        observed = metric(held,[head.predict(e['features']) for e in held],selection['threshold'],selection['margin'],held_total)
        outer_values.append(observed)
        immutable(output / ('outer-'+str(outer)+'.json'), {'selection':selection,'metric':observed})
    configurations = []
    total = sum(d['gold_mentions'] for d in docs if d['automatic_available'])
    for c in C_GRID:
        values = crossfit(examples,assignment,c,'final-oof')
        configurations.extend({'C':c,'threshold':t,'margin':m, **metric(examples,values,t,m,total)} for t in ID_THRESHOLDS for m in MARGINS)
    selection = select(configurations)
    model = ListwiseRanker().fit(examples,selection['C'])
    joblib.dump(model,output/'identity.joblib')
    strict_tp=sum(v['tp'] for v in outer_values);strict_fp=sum(v['fp'] for v in outer_values);strict_fn=sum(v['fn'] for v in outer_values)
    old_tp=sum(v['baseline_tp'] for v in outer_values);old_fp=sum(v['baseline_fp'] for v in outer_values);old_fn=sum(v['baseline_fn'] for v in outer_values)
    strict_preserved=2*strict_tp/max(1,2*strict_tp+strict_fp+strict_fn)>=2*old_tp/max(1,2*old_tp+old_fp+old_fn)
    result = {**contract,'selection':selection,'outer_metrics':outer_values,'training_pmids':sorted(assignment),
              'fold_assignments':assignment,'configurations':configurations,'model_sha256':sha((output/'identity.joblib').read_bytes()),
              'dictionary_sha256':manifest['dictionary_sha256'],'automatic_enabled':False,
              'strict_boundary_id_preservation_verified':strict_preserved,
              'strict_metric_definition':'exact automatic boundary plus native ID; multiple gold IDs count separately; NIL is FN',
              'nutrition_accuracy':'unmeasured','test_labels_read':False}
    immutable(output/'manifest.json',result)
    from .adoption_v7 import write_identity_adoption
    write_identity_adoption(output)
    return result
