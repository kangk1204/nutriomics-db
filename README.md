# NutriOmics

음식·영양성분·식이중재·유전자·후성유전 정보를 탐색하는 연구 DB와 웹 플랫폼입니다. 문헌 수집, 원문 기반 추출, 개체 ID 연결, 관계·임상 결과 모델 학습, 검수, 보고서 생성을 하나의 작업 흐름으로 제공합니다.

## 주요 기능

- **근거 DB:** 음식 조성, 사람 식이중재, 발현·DNA 메틸화 정량 결과, 조절영역·유전자 연결 탐색.
- **문헌 플랫폼:** 주제·기간·유전자·성분·종 검색, 구조화 요약, 원문 위치, 관계 지도, 모델 비교, HTML·Markdown·CSV·JSON 보고서.
- **문헌 알고리듬:** 사전·규칙 기반 엔진, BiomedBERT 개체 탐지, SapBERT ID 후보 검색, 선형·문맥형 ID 순위화, 관계 존재·유형·방향·원문 근거 학습.
- **임상 모델:** P/I/O 구절, 문장 역할, 중재·비교군·결과·군·용량·시점 연결.
- **운영:** 증분 수집, 버전별 결과, 개인 계정 검수, 작업 대기열, 체크포인트, 백업, 평가 manifest.

현재 웹 기본 엔진은 `heuristic-v1`입니다. `v2/v4/v5` 저장 결과와 과거 개발 평가를 조회할 수 있으며, `nutriomics-evidence-v8`은 학습 중입니다.

## 설치

Python 3.12, Node.js 24, pnpm 11.25.0을 사용합니다. R/Bioconductor 분석 환경은 별도로 구성합니다.

```bash
git clone https://github.com/kangk1204/nutriomics-db.git
cd nutriomics-db
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]" -e "./literature_service[dev]"
pnpm -C frontend/explorer install --frozen-lockfile
pnpm -C frontend/explorer build
```

Windows에서는 `py -3.12 -m venv .venv`와 `.\.venv\Scripts\Activate.ps1`로 환경을 활성화합니다.

## 문헌 수집·처리

```bash
export LITERATURE_HOME="$PWD/literature_service/runtime"
export LITERATURE_CONFIG="$PWD/literature_service/config/topics.json"
nutri-literature init
nutri-literature collect
nutri-literature process --stage oa --limit 100
nutri-literature process --stage extract --engine heuristic-v1 --limit 100
nutri-literature report --engine heuristic-v1
```

`--limit`는 한 번에 처리할 작업 수입니다. 대규모 수집은 다음 명령으로 실행합니다.

```bash
python literature_service/scripts/platform_expand_v8.py collect --continuous
python literature_service/scripts/platform_expand_v8.py reconcile
```

기본 6개 주제와 영양유전체·식이중재 검색식은 [topics-v8.json](literature_service/config/topics-v8.json)에 있습니다. PubMed 서지·초록과 공개 XML 본문을 수집하며, 원문·라이선스·검색식·해시·개정 이력을 저장합니다.

## 웹 플랫폼 실행

문헌 DB에서 공개·내부 snapshot을 생성합니다.

```bash
python -m scripts.build_literature_snapshot \
  --source "$LITERATURE_HOME/data/literature.sqlite3" \
  --output "$PWD/literature_service/runtime/snapshots/dev-public" \
  --release-id literature-dev-public
python -m scripts.build_literature_snapshot \
  --source "$LITERATURE_HOME/data/literature.sqlite3" \
  --output "$PWD/literature_service/runtime/snapshots/dev-internal" \
  --release-id literature-dev-internal --internal
```

기존 NutriOmics release와 공개 문헌 snapshot을 연결합니다.

```bash
export NUTRIOMICS_PUBLIC_RELEASE="/absolute/path/to/release"
export NUTRIOMICS_ALLOW_CANDIDATE=1
export NUTRIOMICS_LITERATURE_SNAPSHOT="$PWD/literature_service/runtime/snapshots/dev-public"
export NUTRIGENOMICS_EXPLORER_DIST="$PWD/frontend/explorer/dist"
python -m uvicorn nutrigenomics.public_service:app_factory \
  --factory --host 127.0.0.1 --port 8064
```

- 근거 DB: <http://127.0.0.1:8064/explorer/>
- 문헌·모델 평가: <http://127.0.0.1:8064/explorer/?view=literature>
- 실행 확인: <http://127.0.0.1:8064/healthz>

