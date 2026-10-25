# NutriOmics

음식의 성분과 사람 식이중재 연구의 유전자 발현·DNA 메틸화 결과를 함께 살펴보는 연구용 데이터베이스입니다. 음식명과 섭취량으로 성분량을 계산하고, 관련 연구의 용량·기간·조직·측정 결과를 문헌 출처와 함께 확인할 수 있습니다.

문헌 검색, 한국어 구조화 요약, 원문 인용, 근거 지도와 모델 평가를 같은 웹 화면에 통합했습니다.

[English](README.en.md) · [DB 구조](docs/DB_STRUCTURE.ko.md) · [검토 안내](docs/REVIEWER_GUIDE.ko.md) · [설치 방법](docs/GETTING_STARTED.ko.md) · [문헌 성과표](docs/reviewer/RESULTS.md)

![NutriOmics의 데이터 구성과 연결](docs/figures/db-structure/보고서용_DB_구조도.png)

## 무엇을 확인할 수 있나요?

| 기능 | 제공하는 정보 |
|---|---|
| 음식·성분 검색 | 식품명, 조리 상태, 성분량·단위와 출처 |
| 섭취량 계산 | 입력한 식품과 g 단위 분량에 해당하는 성분량 |
| 사람 연구 탐색 | 음식·보충제, 시험 용량, 섭취 기간, 조직과 비교 조건 |
| 오믹스 결과 조회 | 유전자 발현·DNA 메틸화·경로의 변화와 통계값 |
| 문헌 탐색 | 주제·유전자·물질별 검색, 한국어 요약, 원문 위치와 표준 ID |
| 근거 지도·검수 | 논문별 관계 후보, 승인·수정·기각 기록 |
| 결과 내보내기 | HTML·Markdown·CSV·JSON 보고서 |

예를 들어 요구르트 150 g을 입력하면 해당 식품 기록으로 성분량을 계산합니다. 관련 연구가 요구르트 800 g을 사용했다면 연구 용량, 측정 시점과 관측 반응을 함께 보여줍니다. 성분량 계산과 연구에서 관찰한 생물학적 반응은 각각의 근거로 제공합니다.

## DB 구축 규모

2026년 10월 22일 자료를 10월 24일 확인한 웹 제공용 구성입니다. 집계 정의와 파일 해시는 [기준 수치](docs/reference-data-summary.json)에 있습니다.

| 항목 | 규모 | 집계 기준 |
|---|---:|---|
| 식품 | **5,794개** | 원본 코드·조리 상태를 구분한 식품 기록 |
| 식품 조성 | **368,208건** | 식품·성분·출처별 값과 측정·계산·결측 상태 |
| 연구 묶음 | 등록 9개, 재분석 자료 보유 8개 | 연구 등록 단위 |
| 비교 조건 | 53개 | 군·시점·분석 조건별 대비 |
| 분석 실행 | 14개 | 민감도 분석 등을 포함한 기존 실행 |
| 정량 결과 | **6,254,477행** | 측정 특징·대비·분석 버전별 결과 |

