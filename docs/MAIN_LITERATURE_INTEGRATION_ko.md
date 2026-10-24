# NutriOmics main · 문헌 플랫폼 통합 기록

2026년 10월 24일 최신 main의 DB 탐색·검색·분석 기능과 문헌 플랫폼을 병합했습니다. 같은 브라우저에서 음식·유전자 DB와 문헌 검색·구조화 요약·원문 근거·모델 비교·보고서를 이용합니다.

## 반영 버전과 접속

| 항목 | 값 |
|---|---|
| 기존 main | `649878ca5f5fec7a52c8860fa86177a2c2564c74` |
| 통합한 문헌 브랜치 | `2244b9aa8acb5a4cbe774d0b69bfa43995dc08c4` |
| 검증·배포한 소프트웨어 | `8d1ae42181acdf96a08182afb67b72887e961fd3` |
| 운영 DB | `nutriomics-local-korean-foods-20261023-v2` |
| 문헌 snapshot | `literature-20261024T062701777420Z` |
| 기존 DB 화면 | `http://127.0.0.1:18122/explorer/` |
| 문헌·모델 화면 | `http://127.0.0.1:18122/explorer/?view=literature` |

위 localhost 주소는 연구실 PC의 SSH 포워딩 접속입니다. 소프트웨어 버전은 `/_deployment`, 과학 DB 버전은 `/healthz`, 문헌 버전은 `/api/v1/explorer/literature/stats`에서 각각 확인합니다. 저장소의 후속 문서 커밋과 실제 실행 코드 커밋을 구분해 기록합니다.

## 기존 기능 보존

최신 main의 검색 요청 제한(브라우저 45초·서버 읽기 30초), 유전자 일괄 조회, 경로 결과 선택, 작은 창의 검색 패널·스크롤 동작을 유지했습니다. 기존 DB의 테이블과 값은 변경하지 않고, 문헌 snapshot을 별도 입력으로 연결했습니다.

운영 연구실 DB의 식품 5,844개·조성 369,408건에는 한식 추가 자료가 포함됩니다. README의 공개 후보 집계인 식품 5,794개·조성 368,208건과 범위가 다릅니다. 정량 결과 6,254,477행과 선택된 경로 결과 50,550행의 기존 조회를 유지합니다.

문헌 snapshot은 고유 문헌 48,147편, 초록 46,649편, 본문 확보 2,482편, 기본 엔진 처리 30,329편을 담습니다. 확대 코퍼스의 검색 목록 133,261편 중 이 snapshot에 저장된 문헌은 48,146편입니다. 전체 수집 문헌과 확대 검색 코퍼스의 포함 범위가 달라 두 값을 합산하지 않습니다. 문헌 수집 서버의 후속 진행은 별도 snapshot으로 반영합니다.

완료된 자동 관계 평가의 대표 결과는 같은 개발 100편·정답 관계 154건에서 TP 65·FP 63·FN 89, 정밀도 50.78%·재현율 42.21%·F1 46.10%입니다. 이번 병합은 새로운 모델 학습이나 성능 재평가가 아니며, 저장된 완료 평가와 원문 근거를 웹에서 제공하는 변경입니다.

## 검증 결과

| 검증 | 결과 |
|---|---|
| Windows 전체 Python 검사 | 1,599건 통과·11건 건너뜀·실패 0건 |
| Linux 전체 Python 검사 | 1,593건 통과·17건 건너뜀·실패 0건 |
| 프런트엔드 | 프로덕션 빌드·독립 HTML 런타임 빌드 성공 |
| 브라우저 동작 회귀 스크립트 | 19개 검사 묶음 통과 |
| 운영 DB API 전후 비교 | 295개 요청·JSON 불일치 0건 |
| 경로 정량 값 전후 비교 | 50,550행 일치 |
| 추가 문헌 API 검사 | 40개 요청 통과 |
| 문헌 서버 배포 전·후 검사 | 후보 13개·전환 후 8개 검사 통과 |
| 실제 운영 URL·웹 파일 검사 | 11개 검사 통과·웹 파일 해시 일치 |
| 저장소 파일 무결성 | 배포 소스 1,228개 파일의 SHA-256 일치 |
| 웹 수동 검증 | 음식 검색→150g 계산, 문헌 검색→상세→인용 위치→모델 비교→HTML 보고서 확인 |