내부 검수·작업 관리는 별도 터미널에서 실행합니다.

```bash
export NUTRIOMICS_INTERNAL_LITERATURE_SNAPSHOT="$PWD/literature_service/runtime/snapshots/dev-internal"
python -m uvicorn nutrigenomics.literature_management:app_factory \
  --factory --host 127.0.0.1 --port 8063
```

계정·권한 설정, worker와 서버 실행, v8 입력·학습·평가는 [문헌 실행 안내](docs/LITERATURE_RUNBOOK_ko.md)를 참고하세요. 개발 서버의 SSH 포워딩은 [로컬 접속 안내](docs/LOCAL_TUNNEL_ko.md)에 정리했습니다. 독립 HTML은 내보내기 시점의 snapshot을 포함하고, 서버형 웹은 연결된 snapshot과 모델 결과를 조회합니다.

## 현재 성능

기준일은 **2026-10-24**입니다. 아래 관계 평가는 **동일 개발 100편·정답 관계 154건**, 원시 표준 ID와 관계 유형 일치 기준입니다. 방향성은 별도 평가합니다.

| 구성 | TP | FP | FN | 정밀도 | 재현율 | F1 |
|---|---:|---:|---:|---:|---:|---:|
| chatgpt-repair-v2 | 64 | 64 | 90 | 50.00% | 41.56% | 45.39% |
| chatgpt-repair-v3 | 63 | 57 | 91 | 52.50% | 40.91% | 45.99% |
| chatgpt-repair-v4 | 50 | 54 | 104 | 48.08% | 32.47% | 38.76% |
| chatgpt-repair-v5-r1 | 65 | 63 | 89 | 50.78% | 42.21% | 46.10% |
| relation-repair-v6-gpu-r2 | 평가 대기 | — | — | — | — | — |
| nutriomics-evidence-v8 | 평가 대기 | — | — | — | — | — |

개발 목표는 **TP ≥65, 재현율 ≥65/154(42.21%), 정밀도 ≥85%**입니다. 현재 최고 재현율 구성은 TP 65·FP 63이며, 정밀도 목표까지 추가 개선이 필요합니다. 위 `chatgpt-repair-*` 평가 구성과 웹 기본 엔진 `heuristic-v1`은 별도로 관리합니다.

개체 탐지와 임상 구절 진단은 다음과 같습니다.

| 과제·자료 | 평가 범위 | 정밀도 | 재현율 | F1 |
|---|---|---:|---:|---:|
| 유전자 NER 단독 | v8 run4·공식 학습 415편 OOF | 79.02% | 88.17% | 83.35% |
| 유전자 NER 단독 | v8 run5·공식 학습 415편 OOF | 79.11% | 87.46% | 83.07% |
| 물질 NER 단독 | v8 run4·공식 학습 415편 OOF | 80.64% | 85.49% | 83.00% |
| 물질 NER 단독 | v8 run5·공식 학습 415편 OOF | 80.43% | 85.54% | 82.91% |
| EBM-NLP 결과 구절 | 개발 200편·새 PIO + 기존 순위 모델 | 70.43% | 44.29% | 54.38% |
| EvidenceOutcomes 결과 구절 | 개발 200편·새 PIO + 기존 순위 모델 | 41.90% | 50.20% | 45.68% |

평가 집합별 건수·단위·해시는 [상태·성능 JSON](docs/status/literature-2026-10-24.json)에 저장했습니다. [v8 구현 기록](literature_service/docs/implementation-v8.md)에서 모델·사전·규칙·메모리 수정과 실행 이력을 확인할 수 있습니다.

## 수집 규모와 진행 중인 학습

수집 DB의 2026-10-24 관측값입니다. 항목별 관측 시각을 함께 표시합니다.

| 항목 | 건수 | 관측 시각(KST) |
|---|---:|---|
| 고정 검색 PMID 합집합 | 133,261편 | 11:22·고정 검색 목록 대조 |
| 저장된 고유 문헌 | 42,967편 | 11:22 |
| 초록 확보 | 41,526편 | 11:22 |
| 공개 본문 확보 | 2,203편 | 11:22 |
| 현재 원문 일치 추출·마지막 집계 | 27,597편 | 10:49 |

