# NutriOmics Data Descriptor 공개·검증·운영

현재 생성물은 **투고 후보**다. 자체 코드 MIT와 자체 큐레이션/자체 분석 산출물 CC BY 4.0을 적용한다. 원자료는 source/output별 조건을 유지하며 후보 자료의 공개 가능성과 과학적 검수 완료를 구분한다.

## 구성과 식별자

`nutriomics.sqlite3`의 `science_record`에는 연구·노출·군·대비·주장·분석 실행을 저장한다. `effect`에는 발현·DNAm 등의 공개 허용 정량 결과를 저장한다. `series_accession + feature_type + feature_id + contrast + analysis_version`가 과학적 결과 키이며, `effect:` SHA256 식별자는 그 키로 결정된다. 원본 DB 행 번호와 연구실 절대 경로를 결과 식별자로 사용하지 않는다.

SQLite, 표별 CSV.gz와 분석별 Parquet/CSV.gz, source catalog, 제외 이유, 권리 정책, schema와 파일 해시를 함께 배포한다. Parquet와 SQLite가 null·실수·문자형을 구분하는 정본이다. CSV의 빈 셀은 누락/빈 문자열을 구별할 수 없으므로 정확한 형 복원에는 정본을 사용한다. 대형 정량 결과를 그래프 연결마다 복제하지 않는다.

## 구축과 확인

```powershell
uv sync --frozen --extra dev
python -m scripts.build_publication_release --release <immutable-explorer-release> --core <consistent-core-snapshot> --policy config/publication_rights_20261014.json --output <new-public-candidate> --release-id <new-version>
python -m scripts.prepare_public_service_release --candidate <new-public-candidate> --parent <immutable-explorer-release> --output <new-public-service-candidate> --release-id <new-service-version>
python -m pytest
```

논문 그림 재생성에는 `uv sync --frozen --extra figures`로 별도 plotting extra를 설치하고 `scripts.plot_data_descriptor`를 사용한다. 실제 release metadata·검수 manifest·새 성능 결과가 입력이다. PNG/PDF/SVG와 패널 원자료가 함께 생성된다.

원본 core는 SQLite backup API로 WAL을 포함한 일관된 **비공개** snapshot을 만든다. 원본에 checkpoint를 강제하거나 공개 패키지에 전체 core를 넣지 않는다. 구축 실패 시 `.building`은 실패 증거와 함께 남고 운영 pointer를 교체하지 않는다. 정상 완성한 새 디렉터리만 release로 사용한다.

`review/scientific-review.json`의 기록별 SHA256에 검수 결정을 연결한다. 검수자·일자·원 논문 위치·이유를 반드시 기재한다. 자동 추출과 `source_checked`는 교수님 승인 또는 독립 전문가 검수로 보고하지 않는다. 수정·제외가 있으면 새 release를 구축하고 영향을 받는 대비·주장·평가 정답도 다시 감사한다.

## 공개 서비스

공개 앱의 진입점은 `nutrigenomics.public_service:app_factory`다. `NUTRIOMICS_PUBLIC_RELEASE`를 명시하지 않으면 내부 DB로 대체하지 않고 종료한다. 기본값은 검수·검증 완료 release만 받는다. 후보 검토에 한해 `NUTRIOMICS_ALLOW_CANDIDATE=1`을 사용하고 **127.0.0.1**에서 실행한다.

정식 배포에는 `release-gate.json`이 필요하다. release ID·DB 해시·실제 검수/검증 파일의 해시가 일치해야 하며, metadata의 `status` 문자열만 변경해 배포할 수 없다. `scripts.submission_readiness`는 검증 결과 JSON을 직접 읽고 해시·판정·release ID를 확인한다. 아직 실행하지 않은 검사에 `passed: true`만 기재하거나 다른 release의 보고서를 넣어 제출 조건을 충족시키지 않는다.

```powershell
$env:NUTRIOMICS_PUBLIC_RELEASE='<public-service-candidate>'
$env:NUTRIOMICS_ALLOW_CANDIDATE='1'
python -m uvicorn nutrigenomics.public_service:app_factory --factory --host 127.0.0.1 --port 8022
```

공개 앱은 기존 React Explorer, 읽기 전용 음식/섭취량 계산, 연구·노출·결과 조회를 제공한다. 쓰기·수집·오믹스 재분석 endpoint를 설치하지 않는다. 공개된 source food ID가 정본이며 기존 숫자 food ID는 호환용 별칭이다. 섭취량은 조리 후 가식부 g; 100mL를 근거 없이 g로 환산하지 않는다. 공개 범위 밖의 자료는 결측/근거 부재와 구분한다.

`deploy/public`은 업체에 종속되지 않는 container/HTTPS 설정 예시다. 업체·도메인·비용은 교수님이 선택한다. 현재 Docker build, 외부 HTTPS 운영 및 DOI 등록을 완료했다는 의미가 아니다. container base digest·SBOM은 실제 호스팅 환경의 인수 검사에서 고정한다.

## 갱신·백업·복원

매월 `python -m scripts.check_publication_updates --output <new-check.json> --previous <previous-check.json>`으로 원본의 HTTP validator 변경을 점검한다. 이는 내용 해시 확인이나 전문가 검수가 아니다. `scripts.update_nutriomics plan`으로 수집 범위를 확인하고, 필요한 snapshot을 `fetch`한 후 `scripts.refresh_workbench`로 새 파생 release를 만든다. 다운로드 → 내용 해시·schema·수치 검사 → 변화 보고 → 기록별 교수님 검수 → 새 후보 → 평가 → 새 release 순서다. 원본 변경은 검수 SHA를 무효화하며 승인 상태를 자동 승계하지 않는다. 개발 70질의/봉인 30질의를 유지하고 봉인 결과로 규칙을 재조정하지 않는다.

release와 source catalog/권리 정책/검수 파일/평가 원시 결과를 함께 백업한다. 업로드에 앞서 manifest를 확인하고 운영 이전 디렉터리를 보존한다. 잘못된 배포는 이전 release의 파일과 환경으로 복원하고 `/healthz`, 검색·계산·결과·export를 점검한다. 공개 DOI의 각 버전은 보존하고 새 자료는 새 버전으로 등록한다.

2년 유지 책임자: 강근수 교수. 실제 호스팅 인수 시 월별 백업 확인, 장애 연락처, 복원 시험 날짜와 서비스 중단 안내 위치를 기입한다. 비용·도메인·연락처를 임의로 확정하지 않는다.

## 제출 조건

공개 가능한 발현 및 DNAm 결과, 연구/대비 맥락, 원문 검수, 음식 50종/조성 1,200건 대조, 새 성능·브라우저·offline·export 검사, 깨끗한 환경의 재현·복원, 봉인 질의 평가, 버전 DOI, 공식 저자·소속·연구비·기관 윤리 판단이 같은 release를 참조해야 한다. 미완료 항목은 `submission-readiness.json`에 남긴다.
