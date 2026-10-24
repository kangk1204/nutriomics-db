# 발현 모델 archive 보완 실행안

기준일: 2026-10-22. 이 문서와 preparer는 실행계획이다. R 대량 분석·원시파일 복사·과학적 승인을 수행하지 않는다. 14개 기존 분석의 사람 검수 대기 상태는 유지한다. GSE98645 실제 재실행은 부모 작업자의 별도 검증 범위로, 이 계획에서는 중복 실행하지 않는다.

## 확인한 범위

승인 서버의 `<project-root>/rebuild/20261017`에서 지정한 metadata/DONE/source_snapshot과 제한된 디렉터리 목록만 읽었다. 원시 tree 재귀 탐색·R 모델 실행은 하지 않았다. 작은 count 파일 두 개와 metadata/소스/lock은 현재 SHA를 확인했다. 대형 CEL/RDS/효과 파일은 완료 receipt의 SHA와 현재 크기·존재를 대조했고, 복사 단계에서 실제 전체 SHA를 다시 검사해야 한다.

5개 연구가 **6개 기존 분석 실행**에 대응한다. GSE56960의 식전 대비와 열량 조건 간 변화 차이는 같은 14명·168시료를 사용하며 독립 대상자로 합산하지 않는다.

| 연구/기존 출력 | 우선 입력·새 실행 | 보존할 조건 |
|---|---|---|
| GSE127530 / `gse127530-v1` | fixed count·original count·SOFT·검증된 count audit를 새 `data/raw/geo`와 새 출력에 배치하고 수정 producer 실행 | 5명·45회 측정, day/time 설계, 실제 voom weights·subject block·추정 correlation. 방문–보충제 대응 미확립이므로 성분별 효과로 해석하지 않음 |
| GSE56960 / `gse56960-v1` | 지정된 168개 CEL.gz와 원 sample_design/extraction manifest·lock만 복사 | 14명·168배열, RMA·사람 반복측정 상관·18개 식전 대비·기존 contrast별 BH |
| GSE56960 / `gse56960-dose-v3` | 위 CEL 입력을 공유하고 `--bridge-contrasts true`로 별도 새 출력 | 12개 열량 조건 간 변화 차이와 기존 joint BH. 체중군 자체를 무작위 비교로 바꾸지 않음 |
| GSE54325 / `gse54325-v1` | 7개 Agilent Feature Extraction 파일·GPL10332 원 주석·sample_design·lock | Cy3 baseline/Cy5 8주, processed channel signal·within-array loess·paired log ratio의 intercept. 7명을 14명으로 계산하거나 공개 대조군이 있는 것으로 표시하지 않음 |
| GSE13466 / `gse13466-raw-v2` | 원래 raw-RMA 분석에서 저장한 `reanalysis_inputs.rds` 11,155,606 bytes를 재사용 | 원 custom-CDF RMA expression 16,242×84, design 84×24, 21명. PUFA/SFA 변화 차이. 원시 정규화 재검증으로 표현하지 않음 |
| GSE27385 / `gse27385-raw-v2` | 원래 raw-RMA 분석의 RDS 5,690,409 bytes를 재사용 | expression 20,486×33, design 33×15, 11명 교차·period. Fish oil과 fenofibrate 약물 비교를 구분 |

GSE13466 RDS SHA는 `8af18091c67f9bc4f2bf4bf43276c61f2998ff1b2f3bd5425edb3d0e7415d1da`, GSE27385는 `e79aa30910924701076ed27ff95d2f6ddf29617b6114d842bc30ab9985a02417`이다. 이 저장 행렬·설계는 10월19일 별도 구조 검사에서도 확인했다. 현재 작업에서 RDS를 다시 읽지는 않았다.

**처리자료 우선은 depositor series로 교체한다는 뜻이 아니다.** 두 연구의 `--raw-cdf false`/`--raw-cel false`는 원래 raw-RMA와 다른 입력이다. archive 보완용으로 조용히 대체하지 않는다. 저장 RDS가 없거나 hash가 다르면 중단하고 원 tar·custom CDF부터 별도 실행안을 세운다.

## 코드와 산출물

`analyze_gse127530.R`, `analyze_gse56960.R`, `analyze_gse54325.R`에 archive 저장 구문만 추가했다. 원래 전처리·모델·반복측정·채널·대비·FDR은 유지한다. `expression_archive_helpers.R`는 실제 expression, sample/design/contrast, unmoderated/modified fit, voom 전체와 weights, 실제 block/correlation, 채널 방향을 보존한다. `model_archive`에 SHA·차원·저장 범위를 기록한다.

세 producer의 과거 원본은 공개 코드의 `source_snapshots/review_evidence/<original-sha>/`에서 참조한다. archive 추가 구문만 제외한 **전체 R AST가 원본과 일치**하는 회귀 검사를 통과했다. source bytes는 달라지므로 새 실행에는 `-archive20261022-v1` 버전을 사용한다. 원본 producer와 이전 효과를 덮어쓰지 않는다.

`replay_saved_expression_archive.R`는 두 microarray RDS에만 허용한다. 원 RDS·metadata·gene table·환경 lock의 SHA를 확인하고 저장된 정규화 행렬로 fit을 재생한다. 전 특징×대비의 효과·SE·CI·P·원 BH를 TSV 저장 정밀도(허용오차 1e-12)와 대조한다. 원 gene/pathway TSV를 재생성하지 않으며 pathway 재계산을 주장하지 않는다. 산출물은 새 `archive_metadata.json`과 실제 fitted 모델 RDS로, 독립 코호트나 중복 효과 행을 만드는 새 분석으로 자동 적재하지 않는다.

