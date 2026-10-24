"""Render the diagnostic ledger without changing annotations or model inputs."""
from pathlib import Path
import csv
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / 'runtime/chatgpt-repair-v2/analysis'
CATEGORIES = {
    'annotated_pair_wrong_type': '정답 개체쌍의 관계 유형 불일치',
    'chemical_id_absent_from_annotation': '문헌 주석에 물질 ID 없음',
    'gene_id_absent_from_annotation': '문헌 주석에 유전자 ID 없음',
    'annotated_endpoints_unannotated_pair': '주석된 개체 사이에 관계 주석 없음',
}
HYPOTHESES = {
    'genotype_marker_or_cell_count_confused_with_expression': '유전형·표지자·세포 수와 발현의 혼동 가능성',
    'sequence_component_treated_as_chemical_entity': '서열 구성 요소를 물질 개체로 취급했을 가능성',
    'gene_product_or_complex_role_confusion': '유전자 산물·복합체의 역할 혼동 가능성',
    'unresolved_entity_eligibility_relation_or_annotation_scope': '개체 적격성·관계·주석 범위 추가 확인 필요',
    'binding_loss_confused_with_signed_expression': '결합 소실을 발현 증감으로 바꿨을 가능성',
    'modification_confused_with_expression': '단백질 변형과 발현의 혼동 가능성',
    'native_type_requires_full_document_and_role_review': '관계 유형에 대한 전체 문서·역할 재검토 필요',
    'species_or_cell_line_assignment_or_annotation_ambiguity': '종·세포주 배정 또는 주석의 모호성',
    'assay_or_planning_statement_requires_observation_check': '측정법·계획 문장의 관측 여부 확인 필요',
}


def escape(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def main():
    source = FOLDER / 'previous56-error-ledger.json'
    ledger = json.loads(source.read_bytes())
    if ledger['total'] != 56 or len(ledger['errors']) != 56 or ledger['used_as_training_labels']:
        raise ValueError('Expected the unchanged56-case diagnostic ledger')
    rows = []
    sections = []
    for index, error in enumerate(ledger['errors'], 1):
        chemical, gene = error['chemical'], error['gene']
        evidence = error['evidence']
        gene_names = sorted({name for candidate in gene.get('candidates', [])
                             if str(candidate['native_id']) == str(gene['selected_id'])
                             for name in candidate.get('names', [])})
        row = {
            '번호': index, 'PMID': error['pmid'],
            '주석 대조': CATEGORIES[error['annotation_comparison']],
            '물질 언급': chemical['text'], '물질 ID': chemical['selected_id'],
            '물질 공식 명칭': (chemical.get('official_metadata') or {}).get('name', ''),
            '유전자 언급': gene['text'], '유전자 ID': gene['selected_id'],
            '공식 사전 유전자명·별칭': '; '.join(gene_names) or '미확인',
            '예측 유형': error['predicted_native_type'],
            '같은 개체쌍의 정답 유형': '; '.join(error['expected_native_types_same_pair']),
            '올바른 유형도 기존 출력에 존재': error['correct_type_already_present'],
            '원인 가설': '; '.join(HYPOTHESES[h] for h in error['root_cause_hypotheses']),
            '원문': '\n'.join(e['text'] for e in evidence),
            '원문 위치': '; '.join(f"{e['start']}:{e['end']}" for e in evidence),
            '원문 SHA-256': error['source_sha256'],
            '확실성': '전문가 판정 전 원문 기반 가설',
        }
        rows.append(row)
        sections.extend([
            f"### {index}. PMID {row['PMID']}", '',
            f"- 주석 대조: {row['주석 대조']}",
            f"- 물질: {chemical['text']} / {chemical['selected_id']} / {row['물질 공식 명칭']}",
            f"- 유전자: {gene['text']} / {gene['selected_id']}",
            f"- 공식 사전의 해당 유전자명·별칭: {row['공식 사전 유전자명·별칭']}",
            f"- 기존 예측: {row['예측 유형']}; 같은 개체쌍의 정답 유형: {row['같은 개체쌍의 정답 유형'] or '없음'}",
            f"- 원인 가설: {row['원인 가설']}",
            f"- 올바른 유형도 기존 출력에 존재: {'예' if row['올바른 유형도 기존 출력에 존재'] else '아니오'}", '',
        ])
        for quote in evidence:
            sections.extend([f"원문 위치 `{quote['start']}:{quote['end']}`:", '',
                             '> ' + quote['text'].replace('\n', '\n> '), ''])
        sections.extend([f"원문 SHA-256: `{row['원문 SHA-256']}`", ''])
    header = [
        '# 기존 오탐56건 검토 명세', '',
        '기존 개발100편에서 원시 ID·관계 유형 기준으로 오탐 처리된56건을 정리했다. '
        '아래 분류는 주석 대조이며, 생물학적 오류나 전문가가 확정한 원인과 동일하지 않다. '
        '주석 부재는 관계의 생물학적 부재를 뜻하지 않는다. 이 목록은 학습 레이블·예외 규칙·유지 목록으로 사용하지 않았다.', '',
        f"원본 명세 SHA-256: `{hashlib.sha256(source.read_bytes()).hexdigest()}`", '',
        '| 주석 대조 분류 | 건수 |', '|---|---:|',
        *[f'| {CATEGORIES[key]} | {count} |' for key, count in ledger['categories'].items()], '',
        '| 번호 | PMID | 물질 ID | 유전자 ID | 예측 유형 | 주석 대조 |', '|---:|---|---|---|---|---|',
        *['| ' + ' | '.join(escape(row[key]) for key in ('번호', 'PMID', '물질 ID', '유전자 ID', '예측 유형', '주석 대조')) + ' |' for row in rows], '',
    ]
    (FOLDER / 'previous56-error-ledger.md').write_text('\n'.join(header + sections), encoding='utf-8')
    with (FOLDER / 'previous56-error-ledger.csv').open('w', encoding='utf-8-sig', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({'rows': len(rows), 'labels_or_inputs_changed': False}))


if __name__ == '__main__':
    main()
