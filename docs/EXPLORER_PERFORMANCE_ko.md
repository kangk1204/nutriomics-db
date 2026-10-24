# NutriOmics 조회 색인·성능 검증

## 2026-10-19 공개 오믹스 페이지 확장

아래 기존 실행 예시와 27개 사례는 이전 버전의 기록이다. 최종 평가에는 내부·공개 각각 동결한 62개 사례를 사용하며, 이전 실패와 새 실행 결과를 별도 attempt로 보존한다. 새로운 공개 페이지 보조색인은 원본 release를 수정하지 않고, 식별자·정렬 위치·내부 원본 rowid만 저장한다. 효과량·통계·권한은 원본 `study_effect` view에서 읽는다. source rowid는 API나 export에 노출하지 않는다.

```bash
python -m scripts.build_public_omics_index \
  --source /data/RELEASE/nutriomics.sqlite3 \
  --output /data/derived/public-omics-NEW.sqlite3 \
  --source-sha256 RELEASE_MANIFEST_DATABASE_SHA256
export NUTRIGENOMICS_PUBLIC_OMICS_INDEX=/data/derived/public-omics-NEW.sqlite3
# Unreviewed candidates are for loopback review only; this does not approve publication.
export NUTRIOMICS_ALLOW_CANDIDATE=1
python -m scripts.serve_verified_release \
  --release /data/RELEASE --application public \
  --receipt /data/receipts/public-startup.json --port 8000
```

기존 완성 파일을 덮어쓰지 않는다. 중단된 `.building` 파일은 같은 source·release·builder 코드로 `--resume`을 명시할 때만 이어간다. 단일 스레드·파일 기반 정렬·제한된 batch와 SQLite cache를 사용하고, 메모리 2GiB 예산·디스크 여유·원본 전후 SHA·원본/WAL fingerprint·정확한 selector별 개수·전체 순서를 검사한다. 출처의 TEXT ID와 동일 feature의 여러 레코드를 보존하며, `page`와 `cursor` 의미는 바뀌지 않는다.

새 색인이 설정되면 startup에서 SHA와 원본 결속을 검증한 뒤 준비 상태를 알린다. 누락·변경·release 불일치가 있으면 느린 원본 쿼리로 조용히 돌아가지 않고 명시적 unavailable 오류를 반환한다. HTTP 캐시는 보조색인과 receipt의 변경도 키에 포함한다. 다른 경로로 옮긴 원본에는 새 색인을 구축한다. 내부 서비스에는 이 공개 전용 환경변수를 설정하지 않는다.

사람 중재 매칭은 요청별 preview 읽기를 모아 처리하고, 검증된 startup에서 등록 대비의 기존 사전식 첫 6개 결과만 유한한 예산으로 메모리에 준비한다. 원본 효과 행렬을 복제하지 않으며 source·release·유형·분석 키·권한을 검증한다. 실패·과거 source 값은 준비하지 않는다. 준비 시간과 OS cache warming은 startup receipt에 따로 기록하고 HTTP 성능 개선 수치로 합치지 않는다. 유전자 지정 요청은 동적 조회를 유지한다. 자세한 규칙은 [공개 페이지 색인 안내](WORKBENCH_PUBLIC_OMICS_INDEX.md)에 있다.

내부 서비스는 그래프 release와 별도로 정량 결과를 읽는 core DB도 manifest의 `input_hashes.core_db`와 전체 SHA256을 대조한다. 원본/WAL fingerprint의 전후 일치와 비어 있지 않은 WAL을 검사하며, 다른 source나 변경 중인 파일은 준비 완료로 표시하지 않는다. 순차 해시 읽기는 OS 파일 cache를 준비할 수 있으므로 그 시간·자원을 startup receipt에 별도로 남긴다. 이는 512MiB 응답 cache와 다른 계층이며, OS cache가 비어 있거나 다른 작업으로 교체된 상태의 무작위 읽기 성능을 입증하지 않는다.

완성된 공개 서비스 SQLite를 재사용할 때는 위 `serve_verified_release` 절차를 사용한다. `prepare_public_service_release`는 아직 서비스 테이블이 없는 원시 공개 candidate를 변환하는 도구이며, 서비스 테이블이 이미 들어 있는 공개-v2 패키지에 다시 적용하는 절차가 아니다. 공개 파일만을 사용한 실행·파생 색인 재구축과 원시 오믹스 전체 재분석은 별도의 재현 범위다.

이 변경은 원본 식품 조성과 오믹스 수치를 수정하지 않습니다. 새 탐색 release에 인접 관계, 검색 후보, 음식–노출 대응, 대비별 정확한 결과 수와 정렬된 원본 행 ID를 저장합니다. 효과량·P/FDR·단위·출처는 기존 core DB에서 읽습니다.

## 동작과 갱신

