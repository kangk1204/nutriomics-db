# NutriOmics 최종 업그레이드 실행 안내

투고판과 과학 확장판을 분리합니다. 빌드·수치 보존·소스 대조는 사람의 과학적 승인이나 최종 조회 정확도를 대신하지 않습니다. 원본과 기존 release를 수정하지 않고 새 영구 디렉터리를 만듭니다.

## 1. 원문과 검수 의존자료 고정

```bash
python -m scripts.prepare_final_upgrade --science <corrected-science.sqlite3> \
  --packets <source-bound-study-and-method-packets.json> \
  --workspace <captured-source-root> --output <new-upgrade-inputs>
```

원문 파일 해시와 확인된 위치, 기록별 해시를 검증해 과학적 분류 제안·분석 재검토 목록·실제 집계·검수 의존자료를 생성합니다. 출력 폴더는 새로 만듭니다. `source_checked`는 사람 승인이 아닙니다. GSE219217의 추출법–시점 정렬과 GSE127530의 보충제 방문 대응 미확인을 별도 분류로 제공하며 수치 행렬은 변경하지 않습니다.

기록 payload가 같아도 원문이나 관련 분석 기록이 바뀌면 `source_dependency_sha256`가 달라집니다. 이 값이 다른 과거 사람 판정은 거부합니다. 작업용 생산 코드가 발전한 경우 원래 분석의 정확한 코드 bytes를 `source_snapshots/review_evidence/<sha256>/<filename>`에 보존할 수 있으며 해시가 일치할 때만 원래 증빙으로 사용합니다. 현재 코드를 과거 실행 코드로 표시하지 않습니다.

검수 화면을 생성할 때는 다음과 같이 의존자료를 전달합니다.

```python
build_review_package(science, output, dependencies=review_dependencies)
validate_review_decisions(science, human_decisions, dependencies=review_dependencies)
```

`accept / correct / exclude / unconfirmed` 모두 사람이 이름·시각·원문 위치·이유를 입력합니다. `correct` 기록은 수정된 데이터의 새 해시로 다시 검수합니다. 익명의 일괄 동의를 기록별 승인으로 변환하지 않습니다. 제외·미확인을 과학적 승인 수에 넣지 않습니다.

## 2. 실제 단일 SQLite와 API

`COMPOSED_PUBLIC_RELEASE_ko.md`의 CLI로 불변 공개 parent와 수정된 과학 supplement를 새 공개 SQLite에 결합합니다. 효과의 다섯 부분 키, 단위·결측·통계값과 원본 이용조건은 보존합니다. 동일 DB의 임상·중재·결과를 API·export·manifest에서 조회합니다. 새로운 코드와 DB를 별도 staging에서 검증한 다음에만 운영 반영합니다.

기존 `kind=food`는 호환성을 위해 음식과 식단 노출을 함께 반환합니다. `food_record`는 실제 음식 기록만, `exposure`는 과학 기록의 원본 노출 ID와 일치하는 정식 노출 노드만 반환합니다. 연구 전체나 일반 식단 노드를 제품 노출로 대체하지 않습니다. 연구 노출은 기존 `/api/v1/interventions`의 정식 식별자로도 검색합니다. 조성이 없는 추출물·보충제에는 g 섭취량 계산을 생성하지 않습니다.

## 3. DIRECT PLUS 원본 수집과 분석

```bash
python scripts/download_bridge_studies.py --root <new-permanent-root> \
  --studies E-MTAB-12527 --raw --raw-budget-gib 24 --download-workers 8
Rscript --vanilla tests/check_direct_plus_helpers.R scripts/direct_plus_helpers.R
Rscript --vanilla scripts/analyze_direct_plus.R --root <new-permanent-root> \
  --output-dir <new-v3-analysis-output> --environment-lock <exact-project-R-lock> \
  --chunk-size 32 --feature-block-size 20000
```

수집기는 공식 BioStudies info·파일 페이지를 따라가며 원본 metadata·시도 이력과 해시를 보존합니다. 현재 고정 대상은 1,024 IDAT, 512 생물학적 시료, 256 완전한 개인 쌍입니다. source 변경은 새 버전과 재검토를 요구합니다. 원격 checksum이 없는 파일의 local SHA 검증을 원격 checksum 대조로 표현하지 않습니다.

분석은 QC·single-sample Noob 청크를 고정하고 디스크의 M/β 행 블록으로 모델을 적합한 뒤 전체 CpG에서 global empirical Bayes를 적용합니다. 각 블록에 별도의 eBayes를 적용하지 않습니다. M 검정과 β 효과·SE/CI를 분리하고 세 주대비 전체 BH-FDR과 대비별 FDR을 모두 보존합니다. 기술적 배치·나이/성별·혈구 조성 관련 결과는 주분석과 구분합니다. 식별 불가능한 민감도 모델은 미추정 상태와 이유를 남깁니다.

대형 분석은 한 번에 하나, 최대 16스레드로 실행합니다. 시작 전 메모리·공간을 확인하고 다른 작업이나 전역 R 환경을 변경하지 않습니다. 원본·환경·annotation이 달라진 checkpoint는 재사용하지 않습니다. DIRECT PLUS 파생값의 공개 이용조건이 확인되지 않았다면 분석은 내부 후보로 유지하고 공개 release에 넣지 않습니다.

## 4. 모델 재현과 최종 평가

`bridge_omics_helpers.R`의 새 expression archive는 expression·sample table·design·contrasts·voom object/weights·unmoderated/moderated fit을 보존합니다. `tests/check_expression_archive.R`는 저장 파일에서 다시 적합해 효과·P값·분산이 일치하고 가중치를 버리면 결과가 달라지는지 확인합니다. 과거 출력은 이 변경만으로 새로운 모델 보존 실적이 되지 않습니다.

원고는 실제 `science_kinds`와 검수 기록 수가 맞지 않으면 생성하지 않습니다. `bridge_source`와 `curation`을 임상 endpoint 정의로 설명하지 않습니다. 대상자·시료·결과 행 수와 관계 수는 별도 집계입니다.

개발70 입력 진단은 식품 선택 가능성과 정식 연구 노출을 찾는 범위이며 과학적 precision을 계산하지 않습니다. 최종30은 과학적 검수 반영과 큐레이션 동결 뒤 원문 기반 사람 gold·일회용 실행 승인·동일 release API가 준비된 경우에만 실행합니다. 원문 접근 실패, 불확실, 조회 실패와 확인된 근거 부재는 구분합니다.

최종 제출 준비 여부에는 사람 검수, 조회 평가, 권리, 재현, 성능, 공개 운영, 데이터·코드 버전 DOI와 저자 정보를 각각 확인합니다. 일부 자동 검사의 성공으로 전체를 완료 처리하지 않습니다.
