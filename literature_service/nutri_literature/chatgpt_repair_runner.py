"""Auditable stages for isolated repair-v2; production APIs remain unchanged."""
import argparse
from collections import Counter
from pathlib import Path
import time

from . import chatgpt_tuning as old, benchmarks, evaluation_v6
from .chatgpt_repair_io import HOME, ROOT, MODELS, prepare_data, phase_contract, inference, score, AttemptBudget, log
from .chatgpt_repair_knowledge import prepare as prepare_knowledge, Knowledge
from .chatgpt_repair_semantics import PROMPT, make_payload, parse, native_identity
from .chatgpt_repair_learning import make_rows, train, ablations
from .chatgpt_grounding import Catalog
from .chatgpt_evaluation import _read, _metric, _bootstrap, _settings
from .contract_v7 import canonical, immutable, sha


def files():
    return [Path(__file__).with_name('chatgpt_repair_'+n+'.py') for n in ('io', 'knowledge', 'semantics', 'learning', 'runner')]


def contract():
    value = phase_contract('correction', files(), {
        'prompt_sha256': sha(PROMPT), 'knowledge_sha256': sha((HOME / 'knowledge/manifest.json').read_bytes()),
        'reference_contract_sha256': sha((HOME / 'reference-contract.json').read_bytes()),
        'original_client_sha256': sha((ROOT / 'nutri_literature/chatgpt_client.py').read_bytes()),
        'dictionary_sha256': sha((ROOT / 'runtime/chatgpt-inputs/official-id-dictionary.json.gz').read_bytes()),
        'training_papers': 384, 'training_gold_relations': 549,
        'model_selection': 'new CPU corrector outer5/inner3 paper folds; conditional on frozen upstream',
        'native_primary_label': 'one learned primary document type per ID pair; all scientific events retained',
        'dev_selection_labels_used': False})
    return value


def paths(split, model):
    return HOME / 'correction' / split / (model + '-' + dict(MODELS)[model])


def payloads(documents, split, catalog, knowledge):
    for i, d in enumerate(documents):
        name = 'paper-' + d['pmid'] + '.json'
        path = HOME / 'payloads' / split / name
        if not path.exists():
            value = make_payload(d, catalog, knowledge, split)
            # Deep allowlist enforcement: labels/scores from prior models never enter these prompts.
            def walk(value):
                if isinstance(value, dict):
                    if {'proposals', 'predicate', 'priority', 'proposed_by', 'selected_id', 'benchmark_relation_type'} & set(value):
                        raise ValueError('A previous prediction leaked into a blinded request')
                    for v in value.values():
                        walk(v)
                elif isinstance(value, list):
                    for v in value:
                        walk(v)
            walk(value)
            immutable(path, value)
        if (i+1) % 50 == 0:
            log('correction-payloads', split=split, completed=i+1, total=len(documents))


def infer_documents(documents, split, auth, frozen):
    extra = {}
    if split == 'development':
        extra = {'frozen_selection_sha256': sha((HOME / 'selection.json').read_bytes())}
    jobs = []
    for d in documents:
        name = 'paper-' + d['pmid'] + '.json'
        source = (HOME / 'payloads' / split / name).read_text(encoding='utf-8')
        for model, effort in MODELS:
            jobs.append((paths(split, model) / 'responses' / name, model, effort, PROMPT, source, extra, []))
    inference(jobs, 'independent-'+split, frozen, auth)


def inputs_for(d, split):
    name = 'paper-' + d['pmid'] + '.json'
    payload = _read(HOME / 'payloads' / split / name)
    parsed = {}
    for model, _ in MODELS:
        response = _read(paths(split, model) / 'responses' / name)
        request = _read(paths(split, model) / 'requests' / name)
        if response['request_sha256'] != sha(canonical(request)) or request['input'] != canonical(payload) or response.get('test_only'):
            raise ValueError('Independent correction response provenance differs')
        parsed[model] = parse(payload, response['text'])
    baseline = _read((HOME / 'reference/predictions' if split == 'train' else old.HOME / 'joint-verifier/predictions') / name)
    return payload, parsed, baseline