## 실행계획 생성과 서버 실행

```text
python scripts/prepare_expression_archive_replay.py --inventory <server-inventory-v2.json> --small-inputs <server-small-inputs.json> --destination-root <project-root>/rebuild/20261022/expression-archives --output-dir <new-plan-folder>
```

최종 plan의 `copy_manifest`, `workspace_code_uploads`, 각 run의 `command_argv`·`cwd`·`environment`를 사용한다. preparer 자체에는 복사·R 실행 기능이 없다. 선택 복사는 248개 파일, 470,961,595 bytes이며 두 GSE56960 분석의 168개 CEL은 한 번만 복사한다. 비교용 metadata·효과·Reactome 파일도 포함한다. 원시 전체 tree와 과거 승인 파일은 복사하지 않는다.

1. DIRECT PLUS 및 다른 대형 R 작업 종료와 예약을 확인한다.
2. 새 영구 디렉터리에 명시한 파일만 복사한다. 모든 파일을 source/destination 전체 SHA와 대조하고, 없는 파일·hash drift는 실패로 남긴다. 파일명이 같다는 이유로 대체하지 않는다.
3. 새 코드 SHA를 고정하고 프로젝트 R4.5.3·고정 library/lock에서 작은 검사를 먼저 실행한다. 한 번에 한 작업, 한 스레드와 24GiB 여유를 유지하며 parent launcher가 해당 process group RSS를 감시한다.
4. 생성된 여섯 명령을 순서대로 실행한다. GSE127530의 기존 검증 audit JSON은 지정된 새 출력 경로에 먼저 배치한다. producer의 상대 script 경로를 보존하도록 `cwd`는 새 루트다.
5. 세 producer 재실행은 모든 기존 정량 TSV를 대조한다. gzip header와 uncompressed bytes, feature/contrast/효과/통계/결측/식별자 검증을 구분한다. 실패하면 새 archive를 완료로 승격하지 않는다.
6. 두 saved-input replay는 gene numeric parity·archive roundtrip을 검사한다. 보존한 원 pathway table SHA는 재계산의 증거로 쓰지 않는다.
7. 실제 샘플·대비·voom weights·block/correlation·fitted model과 source/environment SHA가 남은 경우에만 archive 보완 완료로 기록한다. 과학적 검수는 계속 pending이다.

## 검사와 한계

실제 R 합성 검사에서 correlated weighted 모델 및 seven-person two-color identity 모델을 저장 후 refit해 계수·SE 관련 성분·분산·자유도·P가 일치했다. RNG도 바뀌지 않았다. 별도 CLI 합성 검사에서 saved-input 전체 수치·hash drift를 검사했다. 계획 단위 검사는 경로탈출, source 변경, count audit 미확인, 원시파일 수 불일치, 누락을 정상 결과로 바꾸지 않는지 검증한다.

이는 실제 여섯 분석의 실행 증거가 아니다. 새 실행·수치 대조·모델 archive 검사는 부모 작업자가 남길 receipt로 확인해야 한다. 통계식의 재현은 생물학적 타당성, 건강 효능, 독립 검증 또는 교수님 승인과 구분한다.

## 실제 실행에서 발견한 intercept 이름 호환 수정 — helper v2

첫 GSE127530 archive 실행은 limma의 정상적인 이름 변환으로 저장 단계에서 실패했다. `model.matrix`의 첫 계수명은 `(Intercept)`이고, `makeContrasts(levels=design)`가 만든 같은 위치의 행 이름은 `Intercept`였다. 기존 helper v1의 완전한 문자열 일치 검사가 이 경우까지 거부했다. 이는 저장 검증의 오류이며, 계수·대비 값이나 표본 순서를 바꿀 근거가 아니다.

helper v2는 **동일 위치의 `(Intercept)` → `Intercept`만** 허용한다. 실제 design·contrasts·unmoderated fit·moderated fit는 바꾸지 않고, 차이가 있는 위치와 원래 두 이름을 `contrast_design_axis_aliases`로 보존한다. 다른 계수 이름, 행 재정렬, 중복·누락 이름은 실패한다. identity-untransformed two-color 모델은 이름 일치를 계속 요구한다. archive 내부 버전은 `model-archive-20261022-v2`이며, 분석 schema와 원 producer 계산은 유지한다.

합성 R 검사는 실제 `model.matrix(~visit)`와 `makeContrasts(levels=design)`의 이름 차이를 재현했다. weighted·block/correlation fit를 저장하고 다시 적합했을 때 계수·분산·SE 관련 성분·P·자유도가 일치했으며, 실제 입력 행렬과 fit 객체 및 RNG 상태가 그대로 보존되었다. 틀린 intercept 이름·슬로프 재정렬·다른 별칭·이름 결측·중복·alias receipt 변조는 거부한다. 이 검사는 실제 GSE127530 정량 결과의 일치를 대신하지 않는다.

서버의 v1 동결 source와 실패 출력은 보존한다. 수정 helper를 포함한 별도 v2 source와 **새 출력 경로**, `-archive20261022-v2` 분석 버전으로 실패한 GSE127530만 재실행한다. 이때 기존 검증된 count audit를 새 출력에 먼저 복사하고 정확한 SHA를 확인해야 한다. 정상 진행 중인 다른 다섯 작업이나 기존 결과를 수정하지 않는다. 재실행 후 전체 원본 TSV·원래 분석 키·통계 범위를 대조하고, 실제 archive와 helper SHA를 기록해야 완료로 판단한다.
