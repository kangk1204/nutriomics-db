"""CPU-first, resumable experiment; development labels only in audit/evaluate."""
import argparse
from collections import Counter
import csv
import io
import json
from pathlib import Path
import time

from .common import (ROOT, HOME, PRIOR, FLOOR, SEED, ARMS, data, old_inputs, raw_reviews,
                     baseline, source_manifest, verify_source_freeze, budget_status,
                     native_metrics, write, log, _read, canonical, sha)
from .identity import Retriever, build_mentions, aligned_ids, choose
from .events import build_events, bags, project
from . import learning
from ..chatgpt_grounding import Catalog
from .. import chatgpt_tuning as old, benchmarks, evaluation_v6
from ..chatgpt_evaluation import _settings, _bootstrap


def gold():
    locator = _read(old.PRIOR / 'gpt-6.1-sol-medium-0fd4d4e0/scoring-locator.json')
    value, receipt, digest = benchmarks._load_gold(_settings(ROOT / 'runtime/chatgpt-inputs'), locator['gold_path'])
    docs = value['documents']
    if {d['pmid'] for d in docs} != {d['pmid'] for d in data('development')}:
        raise ValueError('개발 100편 불일치')
    if sum(len(evaluation_v6.relation_sets(d, {'relations': []})[0]) for d in docs) != 154:
        raise ValueError('정답 154건 불일치')
    return docs, digest


def audit():
    from ..splits_v7 import text_key
    prior = _read(PRIOR / 'analysis/final-audit.json')
    preserved = dict(prior['frozen_sources_verified'])
    preserved.update({str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in
                      [PRIOR/'comparison.json', PRIOR/'selection.json', *sorted((PRIOR/'predictions/combined').glob('*.json'))]})
    for relative, digest in preserved.items():
        if sha((ROOT/relative).read_bytes()) != digest:
            raise ValueError('기존 동결 파일 변경: '+relative)
    docs, digest = gold()
    predictions = {d['pmid']: baseline(d['pmid'], 'development') for d in docs}
    report = native_metrics(docs, predictions)
    if tuple(report['metrics'][k] for k in ('tp', 'fp', 'fn')) != (64, 64, 90):
        raise ValueError('기준선 64/64/90 재현 실패')
    training, sources = data('train'), data('development')
    previous_audit = _read(PRIOR/'data-audit.json')
    protected = _read(old.HOME/'inputs/training-audit.json')
    ids = set(protected['protected_pmids']) | {d['pmid'] for d in sources}
    texts = set(protected['protected_text_hashes']) | {text_key(d['text']) for d in sources}
    pmcs = set(protected['protected_pmcids'])
    overlap = [d['pmid'] for d in training if d['pmid'] in ids or text_key(d['text']) in texts
               or (d.get('pmcid') and d['pmcid'] in pmcs)]
    if overlap or len(training) != 384:
        raise ValueError('학습 분할/보호 자료 중복')
    errors = [r for r in _read(PRIOR/'analysis/outcomes.json')['false_positive_details'] if r['arm'] == 'combined']
    if len(errors) != 64:
        raise ValueError('오탐 명세 64건 불일치')
    association = Counter(r['annotation_comparison'] for r in errors if r['relation']['predicate'] == 'Association')
    write('analysis/baseline-errors.json', {'records': errors,
        'association_annotation_comparison': dict(association),
        'interpretation': '주석 대조이며 생물학적 오류 확정이 아님. 미주석/이력/정체성/범위 구분 필요',
        'used_as_prediction_whitelist': False})
    write('audit.json', {'schema': 'repair-v3-audit', 'baseline': report, 'gold_sha256': digest,
        'preserved_v2_sha256': preserved, 'budget': budget_status(), 'training_papers': 384,
        'training_gold_relations': 549, 'development_papers': 100, 'gold_relations': 154,
        'protected_overlap': overlap, 'pmcid_missing_training': previous_audit['pmcid_missing_training'],
        'pretraining_overlap': 'unknown', 'default_engine': 'heuristic-v1', 'sealed_test_opened': False})
    log('audit', metrics=report['metrics'], budget=budget_status())