기존 API 비교는 메타데이터, 음식·유전자 검색, 상세, 연결망, 식사·성분 계산, 연구·경로 결과를 포함합니다. 추가 문헌 검사는 검색·연도·본문 필터, 인용 위치, 완료 평가표, HTML·Markdown·CSV·JSON 보고서, 공개 앱의 관리 API 비등록, 4개 동시 연결의 조회를 포함합니다. 테스트용 검수 기록이나 과학적 승인 기록을 만들지 않았습니다.

Windows에서 건너뛴 검사는 선택적 NLP 의존성, 원자료 부재, 운영체제 파일 잠금·심볼릭 링크 동작에 해당합니다. Linux에서는 파일 잠금·심볼릭 링크 검사가 통과했으며, 선택적 NLP·R·Node 의존성 및 원자료가 필요한 17건을 건너뛰었습니다. 프런트엔드 빌드와 19개 회귀 검사 묶음은 Node가 설치된 Windows에서 별도로 통과했습니다. 전체 소프트웨어 검사 통과와 개별 모델의 과학적 정확도는 별도 항목으로 관리합니다.

집계와 해시는 [통합 검증 JSON](status/main-literature-integration-2026-10-24.json)에 기록했습니다. Windows와 Linux는 동일 검사 집합을 실행한 결과이므로 통과 건수를 합산하지 않습니다.

## 설치·재현

```bash
uv sync --frozen --extra dev --extra literature
uv run pytest -q
pnpm -C frontend/explorer install --frozen-lockfile
pnpm -C frontend/explorer build
node frontend/explorer/scripts/run-portable-tests.mjs
uv run python scripts/verify_repository_files.py
```

실제 서버의 전후 비교에는 [통합 검증 스크립트](../scripts/verify_integrated_platform.py)를 사용합니다. 경로 검사 입력의 `runs` 배열에는 `series_accession`, `parent_analysis_version`, `contrasts`, `rows`를 기록합니다. 각 대비를 200행씩 끝까지 조회해 값과 JSON 해시를 대조합니다.

```bash
python scripts/verify_integrated_platform.py \
  --url http://127.0.0.1:8078 --pathway-spec pathways.json \
  --output baseline.json
python scripts/verify_integrated_platform.py \
  --url http://127.0.0.1:8079 --pathway-spec pathways.json \
  --baseline baseline.json --literature --output integrated.json
```

일반 설치와 API 예제는 [실행 안내](LITERATURE_RUNBOOK_ko.md), [API 사용법](LITERATURE_API_ko.md)에 있습니다. 운영 환경의 DB·snapshot·계정·인증 파일은 Git에 포함하지 않습니다.

## 배포와 복귀

검증한 코드·웹 빌드·문헌 snapshot을 새 버전 디렉터리에 배치하고, 기존 서비스와 다른 loopback 포트에서 검사한 뒤 SSH 포워딩을 전환했습니다. 이전 DB 서비스는 복귀용으로 유지합니다. 문헌 서버는 공개 앱과 내부 관리 앱만 새 전용 가상환경으로 전환했습니다. 전환 후 공개 조회·내부 인증·프로세스의 코드 경로를 확인했으며, 기존 수집·작업 worker의 PID·코드 경로·실행환경·계정 저장소를 유지했습니다.

복귀 시 새 포워딩 세션의 `STOP` 파일로 해당 세션만 종료하고, 기록된 이전 포트로 포워딩을 재시작합니다. 문헌 서버는 백업한 supervisor 설정의 공개·내부 앱 섹션만 복원합니다. 복귀 후 `/healthz`·기존 검색·문헌 stats를 확인합니다. 원본 DB나 과거 예측을 덮어쓰는 단계는 없습니다.