| 실행 | 현재 단계 |
|---|---|
| v8 run5 | NER 완료·ID 중첩 검증 학습 중·상류 NER 모델 9개·프로필 7개·ID 외부 fold 0/5 (11:22 KST) |
| v8 보조 학습 | DrugProt 외부 fold 3/5·학습 모델 파일 21개 완료·CG 다음 단계 (11:22 KST) |
| 기존 r2 | 관계 외부 fold 2/5 완료·학습 실행 중 (11:22 KST) |

v8 run5는 중첩 ID 후보의 메모리 사용을 개선한 실행입니다. 실제 후보 208,171건의 값·해시를 유지한 메모리 재현은 최고 2.77GiB였고, Linux 회귀검사 54개를 통과했습니다. 학습·평가 작업은 버전별 소스와 체크포인트로 이어집니다. 추가 Sol·Luna 호출 예산은 소진됐으며 현재 학습은 저장 응답과 공개 주석 자료를 사용합니다.

남아 있던 **NLP 검사 5건은 10:41 KST 고정 Linux 환경에서 모두 통과**했습니다. [개별 검사·의존성·해시](docs/status/nlp-validation-2026-10-24.json)를 저장했습니다. 학습 후속 작업은 서버 재접속·중단 재개, 100편 예측 회수, 두 번 재채점, 보고서·README 갱신과 현재 브랜치 커밋·푸시로 연결했습니다. 실행 방법은 [후속 작업 안내](docs/LITERATURE_RUNBOOK_ko.md)에 있습니다.

ID 중첩 검증은 각 학습 분할에서 상류 NER 후보를 다시 생성합니다. 위 모델·프로필 수에는 이 과정의 산출물도 포함합니다. 후속 작업은 접속 실패 시 체크포인트를 보존하며 재접속합니다.

최신 성능·학습·검사 보고서: [HTML](docs/reports/v8-progress-20261024-1122/report.html) · [Markdown](docs/reports/v8-progress-20261024-1122/report.md) · [CSV](docs/reports/v8-progress-20261024-1122/report.csv) · [JSON](docs/reports/v8-progress-20261024-1122/report.json). v8의 같은 100편 재평가는 ID·관계 학습과 예측 완료 후 채점합니다.

## 남은 작업

- v8 ID·관계 학습, 모델 동결, 동일 100편 전체 예측·재평가, 기존 구성과 비교.
- 133,261편 수집 전수 대조, 공개 본문 재확인, 엔진별 전수 처리.
- 임상 군·비교군·용량·시점 연결의 정량 평가.
- GNorm2·MedCPT 비교, 영양 문헌 추가 사전학습 실험.
- 동결 구성의 BC8 평가, 영양 문헌 정답 주석, 검수 시간 측정.
- 동일 artifact의 7일 운영, 중단·복원·전환·복귀 검증.

## 검증

```bash
python -m pytest tests/test_literature_platform.py tests/test_literature_management.py
python -m pytest tests/test_v8_streaming_json.py tests/test_v8_id_lifecycle.py
python -m pytest tests/test_evidence_v8_contract.py tests/test_evidence_v8_native.py
pnpm -C frontend/explorer typecheck
pnpm -C frontend/explorer build
```

## 자료·release와 라이선스

`dataset-metadata.json`에 release별 집계가, manifest에 파일 해시가 저장됩니다. 음식 조성, 중재, 오믹스 결과, 문헌, 관계 기록은 각 단위로 집계합니다. 정량 결과의 키는 `series_accession, feature_type, feature_id, contrast, analysis_version`입니다.

공개 release 생성·배포는 [운영 안내](docs/PUBLICATION_RUNBOOK_ko.md), 공개 원본 재구축은 [재구축 안내](docs/WORKBENCH_PUBLIC_REBUILD_ko.md), 연구 검수는 [검수 흐름](docs/WORKBENCH_SUBMISSION_REVIEW_ko.md), 식품·유전자 검색은 [식품 검색](docs/FOOD_SEARCH_KO.md)·[유전자 검색](docs/gene-search-ko.md)을 참고하세요.

소프트웨어는 [MIT](LICENSE), 공개 가능한 자체 큐레이션·파생 분석은 CC BY 4.0입니다. 원본 데이터와 의존성의 이용조건은 [LICENSE-DATA.md](LICENSE-DATA.md)에 정리했습니다. DB·모델 가중치·원문·실행 캐시·인증정보는 로컬 또는 서버의 버전별 저장소에서 관리합니다.