def fit_selection(documents, frozen):
    rows = {}
    for d in documents:
        payload, parsed, baseline = inputs_for(d, 'train')
        rows[d['pmid']] = make_rows(payload, parsed, baseline)
        immutable(HOME / 'training-candidates' / ('paper-'+d['pmid']+'.json'), rows[d['pmid']])
    reference = _read(HOME / 'reference/comparison.json')
    selected = train(documents, rows, reference['per_paper'])
    selected.update(contract_sha256=sha(canonical(frozen)), prompt_sha256=sha(PROMPT),
                    training_response_hashes={str(p.relative_to(HOME)): sha(p.read_bytes())
                        for p in (HOME / 'correction/train').glob('*/responses/*.json')})
    immutable(HOME / 'selection.json', selected)
    immutable(HOME / 'development-freeze.json', {
        'selection_sha256': sha((HOME / 'selection.json').read_bytes()),
        'contract_sha256': sha(canonical(frozen)), 'development_labels_used_for_selection': False})
    log('correction-selected', selected=selected['selected'], outer_metrics=selected['outer_metrics'],
        candidate_recall=selected['candidate_recall'])
    return selected


def build_development(sources, selection):
    predictions = {name: {} for name in ('baseline', 'identity_only', 'identity_and_type', 'combined')}
    failures = Counter()
    for d in sources:
        payload, parsed, baseline = inputs_for(d, 'development')
        rows = make_rows(payload, parsed, baseline)
        variants = ablations(payload, parsed, baseline, rows, selection['model'], selection['selected']['threshold'])
        predictions['baseline'][d['pmid']] = baseline
        for model, value in parsed.items():
            failures[model] += value['parse_failure']
        for name, value in variants.items():
            for relation in value['relations']:
                for span in [*relation.get('endpoints', []), *relation.get('evidence', [])]:
                    from .chatgpt_extraction import exact_span
                    if not exact_span(d['text'], span):
                        raise ValueError('A final accepted citation does not match the original source')
            immutable(HOME / 'predictions' / name / ('paper-'+d['pmid']+'.json'), value)
            predictions[name][d['pmid']] = value
    return predictions, dict(failures)


def evaluate(sources, selection):
    predictions, parse_failures = build_development(sources, selection)
    # Gold is opened for scoring ONLY after both models/100 sources and every projection are durable.
    locator = _read(old.PRIOR / 'gpt-6.1-sol-medium-0fd4d4e0/scoring-locator.json')
    gold, receipt, gold_hash = benchmarks._load_gold(_settings(ROOT / 'runtime/chatgpt-inputs'), locator['gold_path'])
    if {d['pmid'] for d in gold['documents']} != {d['pmid'] for d in sources}:
        raise ValueError('Complete unchanged development100 required')
    expected = sum(len(evaluation_v6.relation_sets(d, {'relations': []})[0]) for d in gold['documents'])
    if expected != 154:
        raise ValueError('The primary denominator changed')
    arms, sets = {}, {}
    for name, prediction in predictions.items():
        per_paper = score(gold['documents'], prediction)
        arms[name] = {'metrics': _metric([v['relation'] for v in per_paper.values()]),
                      'direction': _metric([v['relation_direction'] for v in per_paper.values()]),
                      'per_paper': per_paper}
        correct = set()
        for d in gold['documents']:
            wanted, actual, _, _ = evaluation_v6.relation_sets(d, prediction[d['pmid']])
            correct.update(wanted & actual)
        sets[name] = correct
    if tuple(arms['baseline']['metrics'][k] for k in ('tp', 'fp', 'fn')) != (56, 56, 98):
        raise ValueError('The frozen56/56/98 baseline was not reproduced')
    changes = {}
    for name in predictions:
        changes[name] = {'retained_true_positives': len(sets[name] & sets['baseline']),
                         'lost_true_positives': len(sets['baseline'] - sets[name]),
                         'new_true_positives': len(sets[name] - sets['baseline']),
                         'paired_paper_bootstrap2000_ci95': _bootstrap(
                           {p: r['relation'] for p, r in arms['baseline']['per_paper'].items()},
                           {p: r['relation'] for p, r in arms[name]['per_paper'].items()}, 20261017)}
    metric = arms['combined']['metrics']
    achieved = metric['tp'] >= 56 and (metric['precision'] or 0.) >= .85 and (metric['recall'] or 0.) >= 56/154
    genes = _read(HOME / 'knowledge/gene-history.json')
    history_scores = {}
    for name, prediction in predictions.items():
        per = []
        for d in gold['documents']:
            wanted, actual, _, _ = evaluation_v6.relation_sets(d, prediction[d['pmid']])
            def mapped(values):
                return {(p, tuple(sorted(native_identity(i, genes) for i in ids)), label) for p, ids, label in values}
            per.append(benchmarks._metrics(mapped(wanted), mapped(actual)))
        history_scores[name] = _metric(per)
    result = {'schema': 'chatgpt-repair-v2-comparison', 'papers': 100, 'gold_relations': 154,
              'input_sha256': receipt['inputs_sha256'], 'gold_sha256': gold_hash,
              'arms': arms, 'transitions_vs_previous_joint': changes, 'goal_met': achieved,
              'criteria': {'precision': .85, 'tp': 56, 'recall': 56/154}, 'parse_failures': parse_failures,
              'selection_sha256': sha((HOME / 'selection.json').read_bytes()),
              'supplementary_gene_history_scores': history_scores,
              'chemical_history_equivalence_applied': False,
              'evaluation_scope': 'repeated exposed development; not independent final',
              'budget': AttemptBudget().status(), 'default_engine': 'heuristic-v1',
              'nutrition_accuracy': 'unmeasured', 'sealed_test_opened': False}
    immutable(HOME / 'comparison.json', result)
    write_report(result)
    log('repair-evaluated', metrics=metric, goal_met=achieved)
    return result