연구실 자료에 추가한 한식 50종·조성 1,200건은 이 표의 범위 밖입니다. 과제 전체의 대용량 정제 DB는 [통합 보고서 저장소](https://github.com/kangk1204/nutriomics-final-report)에서 별도로 집계합니다.

## 문헌 분석 결과

2026년 10월 24일 12:46 KST 기준 **44,828편**을 저장했으며, **28,977편**에 저장된 초록 또는 본문과 대응하는 추출 결과가 있습니다. 초록 확보는 43,365편, 공개 본문 확보는 2,314편, 등록 리포트는 9건입니다. 확대 검색의 133,261개 PMID 목록은 수집 대상 목록으로 관리합니다.

| 과제 | 결과 | 평가 자료·조건 |
|---|---|---|
| 물질·유전자 관계 존재 예측 | **AUROC 0.876**, 95% 구간 0.851~0.898 | 공개 400편 중 적격 224편·4,362쌍, 정답 개체·ID 제공 |
| 같은 조건의 TF-IDF 비교 | AUROC 0.666 | 위와 동일한 쌍 |
| 유전자 이름 탐지 | F1 83.35%, 정밀도 79.02%, 재현율 88.17% | 공식 학습 415편, 문헌 단위 교차검증 |
| 물질 이름 탐지 | F1 83.00%, 정밀도 80.64%, 재현율 85.49% | 공식 학습 415편, 문헌 단위 교차검증 |
| 전체 자동 관계 추출 | **F1 46.10%**, 정밀도 50.78%, 재현율 42.21% | 반복 개발 100편·정답 154건, 개체·ID·관계 유형 자동 추출 |
| 임상 결과 구절: EBM-NLP | F1 54.38% | 개발 200편, 결과 구절 토큰 평가 |
| 임상 결과 구절: EvidenceOutcomes | F1 46.24% | 개발 200편, 결과 구절 토큰 평가 |

관계 존재 예측은 주어진 물질·유전자 쌍에 문헌 근거가 있는지 판단합니다. 전체 자동 추출은 이름 탐지부터 표준 ID와 관계 유형까지 평가하며, 정탐 65건·오탐 63건·누락 89건이었습니다. AUROC는 점수의 구분 능력, 정밀도는 예측 중 정답 비율, 재현율은 정답 중 찾아낸 비율, F1은 정밀도·재현율의 조화평균입니다.

탐지·추출 표는 각 과제에서 완료된 구성의 최고 F1을 요약한 것으로, 과제별 모델 구성이 다릅니다. 평가 자료, 전체 예측과 원수치는 [성과표](docs/reviewer/RESULTS.md), [CSV](docs/reviewer/metrics.csv), [출처·해시](docs/reviewer/summary.json)에서 확인할 수 있습니다.

## 코드와 데이터 구성

| 위치 | 역할 |
|---|---|
| `nutrigenomics/` | 자료 수집·정제·분석, DB와 FastAPI |
| `literature_service/` | 문헌 수집·요약·개체·관계 추출과 평가 |
| `frontend/explorer/` | 식품·연구·문헌을 탐색하는 React 화면 |
| `docs/` | 자료 사전, 분석 방법, 성과표와 운영 안내 |
| `tests/` | 계산·조회·출처 보존·서비스 동작 검사 |

문헌 기능에는 사전·규칙, 생의학 인코더와 관계·임상 구절 분류기를 사용합니다. 웹의 기본 요약 엔진은 `heuristic-v1`이고, 자동 관계 추출의 대표 평가 구성은 `chatgpt-repair-v5-r1`입니다. 구성별 모델과 실행 내역은 [모델 목록](docs/reviewer/VERSIONS.md)에 있습니다.

코드, 작은 시험 자료, 요약표와 재구축 절차를 GitHub에서 제공합니다. 전체 DB와 모델은 별도 연구 환경에 보관합니다. 외부 공개 서비스와 전체 데이터 배포는 [공개 절차](docs/PUBLICATION_RUNBOOK_ko.md)에 따라 준비합니다. 핵심 기록 263건의 전문가 검수와 최종 30개 질의 평가는 [검수 안내](docs/WORKBENCH_SUBMISSION_REVIEW_ko.md)에서 관리합니다.

## 설치와 실행

Git, Python 3.12와 uv를 설치한 뒤 코드와 작은 시험 자료를 확인할 수 있습니다.

```sh
git clone https://github.com/kangk1204/nutriomics-db.git
cd nutriomics-db
uv sync --frozen --extra dev
uv run pytest tests/test_composition_policy.py tests/test_public_service_isolation.py -q
```

웹 빌드는 Node.js 24와 pnpm 11.25.0을 사용합니다. 자료 준비부터 웹 실행까지는 [설치 안내](docs/GETTING_STARTED.ko.md), 문헌 DB·모델 연결은 [문헌 실행 안내](docs/LITERATURE_RUNBOOK_ko.md)를 따릅니다. 문헌 기능은 현재 `main`에 통합되어 있습니다.

준비된 서비스에서 다음 API로 수집 규모와 문헌을 조회합니다.

```sh
curl --fail-with-body "http://127.0.0.1:8064/api/v1/explorer/literature/stats"
curl --fail-with-body "http://127.0.0.1:8064/api/v1/explorer/literature/papers?page=1&page_size=5"
```

| 목적 | 안내 |
|---|---|
| DB 표·식별자·결측값 이해 | [자료 사전](docs/PUBLIC_DATA_DICTIONARY.md) |
| 원자료로 DB 재구축 | [재구축 방법](docs/WORKBENCH_PUBLIC_REBUILD_ko.md) |
| 자료 갱신 | [갱신 방법](docs/WORKBENCH_UPDATE_ko.md) |
| Python·PowerShell·curl API 사용 | [문헌 API](docs/LITERATURE_API_ko.md) |
| 웹 통합과 동작 확인 | [통합 검증 기록](docs/MAIN_LITERATURE_INTEGRATION_ko.md) |
| 보고서용 구조 그림 | [PNG·SVG·PDF](docs/figures/db-structure/README.md) |

## 사용 조건과 인용

소프트웨어는 [MIT](LICENSE), 공개 가능한 자체 큐레이션·파생 분석은 CC BY 4.0을 적용합니다. 원자료와 문헌은 제공기관·문헌별 이용조건을 따릅니다. 상세 조건은 [LICENSE-DATA.md](LICENSE-DATA.md)에 있습니다.

연구에 사용한 경우 저장소 주소와 사용한 commit을 함께 기재해 주세요. 오류와 문의는 [GitHub Issues](https://github.com/kangk1204/nutriomics-db/issues)에 남길 수 있습니다.
