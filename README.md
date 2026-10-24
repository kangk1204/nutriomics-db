# NutriOmics

음식의 성분과 사람 식이중재 연구의 유전자 발현·DNA 메틸화 결과를 함께 살펴보는 연구용 DB입니다.

[English](README.en.md) · [DB 구조와 쉬운 설명](docs/DB_STRUCTURE.ko.md) · [검토 안내](docs/REVIEWER_GUIDE.ko.md) · [설치 방법](docs/GETTING_STARTED.ko.md)

음식명과 섭취량으로 성분량을 계산하고, 관련 연구에서 어떤 음식·보충제를 어느 용량으로
얼마 동안 섭취했는지 확인할 수 있습니다. 관찰 결과에는 조직, 측정 시점, 비교 조건과 출처를 함께 남깁니다.

![NutriOmics DB의 구성과 자료 연결](docs/figures/db-structure/보고서용_DB_구조도.png)

음식의 성분표에서 성분량을 계산한 뒤, 관련 사람 연구의 조건과 발현·메틸화·경로 결과를 찾아갑니다.
건강지표는 같은 연구의 별도 결과로 연결합니다. 기전·계산 예측은 별도 층으로 구분합니다.
그림은 자료를 찾는 구조이며, 모든 음식의 건강 효과가 확인됐다는 뜻은 아닙니다.