def load_papers(split, include_supplement=True):
    docs = {d['pmid']: d for d in data(split)}
    result = []
    for pmid, d in docs.items():
        path = HOME/'rebuilt'/split/('paper-'+pmid+'.json')
        p = _read(path)
        if p['source_sha256'] != sha(d['text']):
            raise ValueError('재구성 원문 변경')
        active = baseline(pmid, split)
        active_ids = {}
        for relation in active['relations']:
            for j, m in enumerate(relation.get('endpoints', [])):
                active_ids.setdefault((m['kind'], m['start'], m['end']), set()).add(
                    relation['entity1' if j == 0 else 'entity2'])
        for m in p['mentions']:
            ids = sorted(active_ids.get((m['kind'], m['start'], m['end']), set()))
            m['old_ids'] = ids
            for candidate in m['candidates']:
                candidate['features']['old_id'] = float(candidate['native_id'] in ids)
        if include_supplement:
            from .review import saved, add_id_evidence
            supplied, extra = saved({**p, 'document': d}, split)
            if supplied is not None:
                if supplied['source_sha256'] != p['source_sha256']:
                    raise ValueError('보충 원문 변경')
                extra_events, errors = build_events(supplied, p['mentions'], extra, {'relations': []})
                p['events'] = sorted({e['key']: e for e in p['events']+extra_events}.values(), key=lambda e: e['key'])
                p['supplemental_id_evidence'] = add_id_evidence(p['mentions'], supplied, extra, d['text'])
                p['supplemental_response_sha256'] = {k: v['response_sha256'] for k, v in extra.items()}
                p['event_rejections'] = dict(Counter(p['event_rejections'])+Counter(errors))
        result.append({**p, 'document': d, 'active_baseline_ids': active_ids})
    return result


def rebuild():
    if not (HOME/'audit.json').exists():
        audit()
    catalog = Catalog.load(ROOT/'runtime/chatgpt-inputs/official-id-dictionary.json.gz')
    retriever = Retriever(catalog)
    for split in ('train', 'development'):
        docs = data(split)
        for i, d in enumerate(docs):
            path = HOME/'rebuilt'/split/('paper-'+d['pmid']+'.json')
            if path.exists():
                continue
            payload, _, previous = old_inputs(d, split)
            # Current v2 is the baseline; old input originals provide additional source witnesses.
            current = baseline(d['pmid'], split)
            combined = {'relations': previous['relations']+current['relations']}
            mentions, truncated = build_mentions(payload, combined, retriever)
            reviews = raw_reviews(d['pmid'], split)
            events, rejected = build_events(payload, mentions, reviews, combined)
            write(path.relative_to(HOME), {'pmid': d['pmid'], 'source_sha256': sha(d['text']),
                'mentions': mentions, 'events': events, 'event_rejections': rejected,
                'dictionary_sha256': catalog.sha256, 'automatic_mentions_truncated': truncated,
                'response_sha256': {k: v['response_sha256'] for k, v in reviews.items()},
                'new_model_calls': 0, 'labels_in_prediction_input': False})
            if (i+1) % 20 == 0 or i+1 == len(docs):
                log('rebuild', split=split, complete=i+1, total=len(docs))
    write('rebuild.json', {'schema': 'repair-v3-rebuild', 'dictionary_sha256': catalog.sha256,
        'source_sha256': source_manifest(), 'new_model_calls': 0,
        'counts': {s: len(list((HOME/'rebuilt'/s).glob('*.json'))) for s in ('train', 'development')}})


def verify_train():
    papers = load_papers('train')
    records, triggers = [], []
    total = available = 0
    for p in papers:
        expected = evaluation_v6.relation_sets(p['document'], {'relations': []})[0]
        menu = {'chemical': set(), 'gene': set()}
        for m in p['mentions']:
            menu[m['kind']].update(r['native_id'] for r in m['candidates'])
        covered = [k for k in expected if any(i in menu['chemical'] for i in k[1])
                   and any(i in menu['gene'] for i in k[1])]
        total += len(expected)
        available += len(covered)
        # No gold-derived trigger; annotations only used in the independent coverage record.
        reasons = []
        if p['event_rejections']:
            reasons.append('invalid_review_events')
        if not p['events']:
            reasons.append('no_valid_events')
        grouped = {}
        for e in p['events']:
            grouped.setdefault((e['chemical'], e['gene']), set()).add(e.get('native_type'))
        if any(len(v) > 1 for v in grouped.values()):
            reasons.append('type_conflict')
        if reasons:
            triggers.append({'pmid': p['pmid'], 'reasons': reasons,
                             'priority': len(reasons), 'label_free': True})
        records.append({'pmid': p['pmid'], 'gold': len(expected), 'both_ids_available': len(covered)})
    triggers.sort(key=lambda r: (-r['priority'], sha(str(SEED)+r['pmid'])))
    write('verification-policy.json', {'schema': 'repair-v3-label-free-review-policy',
        'training_order': triggers, 'max_training_papers': 200, 'max_development_papers': 100,
        'new_calls_default': 0, 'retries_max': 53, 'global_limit': 3000,
        'remaining_at_audit': _read(HOME/'audit.json')['budget']['remaining'],
        'automatic_account_or_model_switch': False})
    write('analysis/candidate-coverage.json', {'training_papers': len(papers), 'gold_relations': total,
        'both_id_menu_coverage': available/total, 'both_ids_in_menu': available,
        'is_relation_candidate_recall': False, 'records': records,
        'prior_candidate_recall': 210/549, 'target_candidate_recall': .80})
    log('verify-train', both_id_menu_coverage=available/total, trigger_papers=len(triggers), new_calls=0)