- FTS5 trigram은 검색 후보를 좁히고 기존 LIKE 조건으로 재검사합니다. 3글자 미만은 기존 조건을 사용합니다.
- 그래프의 탐색 순서·필터·80/200 기본 한도·250/750 최대 한도를 유지합니다. 관계와 노드를 묶어서 조회합니다.
- 오믹스 `page` 입력을 유지하며 `cursor`와 `next_cursor`를 추가했습니다. cursor는 release·원본 상태·전체 조회 조건에 묶입니다.
- HTTP 응답과 효과 preview는 합계 512MiB LRU를 사용합니다. 동일 요청은 한 번 계산하고 DB 작업은 최대 4개까지 실행합니다. 실패 응답은 저장하지 않습니다.
- main DB와 WAL의 크기·inode·mtime·ctime 및 release 경로·내보내기 정책 파일이 캐시 키에 포함됩니다. 원본이나 release가 바뀌면 이전 캐시를 사용하지 않습니다. 색인이 현재 core와 맞지 않으면 원본 조회로 돌아갑니다.
- 일반 조회는 10초, export는 45초의 서버 예산을 사용합니다. progress handler와 interrupt로 SQL을 취소하고 연결을 닫습니다. 커널 디스크 대기는 interrupt보다 늦게 종료될 수 있으며 측정에서 별도로 기록합니다.
- 매칭의 `effects_status`는 `available / source_unavailable / query_timeout / analysis_key_mismatch`를 구분합니다. 조회 실패의 개수는 null입니다. HTTP 503도 근거 0건으로 처리하지 않습니다.

새 식품·근거·오믹스 release를 만든 뒤 성능 색인도 다시 구축합니다. `indexed_for_release`가 다르면 이전 release의 색인을 사용하지 않습니다. 원본이 없는 경우, 동일한 분석 키가 없는 경우, 반환된 원본 키가 맞지 않는 경우도 정상 0건과 구분합니다.

## 재현 실행

승인된 서버 `<project-root>`의 프로젝트 Python을 사용합니다. 아래 명령은 candidate source 폴더에서 실행합니다. 실제 원본과 release 경로를 명시하고 기존 output 디렉터리를 덮어쓰지 않습니다.

```bash
./.venv/bin/python scripts/build_explorer_performance.py \
  --from-release <project-root>/data/explorer/releases/explorer-20261009-v7 \
  --core-db <project-root>/data/db/nutrigenomics.sqlite3 \
  --output-dir <project-root>/data/explorer/releases/NEW_RELEASE \
  --release-id NEW_RELEASE --ram-workspace --memory-budget-gib 32
```

RAM 작업 공간은 이 작업이 만든 `/dev/shm/nutriomics-perf-*`만 사용하고 완성된 파일은 영구 디렉터리로 복사·해시 검증합니다. 32GiB 예산과 4GiB 여유 메모리를 확인합니다. 작업이 실패하면 `FAILED`를 남기고 운영 포인터를 바꾸지 않습니다. 호스트가 재시작돼 작업이 중단되면 실패 파일을 보존하고 새 output에서 다시 실행합니다.

고정 평가 사례는 `research/performance/20261010/cases.json`입니다. 2026-10-10에 운영 v7에서 27개 사례를 고정했으며 SHA256은 `ba34ca122dbc79f535740a8f89e33afcf29770fad2c24b3c5032cf1aba97ad43`입니다. 음식 50종 목록도 포함합니다. 원본 식품 50종/조성 1,200건 대조, 실제 그래프·매칭·오믹스 응답의 전후 비교는 별도 수행해야 합니다.

```bash
./.venv/bin/python scripts/benchmark_explorer_performance.py benchmark \
  --spec <project-root>/research/performance/20261010/cases.json \
  --base http://127.0.0.1:8020 --output /path/to/staging-report \
  --runs 100 --concurrent --server-pid STAGING_PID \
  --source-manifest /path/to/source-manifest.json
```

`performance.json`과 `samples.csv`에 첫 요청·warm 100회·4개 동시 혼합 요청의 원시 측정값을 저장합니다. `resources.json`에는 /proc 자원 상태를 남깁니다. HTTP 200이라도 오믹스 원본이 조회되지 않거나 반복 요청 중 응답이 바뀌면 실패입니다. 서버 내부 측정과 SSH 터널 측정을 분리합니다. baseline은 같은 사례를 `--baseline`으로 실행하며 실패·시간 초과도 보존합니다. baseline 실패를 숫자 0이나 성능 개선 완료로 해석하지 않습니다.

`verify_explorer_performance_parity.py`는 원본 탐색 테이블의 논리적 hash, 50종 음식 계산, 동결한 HTTP 응답을 비교합니다. 응답에서 허용하는 차이는 release ID와 새 프로토콜 필드뿐이며 효과 수치·분석 키·출처·권한·관계 선택은 비교 대상입니다.

## 배포와 복원