문헌 분석: [최종 성과](docs/reviewer/RESULTS.md) · [지표 해설](#성능-수치를-읽는-방법) · [API 사용법](docs/LITERATURE_API_ko.md)

## 사용 예

요구르트 150 g을 입력하면 선택한 식품 기록의 성분량을 계산합니다.
관련 연구가 요구르트 800 g을 사용했다면, 입력량과 시험량의 차이 및 측정 시점을 함께 보여줍니다.
800 g 실험의 발현 변화를 150 g의 개인 반응으로 환산하지 않습니다.

음식뿐 아니라 성분·유전자 이름과 식별자로도 검색할 수 있습니다. 검색 결과에서 관련 중재,
해당 대비의 발현·DNA 메틸화·경로 결과와 원문으로 이동합니다.
연결 근거가 없거나 조건이 확인되지 않은 항목은 자료 부족으로 표시합니다.

## 먼저 확인할 자료

| 확인하려는 내용 | 안내 |
|---|---|
| DB가 무엇을 저장하고 어떻게 연결하는가 | [구조와 쉬운 설명](docs/DB_STRUCTURE.ko.md) |
| 구축 범위와 검수 상태 | [검토 안내](docs/REVIEWER_GUIDE.ko.md) |
| 전체 DB 없이 코드가 동작하는가 | [설치와 작은 검사](docs/GETTING_STARTED.ko.md) |
| 표·분석 결과·결측값의 의미 | [자료 사전](docs/PUBLIC_DATA_DICTIONARY.md) |
| 원본 확보와 재분석·갱신 방법 | [재구축](docs/WORKBENCH_PUBLIC_REBUILD_ko.md) · [갱신](docs/WORKBENCH_UPDATE_ko.md) |
| 이번 코드의 변경과 검사 범위 | [2026-10-24 변경 기록](docs/UPDATES_20261024.ko.md) |
| 보고서에 넣을 그림과 문구 | [PNG·SVG·PDF](docs/figures/db-structure/README.md) · [본문 문구](docs/REPORT_TEXT.ko.txt) |

## 구축 규모

2026년 10월 24일 확인한 공개 후보 자료를 기준으로 합니다.

| 항목 | 수량 | 집계 기준 |
|---|---:|---|
| 식품 | 5,794개 | 조리상태·원본 코드가 구분된 식품 기록 |
| 식품 조성 | 368,208건 | 식품·성분·출처별 기록. 측정·계산·추정·결측 상태 포함 |
| 사람 연구 | 등록 9개 | 재분석 자료가 있는 연구 묶음은 8개 |
| 대비 | 53개 | 시점·군·분석 조건별 비교 |
| 기존 분석 실행 | 14개 | 같은 대상자를 이용한 분석·민감도 분석을 포함 |
| 선택된 정량 결과 | 6,254,477행 | 특징·대비·분석 버전별 값 |

결과 행과 관계 수는 대상자 수나 검증된 건강 효과의 수와 다릅니다.
연구실 판에 추가한 한식 50종·조성 1,200건은 위 공개 후보 수치에 포함하지 않았습니다.
GSE56960의 경로 교정은 기존 50,550행을 새 출력 버전으로 선택하는 방식이며, 총 결과 행 수를 늘리지 않습니다.
집계의 출처·해시는 [기준 수치 파일](docs/reference-data-summary.json)에 있습니다.

## 현재 제공 범위

이 저장소에는 자료 수집·분석·DB 구축·갱신·웹 조회 코드와 작은 시험 자료가 있습니다.
한국어 식품·유전자 검색, 성분 계산, 연구·결과 조회, 독립 HTML 내보내기 코드를 포함합니다.
자료가 준비된 환경에서는 로그인 없이 탐색할 수 있습니다.

전체 DB 다운로드와 데이터 DOI, 외부 공개 HTTPS 서비스는 아직 제공하지 않습니다.
핵심 263개 기록과 새 경로 출력 2개의 수록 판정, 공개 이용조건 확인,
최종 30개 질의의 조회 평가는 남아 있습니다. 코드 공개와 사람의 과학적 승인은 구분합니다.
문헌 검색·요약·관계 후보·모델 평가는 같은 웹과 API에서 제공합니다. 자세한 사용법은 아래 문헌 플랫폼 안내에 있습니다.

## 코드 확인하기

Git, Python 3.12와 uv를 설치한 뒤 실행합니다.

```sh
git clone https://github.com/kangk1204/nutriomics-db.git
cd nutriomics-db
uv sync --frozen --extra dev
uv run pytest tests/test_composition_policy.py tests/test_public_service_isolation.py -q
```

작은 시험 자료로 성분 계산과 읽기 전용 서비스의 동작을 확인합니다.
전체 데이터 없이 실행할 수 있습니다. 웹 화면 빌드와 준비된 DB 실행은 [설치 방법](docs/GETTING_STARTED.ko.md)에 있습니다.

## 사용 조건과 인용

코드는 [MIT](LICENSE)를 적용합니다. 외부 자료는 제공기관의 이용조건을 따르며,
자체 큐레이션·분석 결과의 조건은 [LICENSE-DATA.md](LICENSE-DATA.md)에 구분했습니다.
버전 DOI가 등록되기 전에는 저장소 주소와 사용한 commit을 함께 기재해 주세요.

오류와 문의는 [GitHub Issues](https://github.com/kangk1204/nutriomics-db/issues)에 남길 수 있습니다.

## 문헌 플랫폼 · 최종 대표 성과

**2026-10-24 기준, 문헌 44,828편을 확보하고 28,977편에서 저장 원문과 대응하는 추출 결과를 구축했습니다.** 관계 존재 예측은 정답 개체를 제공한 224편·4,362개 물질–유전자 쌍에서 **AUROC 0.876**으로, 동일 조건의 TF-IDF 기준선 **0.666**보다 높았습니다.

| 평가 항목 | 대표 결과 | 평가 조건 |
|---|---|---|
| 관계 존재 예측 | **AUROC 0.876** · 95% 구간 0.851–0.898 | 공개 400편 중 적격 224편·4,362쌍, 정답 개체·ID 제공 |
| 유전자 이름 탐지 | **F1 83.35%** · 정밀도 79.02% · 재현율 88.17% | 공식 학습 415편의 문헌 단위 교차검증 |
| 물질 이름 탐지 | **F1 83.00%** · 정밀도 80.64% · 재현율 85.49% | 공식 학습 415편의 문헌 단위 교차검증 |
| 전체 자동 관계 추출 | **F1 46.10%** · 정밀도 50.78% · 재현율 42.21% | 반복 개발 100편·정답 154건, 개체·표준 ID·관계 유형 자동 추출 |
| 임상 결과 구절 · EBM-NLP | **F1 54.38%** · 정밀도 70.43% · 재현율 44.29% | 개발 200편, 결과 구절 토큰 평가 |
| 임상 결과 구절 · EvidenceOutcomes | **F1 46.24%** · 정밀도 42.56% · 재현율 50.61% | 개발 200편, 결과 구절 토큰 평가 |

대표값은 완료된 평가 중 **각 과제의 F1이 가장 높은 구성 전체**를 사용했습니다. 관계 존재 예측은 AUROC로 보고하며, 정답 개체를 제공한 분류와 전체 자동 추출을 구분합니다. 과제별 대표 구성은 하나의 새 모델로 합친 결과가 아닙니다. 100편 관계 평가는 반복 사용한 개발 자료이며, 임상 구절 점수의 단위는 결과 구절의 토큰입니다.

전체 자동 추출의 대표 구성은 **예측 128건 중 정탐 65건·오탐 63건·누락 89건**입니다. [최종 성과표와 평가 조건](docs/reviewer/RESULTS.md) · [인쇄용 HTML](docs/reviewer/report.html) · [CSV](docs/reviewer/metrics.csv) · [근거·해시 JSON](docs/reviewer/summary.json)에서 수치와 출처를 확인할 수 있습니다.

## 성능 수치를 읽는 방법

문헌 분석은 **이름 찾기 → 표준 ID 연결 → 관계 존재 판단 → 관계 유형 추출** 순서로 진행됩니다. 예를 들어 논문에서 ‘vitamin D’와 ‘VDR’을 찾아 각각의 ID를 연결한 뒤, 두 개체 사이에 어떤 관계가 기술됐는지 판단합니다. 이름만 맞히는 것보다 이 과정을 모두 자동으로 수행하는 평가가 더 많은 조건을 요구합니다.

| 지표 | 전문적 의미 | 이 결과에서는 어떻게 읽나요? |
|---|---|---|
| **AUROC** | 관계가 있는 쌍을 없는 쌍보다 높은 점수로 구분하는 능력. 0.5는 무작위 수준, 1.0은 완전한 구분 | **0.876**은 무작위로 고른 관계 있음·없음 쌍에서, 관계 있는 쌍의 점수가 더 높을 확률이 약 **87.6%**라는 뜻입니다. ‘전체 예측의 87.6%가 정답’이라는 정확도와는 다릅니다. 동점은 절반의 성공으로 계산합니다. |
| **정밀도 · Precision** | 모델이 있다고 예측한 관계 중 실제 정답인 비율 | 전체 자동 평가에서 **65 ÷ 128 = 50.78%**입니다. 연구자가 검토할 후보 중 맞는 관계의 비중을 나타냅니다. |
| **재현율 · Recall** | 실제 정답 관계 중 모델이 찾아낸 비율 | 정답 **154건 중 65건**을 찾아 **42.21%**입니다. 나머지 **89건**은 찾지 못했거나 ID·유형이 정답과 달랐습니다. |
| **F1** | 정밀도와 재현율의 조화평균. 한쪽만 높으면 함께 낮아지는 균형 지표 | 전체 자동 추출의 **46.10%**는 정밀도 50.78%와 재현율 42.21%를 함께 요약한 값입니다. 단순 평균이나 정확도와는 다릅니다. |
| **95% 구간** | 문헌을 단위로 재표집했을 때 성능 추정의 변동 범위를 요약 | AUROC의 **0.851–0.898**은 문헌 단위 bootstrap으로 산출한 구간입니다. 개별 예측이 맞을 확률을 뜻하지 않습니다. |

**AUROC 0.876의 핵심 성과는 같은 평가 조건에서 TF-IDF 0.666보다 관계의 유무를 더 잘 구분했다는 점입니다.** 차이는 AUROC **0.210**이며, 공개 400편 중 적격 224편의 4,362쌍을 동일하게 비교했습니다. 이 평가에서는 개체 위치와 ID를 정답으로 제공하므로 관계 판단 모델의 성능을 따로 측정합니다. 전체 자동 추출은 개체 탐지·ID 연결·관계 유형까지 모두 맞아야 정탐으로 계산하며, 그 결과를 별도 행에 제시했습니다.

유전자·물질 탐지의 F1 약 **83%**는 이름의 **원문 경계를 찾는 성능**입니다. 표준 ID나 생물학적 관계까지 83% 맞혔다는 의미는 아닙니다. 임상 결과 구절의 F1은 ‘혈압 감소’처럼 결과가 기술된 **텍스트 범위를 찾는 성능**이며, 치료 효과의 크기나 군·용량·시점 연결의 정확도와 구분합니다. 과제마다 평가 단위와 정답 조건이 달라 서로 다른 행의 점수를 같은 척도로 순위화하지 않습니다.

문헌 단위 교차검증은 일부 논문을 학습에서 제외해 평가하고, 이 과정을 나누어 반복한 뒤 제외됐던 논문들의 예측을 모으는 방법입니다. 표의 **OOF**는 이렇게 해당 논문을 학습에 사용하지 않은 분할의 예측을 뜻합니다. 새로운 영양 문헌을 전문가가 직접 검증한 평가와는 구분합니다.

수집 문헌 **44,828편**은 시스템이 확보한 자료의 규모이고, 평가 문헌 **100편·154건** 또는 **224편·4,362쌍**은 모델의 성능을 측정한 자료입니다. 따라서 수집 편 수를 곧바로 정답 관계 수나 전문가 검증 건수로 해석하지 않습니다.

## 문헌 자료 규모

아래 수치는 **2026-10-24 12:46 KST**의 고정 수집 기록입니다.

| 항목 | 건수 |
|---|---:|
| 저장된 고유 문헌 | **44,828편** |
| 초록 확보 | **43,365편** |
| 공개 본문 확보 | **2,314편** |
| 현재 원문과 대응하는 추출 결과를 보유한 문헌 | **28,977편** |
| 문헌 리포트 등록 | **9건** |
| 확대 검색의 고유 PMID 목록 | **133,261편** |

검색 목록과 실제 확보 문헌은 별도로 집계합니다. 원문 대응 추출 수는 초록 또는 공개 본문의 해시와 일치하는 결과가 최소 한 엔진에 있는 고유 문헌 수입니다.

## 웹에서 할 수 있는 일

- **문헌 탐색:** 주제·연도·유전자·물질·종·공개 본문 여부와 처리 상태로 검색합니다.
- **논문 상세:** 한국어 구조화 요약, 원문 인용·문자 위치, 개체 ID와 연구 조건을 확인합니다.
- **근거 지도:** 음식·성분·유전자·건강결과와 논문별 관계 후보를 탐색합니다.
- **성능·보고서:** 완료된 평가의 대표 결과와 버전별 비교를 확인하고 HTML·Markdown·CSV·JSON으로 내려받습니다.
- **내부 검수·관리:** 개인 계정으로 승인·수정·기각·미확인을 기록하고 작업 상태를 조회합니다.

소스는 `literature_service`의 수집·추출·학습 모듈, `nutrigenomics`의 FastAPI, `frontend/explorer`의 React 화면으로 구성합니다. 사전·규칙, 생의학 인코더, ID 순위 모델과 관계·임상 구절 분류기를 사용하며, 저장한 모델 응답과 공개 주석 자료를 함께 관리합니다.

## 설치와 실행

Python 3.12, Node.js 24, pnpm 11.25.0을 사용합니다. 문헌 기능을 포함한 최신 main을 내려받습니다.

```bash
git clone https://github.com/kangk1204/nutriomics-db.git
cd nutriomics-db
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]" -e "./literature_service[dev]"
pnpm -C frontend/explorer install --frozen-lockfile
pnpm -C frontend/explorer build
```

Windows PowerShell에서는 `py -3.12 -m venv .venv`와 `.\.venv\Scripts\Activate.ps1`로 환경을 만듭니다. 문헌 DB·모델·공개 release는 코드와 별도 저장합니다. 처음 설정할 때는 [문헌 실행 안내](docs/LITERATURE_RUNBOOK_ko.md)의 **환경과 초기화 → 웹 빌드와 snapshot** 순서를 따릅니다.

준비된 공개 release와 문헌 snapshot을 연결해 실행합니다.

```bash
export NUTRIOMICS_PUBLIC_RELEASE="/absolute/path/to/release"
export NUTRIOMICS_ALLOW_CANDIDATE=1
export NUTRIOMICS_LITERATURE_SNAPSHOT="/absolute/path/to/public-literature-snapshot"
export NUTRIGENOMICS_EXPLORER_DIST="$PWD/frontend/explorer/dist"
python -m uvicorn nutrigenomics.public_service:app_factory \
  --factory --host 127.0.0.1 --port 8064
```

| 목적 | 로컬 주소 |
|---|---|
| 웹 플랫폼 | [http://127.0.0.1:8064/explorer/](http://127.0.0.1:8064/explorer/) |
| 문헌 화면 | [http://127.0.0.1:8064/explorer/?view=literature](http://127.0.0.1:8064/explorer/?view=literature) |
| API를 직접 실행해 보는 화면 | [http://127.0.0.1:8064/docs](http://127.0.0.1:8064/docs) |
| 서버 실행 확인 | [http://127.0.0.1:8064/healthz](http://127.0.0.1:8064/healthz) |

`NUTRIOMICS_ALLOW_CANDIDATE=1`은 내부 연구용 release 확인에 사용합니다. 공개 배포는 [release 검증 절차](docs/PUBLICATION_RUNBOOK_ko.md)를 따릅니다. 원격 서버에 접속할 때는 [SSH 포워딩 안내](docs/LOCAL_TUNNEL_ko.md)를 참고하세요.

## API 빠른 시작

공개 조회 주소는 `/api/v1/explorer/literature`입니다. 실행 중인 서버에서 아래 두 요청으로 연결 상태와 첫 문헌을 확인할 수 있습니다.

```bash
curl --fail-with-body "http://127.0.0.1:8064/api/v1/explorer/literature/stats"
curl --fail-with-body "http://127.0.0.1:8064/api/v1/explorer/literature/papers?page=1&page_size=5"
```

PowerShell에서는 다음과 같이 호출합니다.

```powershell
$api = 'http://127.0.0.1:8064/api/v1/explorer/literature'
Invoke-RestMethod "$api/stats"
$papers = Invoke-RestMethod "$api/papers?page=1&page_size=5"
$papers.items | Select-Object pmid, title, pub_date
```

| API | 용도 |
|---|---|
| `GET /stats` | 연결된 자료 버전·수집 규모·엔진 확인 |
| `GET /papers` | 문헌 검색과 페이지 조회 |
| `GET /papers/{pmid}` | 요약·원문·출처·처리 상태 |
| `GET /events` | 논문·엔진·개체별 관계 후보와 원문 위치 |
| `GET /benchmarks` | 저장된 모델 평가 결과 |
| `GET /reports/current?format=html` | 현재 snapshot의 보고서 |

**[API 사용 안내](docs/LITERATURE_API_ko.md)**에는 처음부터 따라 하는 예제, 모든 검색 조건, 응답 읽는 방법, Python 전체 조회, 보고서 저장, 내부 로그인·검수·작업 등록과 오류 해결 방법을 정리했습니다. 공개 조회는 저장된 결과를 읽으며, 요청 중에 모델 학습이나 외부 생성형 모델 호출을 실행하지 않습니다.

## 버전과 재현 기록

심사용 성과 묶음은 **NutriOmics Literature 2026.10**입니다. 이 이름은 성과표의 문서 버전이며 개별 모델의 엔진 ID는 그대로 보존합니다. 웹 기본 엔진은 `heuristic-v1`이고, 완료된 자동 관계 평가의 대표 구성은 `chatgpt-repair-v5-r1`입니다. 평가 중인 r2·v8의 학습 기록은 [버전 이력](docs/reviewer/VERSIONS.md)에서 별도로 확인할 수 있습니다.

```bash
python scripts/build_reviewer_summary.py --check
python -m pytest tests/test_literature_platform.py tests/test_literature_management.py
pnpm -C frontend/explorer typecheck
pnpm -C frontend/explorer build
```

새 학습의 결과는 전체 예측과 평가가 저장된 후 성과표에 반영합니다. [상태 기록](docs/status/literature-2026-10-24.json), [검증 기록](docs/status/validation-2026-10-24.md), [개발 실행 안내](docs/LITERATURE_RUNBOOK_ko.md)와 [최종보고서 저장소](https://github.com/kangk1204/nutriomics-final-report)를 함께 제공합니다.

## 자료·release와 라이선스

음식 조성, 중재, 오믹스 결과, 문헌과 관계는 각각의 단위로 집계합니다. release별 집계는 `dataset-metadata.json`, 파일 해시는 manifest에 저장합니다. 공개 생성·배포는 [운영 안내](docs/PUBLICATION_RUNBOOK_ko.md), 원본 재구축은 [재구축 안내](docs/WORKBENCH_PUBLIC_REBUILD_ko.md), 연구 검수는 [검수 흐름](docs/WORKBENCH_SUBMISSION_REVIEW_ko.md)을 따릅니다.

소프트웨어는 [MIT](LICENSE), 공개 가능한 자체 큐레이션·파생 분석은 CC BY 4.0입니다. 원본 데이터와 의존성의 이용조건은 [LICENSE-DATA.md](LICENSE-DATA.md)에 정리했습니다. 원문 공개 범위는 문헌별 이용조건을 적용합니다.