def train():
    papers = load_papers('train')
    start = time.monotonic()
    result = learning.train(papers)
    result['elapsed_seconds'] = round(time.monotonic()-start, 3)
    result['recall_floor_reference'] = {'minimum': FLOOR,
        'previous_v2_outer_recall': _read(PRIOR/'selection.json')['outer_metrics']['recall'],
        'fit_all_v2_not_used_as_training_selection_reference': True}
    write('selection.json', result)
    log('train-complete', selected=result['selected'], outer=result['outer_metrics'])


def training_diagnostics():
    papers = load_papers('train')
    chosen = _read(HOME/'selection.json')
    policy = chosen['model']['identity']
    first_loss, records = Counter(), []
    total = candidates = 0
    for p in papers:
        wanted = evaluation_v6.relation_sets(p['document'], {'relations': []})[0]
        menus, selected = {'chemical': set(), 'gene': set()}, {'chemical': set(), 'gene': set()}
        for m in p['mentions']:
            menus[m['kind']].update(r['native_id'] for r in m['candidates'])
            value = choose(m, policy['model'], policy['threshold'], policy['margin'], policy['enabled'])['selected_id']
            if value:
                selected[m['kind']].add(value)
        document_bags = bags(p, policy)
        for key in sorted(wanted):
            pair, native = set(key[1]), key[2]
            loss = None
            for kind in ('chemical', 'gene'):
                if not pair & menus[kind]:
                    loss = kind+'_not_retrieved'
                    break
            if loss is None:
                for kind in ('chemical', 'gene'):
                    if not pair & selected[kind]:
                        loss = kind+'_not_selected'
                        break
            matching = [b for b in document_bags if set(b['ids']) == pair]
            if loss is None and not matching:
                loss = 'no_resolved_event_pair'
            if loss is None and not any(native in b['types'] for b in matching):
                loss = 'no_matching_type_witness'
            if loss is None:
                loss = 'candidate_available'
                candidates += 1
            total += 1
            first_loss[loss] += 1
            records.append({'key': list(key), 'first_loss': loss})
    write('analysis/pipeline-losses.json', {'training_papers': len(papers), 'gold_relations': total,
        'strict_id_type_candidate_relations': candidates, 'candidate_recall': candidates/max(1, total),
        'first_loss_counts': dict(first_loss), 'records': records, 'selection_changed': False,
        'scope': 'post-selection training diagnostics, not held-out performance'})


def freeze():
    import importlib.metadata
    import platform
    selection = _read(HOME/'selection.json')
    training_diagnostics()
    write('environment.json', {'python': platform.python_version(), 'platform': platform.platform(),
        'packages': {name: importlib.metadata.version(name) for name in
                     ['numpy', 'scipy', 'scikit-learn', 'psutil', 'pyahocorasick']},
        'library_threads': 1, 'cpu_only': True})
    paths = [HOME/'selection.json', HOME/'audit.json', HOME/'verification-policy.json',
             HOME/'environment.json',
             *sorted((HOME/'rebuilt').glob('*/*.json')),
             *sorted((HOME/'supplement/train').glob('*/*/*.json')),
             *sorted((HOME/'supplement/payloads/train').glob('*.json'))]
    write('freeze.json', {'schema': 'repair-v3-freeze', 'implementation_sha256': source_manifest(),
        'evaluator_sha256': benchmarks.evaluator_source_hashes(),
        'artifacts_sha256': {str(p.relative_to(HOME)): sha(p.read_bytes()) for p in paths},
        'development_labels_used_for_selection': False, 'selected': selection['selected'],
        'supplemental_train_enabled': (HOME/'supplement/train-complete.json').exists(),
        'training_gate_passed': selection['training_gate_passed'],
        'diagnostic_only': selection['diagnostic_only'], 'default_engine': 'heuristic-v1'})


