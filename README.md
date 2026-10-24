# NutriOmics

음식의 성분과 사람 식이중재 연구의 유전자 발현·DNA 메틸화 결과를 함께 살펴보는 연구용 DB입니다.

[English](README.en.md) · [DB 구조와 쉬운 설명](docs/DB_STRUCTURE.ko.md) · [검토 안내](docs/REVIEWER_GUIDE.ko.md) · [설치 방법](docs/GETTING_STARTED.ko.md)

음식명과 섭취량으로 성분량을 계산하고, 관련 연구에서 어떤 음식·보충제를 어느 용량으로
얼마 동안 섭취했는지 확인할 수 있습니다. 관찰 결과에는 조직, 측정 시점, 비교 조건과 출처를 함께 남깁니다.

![NutriOmics DB의 구성과 자료 연결](docs/figures/db-structure/보고서용_DB_구조도.png)

음식의 성분표에서 성분량을 계산한 뒤, 관련 사람 연구의 조건과 발현·메틸화·경로 결과를 찾아갑니다.
건강지표는 같은 연구의 별도 결과로 연결합니다. 기전·계산 예측은 별도 층으로 구분합니다.
그림은 자료를 찾는 구조이며, 모든 음식의 건강 효과가 확인됐다는 뜻은 아닙니다.

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
문헌·모델 통합은 [별도 개발 브랜치](https://github.com/kangk1204/nutriomics-db/tree/feature/20261023-literature-integration)에서 진행 중이며 이번 기본판에 합치지 않았습니다.

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