def write_report(result):
    import csv
    lines = ['# 관계 추출 교정 후 동일100편 재평가', '',
             '원문100편·정답154관계와 기존 유형·원시 표준 ID 평가 기준을 유지했습니다. 방향은 별도 지표입니다.', '',
             '| 구성 | 정탐 | 오탐 | 누락 | 정밀도 | 재현율 | F1 |', '|---|---:|---:|---:|---:|---:|---:|']
    labels = {'baseline': '이전 결합 방식', 'identity_only': 'ID 교정',
              'identity_and_type': 'ID·유형 교정', 'combined': '독립 검증·CPU 교정 결합'}
    for name, row in result['arms'].items():
        m = row['metrics']
        values = [labels[name], m['tp'], m['fp'], m['fn'],
                  *['미정의' if m[k] is None else f'{100*m[k]:.2f}%' for k in ('precision', 'recall', 'f1')]]
        lines.append('| '+' | '.join(map(str, values))+' |')
    lines.extend(['', '합격 기준: 정탐56건 이상·재현율36.36% 이상·정밀도85% 이상을 모두 충족.',
                  '판정: '+('개발 성능 기준 충족' if result['goal_met'] else '개발 성능 기준 미달'), '',
                  '384편·관계549건에서만 교정기와 임계값을 선택했습니다. 외부5-fold·내부3-fold 수치는 새 CPU 교정기에 대한 고정 상위 모델 조건부 평가이며 전체 시스템의 독립 최종 성능이 아닙니다.', '',
                  '역사적 유전자 ID 병합은 기준선과 모든 개선안에 동일하게 적용한 보조 지표입니다. MeSH 대체 이력은 물질 동일성으로 인정하지 않았으며 주지표에는 적용하지 않았습니다.', '',
                  '기존100편은 반복 사용한 개발 자료입니다. 봉인 시험을 열거나 기본 엔진v1을 변경하지 않았습니다. 영양학 정확도와 검수 시간 절감은 미측정입니다.', '',
                  '재시도를 포함한 추론 예약: '+str(result['budget']['attempts'])+'/3000회. 완료 응답·원문 위치·오프라인 재채점 감사는 별도 기록합니다.', ''])
    (HOME / 'comparison.md').write_text('\n'.join(lines), encoding='utf-8')
    with (HOME / 'comparison.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['configuration', 'tp', 'fp', 'fn', 'precision', 'recall', 'f1'])
        for name, row in result['arms'].items():
            writer.writerow([name, *[row['metrics'][k] for k in ('tp', 'fp', 'fn', 'precision', 'recall', 'f1')]])


def audit():
    import xml.etree.ElementTree as ET
    result = _read(HOME / 'comparison.json')
    selection = _read(HOME / 'selection.json')
    if result['selection_sha256'] != sha((HOME / 'selection.json').read_bytes()):
        raise ValueError('Selection changed after evaluation')
    frozen = contract()
    response_ids, counts = set(), Counter()
    for phase in ('reference', 'correction'):
        for response_path in (HOME / phase).glob('**/responses/*.json'):
            value = _read(response_path)
            request = _read(response_path.parent.parent / 'requests' / response_path.name)
            if (value['request_sha256'] != sha(canonical(request)) or value.get('status') != 'completed'
                    or value.get('test_only') or value.get('actual_model') != request['model']):
                raise ValueError('A real completed request failed provenance validation')
            if value.get('reused_from'):
                original = ROOT / value['reused_from']
                if sha(original.read_bytes()) != value['reused_response_sha256']:
                    raise ValueError('Reused original response changed')
            response_ids.add(value['response_id'])
            counts[phase] += 1
    if counts != {'reference': 1536, 'correction': 968}:
        raise ValueError('All384 training/100 development model partitions must complete')
    if sha(canonical(frozen)) != selection['contract_sha256']:
        raise ValueError('Frozen implementation changed')
    for name, checksum in selection['training_response_hashes'].items():
        if sha((HOME / name).read_bytes()) != checksum:
            raise ValueError('A frozen training response changed')
    docs = _read(HOME / 'inputs/development-sources.json')['documents']
    remeasured = evaluate(docs, selection)
    if remeasured != result:
        raise ValueError('Offline native rescoring differs')
    state = AttemptBudget().status()
    if state['attempts'] > 3000 or state['states'].get('started'):
        raise ValueError('Attempt budget or unfinished requests remain')
    suites = ET.parse(HOME / 'verification.xml').getroot().findall('testsuite')
    tests = {k: sum(int(s.get(k, 0)) for s in suites) for k in ('tests', 'failures', 'errors', 'skipped')}
    if tests['tests'] < 178 or any(tests[k] for k in ('failures', 'errors', 'skipped')):
        raise ValueError('Related regression verification must pass')
    receipt = {'status': 'complete', 'partitions': dict(counts), 'unique_response_ids': len(response_ids),
               'primary_baseline_reproduced': [56, 56, 98], 'offline_rescoring_identical': True,
               'accepted_source_offset_mismatches': 0, 'budget': state,
               'goal_met': result['goal_met'], 'report_sha256': sha((HOME / 'comparison.json').read_bytes()),
               'contract_sha256': sha(canonical(frozen)), 'tests': tests, 'default_engine': 'heuristic-v1'}
    immutable(HOME / 'completion-audit.json', receipt)
    log('repair-audit', **receipt)


def main():
    import os
    import psutil
    from .chatgpt_auth import ChatGPTAuth
    from .chatgpt_batch import batch_lock
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['prepare', 'baseline', 'correct-train', 'train', 'freeze', 'development', 'evaluate', 'audit', 'run', 'resume'])
    stage = parser.parse_args().stage
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10)
    docs, sources = prepare_data()
    prepare_knowledge()
    if stage == 'prepare':
        return
    if stage == 'audit':
        audit()
        return
    if stage in {'run', 'resume'}:
        while not (HOME / 'reference/comparison.json').exists():
            from .chatgpt_batch import batch_busy
            if not batch_busy(old.PRIOR):
                from .chatgpt_repair_baseline import run
                with batch_lock(old.PRIOR):
                    run(ChatGPTAuth())
                break
            log('await-reference', **AttemptBudget().status())
            time.sleep(30)
    with batch_lock(old.PRIOR):
        auth = ChatGPTAuth()
        if stage == 'baseline':
            from .chatgpt_repair_baseline import run
            run(auth)
            return
        if not (HOME / 'reference/comparison.json').exists():
            raise ValueError('The384-paper frozen reference must finish first')
        frozen = contract()
        catalog = knowledge = None
        if stage in {'correct-train', 'development', 'run', 'resume'}:
            catalog = Catalog.load(ROOT / 'runtime/chatgpt-inputs/official-id-dictionary.json.gz')
            knowledge = Knowledge(catalog)
        if stage in {'correct-train', 'run', 'resume'}:
            payloads(docs, 'train', catalog, knowledge)
            infer_documents(docs, 'train', auth, frozen)
        if stage in {'train', 'freeze', 'run', 'resume'}:
            selection = _read(HOME / 'selection.json') if (HOME / 'selection.json').exists() else fit_selection(docs, frozen)
        else:
            selection = _read(HOME / 'selection.json') if (HOME / 'selection.json').exists() else None
        if stage in {'development', 'run', 'resume'}:
            if not selection:
                raise ValueError('Freeze a training-only selection first')
            payloads(sources, 'development', catalog, knowledge)
            infer_documents(sources, 'development', auth, frozen)
        if stage in {'evaluate', 'run', 'resume'}:
            if not selection:
                raise ValueError('Frozen selection required')
            evaluate(sources, selection)
        if stage in {'run', 'resume'}:
            audit()


if __name__ == '__main__':
    main()