def predict():
    verify_source_freeze()
    selected = _read(HOME/'selection.json')
    papers = load_papers('development')
    for paper in papers:
        predictions = {'baseline': baseline(paper['pmid'], 'development')}
        policy = selected['model']['identity']
        current = predictions['baseline']
        by_span = {(m['kind'], m['start'], m['end']): choose(m, policy['model'], policy['threshold'],
                    policy['margin'], policy['enabled']) for m in paper['mentions']}
        corrected = []
        for r in current['relations']:
            ids = []
            for i, m in enumerate(r['endpoints']):
                chosen = by_span.get((m['kind'], m['start'], m['end']), {})
                ids.append(chosen.get('selected_id') or r['entity1' if i == 0 else 'entity2'])
            value = {**r, 'entity1': ids[0], 'entity2': ids[1]}
            if r.get('subject'):
                value['subject'] = ids[int(r['subject'] == r['entity2'])]
            corrected.append(value)
        predictions['identity_only'] = {'relations': corrected}
        for arm, lock in [('events_only', True), ('combined', False)]:
            predictions[arm] = learning.infer([paper], selected['model'], selected['selected']['threshold'], lock)[paper['pmid']]
        from ..chatgpt_extraction import exact_span
        for arm, value in predictions.items():
            for r in value['relations']:
                if any(not exact_span(paper['document']['text'], s) for s in r['endpoints']+r['evidence']):
                    raise ValueError('수락 인용 원문 위치 불일치')
            value = {**value, 'source_sha256': paper['source_sha256'],
                     'supplemental_response_sha256': paper.get('supplemental_response_sha256', {}),
                     'supplemental_id_evidence': paper.get('supplemental_id_evidence', [])}
            write('predictions/'+arm+'/paper-'+paper['pmid']+'.json', value)
    write('prediction-complete.json', {'papers': len(papers), 'arms': list(ARMS),
        'freeze_sha256': sha((HOME/'freeze.json').read_bytes()), 'gold_read_in_predict': False})


