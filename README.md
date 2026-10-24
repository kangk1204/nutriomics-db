# NutriOmics

음식·영양성분·식이중재·유전자·후성유전 정보를 연결하는 연구 DB와 문헌 분석 플랫폼입니다. 문헌 검색부터 원문 근거, 개체 ID, 관계 예측, 구조화 요약과 연구자 검수까지 웹브라우저에서 탐색합니다.

## 최종 대표 성과

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

Python 3.12, Node.js 24, pnpm 11.25.0을 사용합니다. 현재 문헌 통합 소스는 다음 브랜치에 있습니다.

```bash
git clone --branch feature/20261023-literature-integration https://github.com/kangk1204/nutriomics-db.git
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