`performance_release_gate.py`는 같은 source hash·release·평가 사례에 대한 새 보고서 6개를 요구합니다: performance, data-parity, exports, browser, compatibility, tests. 원시 p95와 표본 수를 다시 확인하고 누락·오래된 결과·변경된 코드·원본 부재를 통과시키지 않습니다. Linux 테스트, 실제 브라우저의 기본 그래프 렌더 시간, 3개 export 형식과 오프라인 HTML 동작도 필요합니다. 테스트용 fixture 결과는 운영 성능 증거가 아닙니다.

브라우저 시간은 그래프 응답 객체마다 기록합니다. `data_to_frame_ms`는 응답 수신부터 표시까지, `render_to_frame_ms`는 그래프 생성부터 표시까지입니다. 탭을 열기 전에 미리 수신한 자료는 `preloaded_before_render_ms`로 구분합니다. 수신 시각이 없는 측정을 렌더 시작 시각으로 대체하지 않으며, 누락·음수·NaN 측정은 배포 기준을 통과하지 못합니다. 기본 그래프 검증은 그래프 화면이 열린 상태에서 새 응답을 받아 수행합니다.

`verify_explorer_release_flows.py exports`는 동결한 요구르트 최대 그래프의 250개 노드·750개 관계를 그대로 내보냅니다. HTML/JSON/CSV의 ID, release, 저장 조건·좌표, 재배포 제한 표시를 대조하고 실제 HTTP 시간을 남깁니다. HTML에 내장 runtime과 `connect-src 'none'`이 있는지도 확인합니다. 이 검사만으로 오프라인 브라우저 통과를 기록하지 않으며 실제 브라우저 탐색은 별도 증거가 필요합니다. `compatibility` 모드는 기존 음식·연구·중재 조회 응답을 운영 서버와 staging에서 비교합니다.

아래 블록의 `deploy_explorer_performance`는 공개 패키지에 없는 **이전 사설 배포 명령**이다. 현재 공개 실행 예시로 복사하지 않는다.

```bash
./.venv/bin/python scripts/performance_release_gate.py \
  --directory /path/to/staging-report --release-id NEW_RELEASE \
  --source-manifest /path/to/source-manifest.json --cases /path/to/cases.json
./.venv/bin/python -m scripts.deploy_explorer_performance \
  --pid LIVE_API_PID --release /path/to/NEW_RELEASE \
  --manifest /path/to/source-manifest.json --reports /path/to/staging-report \
  --cases /path/to/cases.json
```

위 `deploy_explorer_performance`와 아래 rollback 명령은 **사설 운영자 도구의 과거 실행 이력**이며 공개 코드 패키지에 포함하지 않는다. 공개 패키지의 실행에는 `scripts.serve_verified_release`를 사용한다. 내부 그래프 release의 격리 복원 시험에는 `scripts.verify_service_rollback`을, 내부·공개 wrapper의 전체 startup 검증을 포함한 복원에는 `scripts.verify_final_upgrade_staging_restore`를 사용한다. 구체적인 인수 환경은 기존 서비스의 PID·cwd·명령과 새 release 검증을 확인한 뒤 별도로 구성한다. 과거 배포기는 검증된 파일 교체와 실패 시 이전 코드·환경·release 복원을 수행했다. 환경 백업은 프로젝트 내 private 파일(0600)이며 출력하지 않는다.

```bash
./.venv/bin/python -m scripts.deploy_explorer_performance \
  --pid ACTIVE_API_PID --rollback <project-root>/backups/performance-TIMESTAMP
```

서비스는 127.0.0.1과 기존 SSH 터널을 유지합니다. 배포 직후 상태는 `active_pending_production_revalidation`입니다. 운영 서버의 동일 100회 평가, 데이터·브라우저·export 흐름, 실제 복원 확인까지 통과해야 완료로 보고합니다.
# 조회 한도와 지연 진단

일반 API의 서버 처리 한도는 기본 10초입니다. 디스크 대기가 큰 연구실 환경에서는 `NUTRIGENOMICS_READ_TIMEOUT_SECONDS=30`으로 조정할 수 있으며, 허용 범위는 정수 10–30초입니다. 화면 요청은 헤더와 응답 본문을 합쳐 45초까지 기다립니다. 내보내기는 서버 45초·화면 60초를 유지합니다. 동시 DB 작업 상한 4개와 실패 결과를 캐시하지 않는 규칙도 유지합니다.

이 설정은 일시적인 대기를 허용합니다. 조회 속도를 개선했다는 증거가 아니며 기존 warm p95 1초·최대 그래프 3초 기준은 그대로 평가합니다. 대용량 파일 복사·분석과 조회를 동시에 시험했다면 그 조건을 별도로 기록합니다. 운영 요청 기록에는 실제 HTTP 상태와 적용된 처리 한도를 저장하며, 조회 실패를 확인된 0건이나 식품 함량 기준의 부재로 표현하지 않습니다.