def evaluate():
    verify_source_freeze()
    if not (HOME/'prediction-complete.json').exists():
        raise ValueError('전체 예측 저장 전 평가 금지')
    docs, digest = gold()
    arms, correct, observed, predictions_by_arm = {}, {}, {}, {}
    for arm in ARMS:
        predictions = {d['pmid']: _read(HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')) for d in docs}
        predictions_by_arm[arm] = predictions
        arms[arm] = native_metrics(docs, predictions)
        correct[arm], observed[arm] = set(), set()
        for d in docs:
            expected, actual, _, _ = evaluation_v6.relation_sets(d, predictions[d['pmid']])
            correct[arm].update(expected & actual)
            observed[arm].update(actual)
    if tuple(arms['baseline']['metrics'][k] for k in ('tp', 'fp', 'fn')) != (64, 64, 90):
        raise ValueError('재평가 기준선 불일치')
    transitions = {}
    for arm in ARMS:
        transitions[arm] = {'retained_tp': len(correct[arm] & correct['baseline']),
            'lost_tp': len(correct['baseline']-correct[arm]), 'new_tp': len(correct[arm]-correct['baseline']),
            'paired_bootstrap2000_ci95': _bootstrap(
                {p: v['relation'] for p, v in arms['baseline']['per_paper'].items()},
                {p: v['relation'] for p, v in arms[arm]['per_paper'].items()}, SEED)}
    m = arms['combined']['metrics']
    type_metrics = {}
    all_expected = set().union(*(evaluation_v6.relation_sets(d, {'relations': []})[0] for d in docs))
    for arm in ARMS:
        type_metrics[arm] = {t: benchmarks._metrics({k for k in all_expected if k[2] == t},
                                {k for k in observed[arm] if k[2] == t}) for t in learning.TYPES}
    error_fates = []
    original_errors = _read(HOME/'analysis/baseline-errors.json')['records']
    for error in original_errors:
        key = (error['key'][0], tuple(error['key'][1]), error['key'][2])
        same_pair_correct = [k for k in correct['combined'] if k[:2] == key[:2]]
        error_fates.append({'key': error['key'], 'annotation_comparison': error['annotation_comparison'],
            'previous_relation': error['relation'], 'retained_same_error': key in observed['combined'],
            'correct_type_recovered_same_pair': [list(k) for k in same_pair_correct],
            'status': 'retained_error' if key in observed['combined'] else
                      'type_corrected' if same_pair_correct else 'removed_or_identity_changed',
            'expert_adjudication': False})
    # Development sources have no labels. Attach gold only inside this scoring stage.
    gold_by_id = {d['pmid']: d for d in docs}
    labeled_papers = [{**p, 'document': gold_by_id[p['pmid']]} for p in load_papers('development')]
    id_report = learning.identity_diagnostics(labeled_papers, _read(HOME/'selection.json')['model']['identity'])
    write('analysis/id-report.json', {'development': id_report,
        'outer_fold_training': [f['identity_diagnostics'] for f in _read(HOME/'selection.json')['outer_folds']],
        'unmatched_or_ambiguous_not_scored_as_id_error': True,
        'full_annotation_scope_boundary_not_an_id_precision_estimate': True})
    write('analysis/error-fates.json', {'records': error_fates, 'per_type': type_metrics,
        'association_summary': dict(Counter(r['status'] for r in error_fates if r['key'][2] == 'Association')),
        'all64_summary': dict(Counter(r['status'] for r in error_fates)), 'used_for_reselection': False})
    write('comparison.json', {'schema': 'repair-v3-comparison', 'papers': 100, 'gold_relations': 154,
        'gold_sha256': digest, 'arms': arms, 'transitions': transitions, 'per_type': type_metrics,
        'criteria': {'tp': 64, 'precision': .85, 'recall': FLOOR, 'old_tp_replacement_allowed': True},
        'goal_met': m['tp'] >= 64 and m['precision'] >= .85 and m['recall'] >= FLOOR,
        'default_engine': 'heuristic-v1', 'sealed_test_opened': False,
        'nutrition_accuracy': 'unmeasured', 'scope': 'repeated exposed development; not independent final',
        'budget': budget_status(), 'additional_calls': budget_status()['attempts']-_read(HOME/'audit.json')['budget']['attempts']})
    log('evaluation-complete', arms={a: v['metrics'] for a, v in arms.items()})


def compare():
    result = _read(HOME/'comparison.json')
    # Offline rescoring must be byte/number identical to the durable metrics.
    docs, _ = gold()
    for arm in ARMS:
        predictions = {d['pmid']: _read(HOME/'predictions'/arm/('paper-'+d['pmid']+'.json')) for d in docs}
        if native_metrics(docs, predictions) != result['arms'][arm]:
            raise ValueError('오프라인 재채점 불일치')
    for relative, digest in _read(HOME/'audit.json')['preserved_v2_sha256'].items():
        if sha((ROOT/relative).read_bytes()) != digest:
            raise ValueError('v2 보존 실패: '+relative)
    labels = {'baseline': 'v2 기준선', 'identity_only': 'ID 개선', 'events_only': '이벤트·유형 개선', 'combined': 'v3 결합'}
    lines = ['# 관계 추출 v3 개발 평가', '', '같은 100편·정답 154건의 반복 노출 개발 비교이며 독립 최종 성능이 아니다.', '',
             '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |', '|---|---:|---:|---:|---:|---:|---:|']
    for arm in ARMS:
        m = result['arms'][arm]['metrics']
        lines.append(f"| {labels[arm]} | {m['tp']} | {m['fp']} | {m['fn']} | {m['precision']:.2%} | {m['recall']:.2%} | {m['f1']:.2%} |")
    lines.extend(['', '판정: '+('합격' if result['goal_met'] else '목표 미달')+'. 기본 엔진 v1 유지.', '',
                  '## 기존 정탐 변화', ''])
    for arm in ARMS[1:]:
        t = result['transitions'][arm]
        lines.append(f"- {labels[arm]}: 유지 {t['retained_tp']}건, 상실 {t['lost_tp']}건, 신규 {t['new_tp']}건.")
    lines.extend(['', f"추가 추론 {result['additional_calls']}회. 저장 응답과 CPU만 사용한 경로이며 영양학 정확도는 미측정.", '',
                  '상세 원문·ID·이벤트 및 보류 사유는 rebuilt 및 predictions, 선택·학습 이력은 selection.json에 저장한다.'])
    errors = _read(HOME/'analysis/error-fates.json')
    lines.extend(['', '## Association 오탐 처리', '',
                  '기존 40건의 처리: '+canonical(errors['association_summary'])+'.', '',
                  '주석 부재는 생물학적 오류 확정과 구분한다. 제거된 관계도 원문과 기존 예측을 보존했다.', '',
                  '## ID 처리', ''])
    identity = _read(HOME/'analysis/id-report.json')['development']
    for kind, v in identity.items():
        n = v.get('aligned_mentions', 0)
        lines.append(f"- {kind}: 정답 위치 대응 {n}건, Recall@10 {v.get('recall10_hits', 0)/max(1,n):.2%}, "
                     f"Recall@50 {v.get('recall50_hits', 0)/max(1,n):.2%}, 잘못된 기존 ID 교정 {v.get('wrong_old_corrected', 0)}건, "
                     f"올바른 기존 ID 손상 {v.get('old_correct_damaged', 0)}건. 미대응 언급은 ID 정확도 분모와 별도로 보고한다.")
    path = HOME/'comparison.md'
    text = '\n'.join(lines)+'\n'
    if path.exists() and path.read_text(encoding='utf8') != text:
        raise ValueError('보고서 변경')
    path.write_text(text, encoding='utf8')
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=['arm', 'tp', 'fp', 'fn', 'precision', 'recall', 'f1'])
    writer.writeheader()
    for arm in ARMS:
        writer.writerow({'arm': arm, **result['arms'][arm]['metrics']})
    csv_path = HOME/'comparison.csv'
    if csv_path.exists() and csv_path.read_bytes() != output.getvalue().encode('utf8'):
        raise ValueError('CSV 변경')
    with csv_path.open('w', encoding='utf8', newline='') as stream:
        stream.write(output.getvalue())
    write('completion-audit.json', {'offline_rescore_identical': True, 'v2_preserved': True,
        'source_offsets_valid': True, 'gold_relations': 154, 'papers': 100,
        'goal_met': result['goal_met'], 'budget': budget_status(), 'default_engine': 'heuristic-v1'})
    artifact_files = [*sorted(HOME.glob('*.json')), HOME/'comparison.md', HOME/'comparison.csv',
                      *sorted((HOME/'analysis').glob('*.json'))]
    write('delivery-manifest.json', {'schema': 'repair-v3-delivery',
        'artifacts': {str(p.relative_to(HOME)): sha(p.read_bytes()) for p in artifact_files if p.name != 'delivery-manifest.json'},
        'implementation_sha256': source_manifest(), 'default_engine': 'heuristic-v1'})


STAGES = {'audit': audit, 'rebuild': rebuild, 'verify-train': verify_train, 'train': train,
          'freeze': freeze, 'predict': predict, 'evaluate': evaluate, 'compare': compare}


def main():
    parser = argparse.ArgumentParser(description='격리된 관계 추출 v3 개발 실험')
    parser.add_argument('stage', choices=[*STAGES, 'run', 'resume'])
    parser.add_argument('--allow-model-requests', action='store_true',
                        help='기존 로그인과 남은 전체 예산 안에서만 보충 추론 허용; 기본 0회')
    args = parser.parse_args()
    def execute(name, run):
        if name == 'predict' and args.allow_model_requests:
            frozen = verify_source_freeze()
            if not frozen.get('supplemental_train_enabled'):
                raise ValueError('보충 학습 없이 개발 입력 프로필 변경 금지: 새 실험 필요')
            from .review import request
            request(load_papers('development', include_supplement=False), 'development')
        run()
        if name == 'verify-train' and args.allow_model_requests:
            from .review import request
            if (HOME/'freeze.json').exists():
                raise ValueError('동결 후 학습 보충 응답 변경 금지: 새 실험 버전 필요')
            request(load_papers('train', include_supplement=False), 'train')
            write('supplement/train-complete.json', {'completed': True})
    if args.stage in STAGES:
        execute(args.stage, STAGES[args.stage])
    else:
        receipts = ['audit.json', 'rebuild.json', 'verification-policy.json', 'selection.json',
                    'freeze.json', 'prediction-complete.json', 'comparison.json', 'completion-audit.json']
        for (name, run), receipt in zip(STAGES.items(), receipts):
            pending_reviews = name == 'verify-train' and args.allow_model_requests and not (HOME/'supplement/train-complete.json').exists()
            if not (HOME/receipt).exists() or pending_reviews:
                log('stage-start', name=name)
                execute(name, run)


if __name__ == '__main__':
    main()
