# 공개 원본에서 재구축·갱신하기

이 절차는 이전 서버의 SQLite나 분석 결과 없이 새 Linux x86_64 프로젝트에서 구축한다. 공개 접근과 재배포 허용은 별개다. K-FIND와 CTD는 확인되지 않은 재배포 권한을 허용으로 바꾸지 않는다. 자동 계산 검증은 전문가의 연구설계 검수와 별도로 기록된다.

## 환경과 원본

프로젝트 루트를 `$project`, 이 코드의 절대 경로를 `$source`, 새 작업 경로를 `$work`로 지정한다. 작업 경로는 프로젝트 루트 아래에 둔다. 기존 작업 폴더를 덮어쓰지 않는다. Python은 `uv sync --frozen`으로 설치하고, R은 `bash scripts/setup_rebuild_runtime.sh "$project"`로 고정 환경을 설치할 수 있다. 이 스크립트는 기존 R 환경 교체를 거부한다. 동봉한 Linux 패키지 lock에는 2026-10-17에 실행한 CDF 판독 패키지가 포함된다.

프로젝트 환경변수 `NUTRIGENOMICS_HOME`, `NUTRIGENOMICS_SOURCE_ROOT`, `NUTRIGENOMICS_DATA_DIR`, `NUTRIGENOMICS_DATABASE_URL`을 각각 프로젝트·코드·`$work/data`·새 SQLite에 지정한다. R의 프로젝트 라이브러리는 `$project/toolchain/r-library`다. 전역 Python/R/Conda 환경을 변경할 필요가 없다.

공식 원본을 아래 CLI로 받는다. 각 다운로드의 URL·시각·크기·해시·실패 이력을 보존한다. 상류 자료가 변경되면 과거 바이트와 동일한 재현이라고 주장하지 않고 새 입력 release를 사용한다.

```bash
python -m scripts.download_fdc_json --data-dir "$work/data"
python -m scripts.download_geo_processed --data-dir "$work/data" --database-url "$NUTRIGENOMICS_DATABASE_URL"
python -m scripts.download_bridge_studies --root "$work/raw" --studies GSE13466 GSE98645 GSE27385 --raw --download-workers 2
python -m scripts.download_kfind_standard --output-dir "$work/data/raw/kfind/15100065" --dataset-id 15100065
python -m scripts.download_kfind_standard --output-dir "$work/data/raw/kfind/15100070" --dataset-id 15100070
python -m scripts.reacquire_ctd --output "$work/data/raw/ctd"
python -m scripts.prepare_reacquired_inputs --work "$work" --source "$source"
python -m scripts.import_kfind_foods --dataset-dir "$work/data/raw/kfind/15100065" --dataset-dir "$work/data/raw/kfind/15100070" --database-url "$NUTRIGENOMICS_DATABASE_URL"
```

HGNC의 공식 `hgnc_complete_set.txt`도 `$work/raw/hgnc/`에 받고, URL·다운로드 시각·SHA-256·이용조건을 별도 manifest로 남긴다. 유전자명이나 숫자 접미사만으로 HGNC/Entrez 동일성을 추정하지 않는다. 위 GEO 다운로드 전에 스키마 초기화와 연구 사전 적재가 필요하다면 `scripts.import_human_bridge`를 먼저 실행한다. K-FIND의 선택 목록은 `config/kfind_food_selection.json`으로 고정되어 있다.

## 분석과 release

`$work/toolchain`은 해당 프로젝트의 `toolchain` 디렉터리를 참조해야 한다. 공개 원본 확보와 추출, count 수정본 감사, 시료 대응표 확인이 끝나면 다음을 실행한다. 대형 R 분석은 직렬로 실행되며 실행 코드의 해시가 동결된다. 실행 중 R 소스를 바꾸지 않는다.

```bash
python -m scripts.rebuild_public_omics plan --work "$work" --source "$source" --project "$project" --analysis-suffix=-rebuild20261017-v2
python -m scripts.rebuild_public_omics execute --work "$work" --source "$source" --project "$project" --analysis-suffix=-rebuild20261017-v2
python -m scripts.build_reacquired_release --work "$work" --source "$source"
```

다음 갱신은 다른 `$work`와 새 날짜·버전 접미사를 사용한다. 과거 버전과 동명의 결과를 대체하지 않는다. 입력→모델→결과→허용된 공개 패키지를 별도로 보존하고, `DONE.json`과 실제 실행 영수증을 통과한 14개 결과만 적재한다. 공개 release는 식품 조성·연구/노출/대비·정량 결과·식별자 자료 중 개별 정책이 허용하는 것만 포함한다. 제외 자료는 부재와 구분한다.

배포 코드의 R 기본 경로를 이식 가능한 값으로 바꾼 파일은 `portable-path-changes.json`에 별도로 기록한다. 치환 전 원본 바이트는 `source_snapshots/default_path_originals/`에 보존한다. 분석의 실행 SHA-256은 이 원본 바이트와 연결되며, 경로를 바꾼 파일을 동일한 바이트라고 주장하지 않는다. 나머지 R 소스는 코드 파일 manifest의 해시로 추적한다.

## 검증과 반영

`verify_reacquired_accuracy numeric`은 14개 실행 결과의 키·효과·통계·결측을 core DB와 전수 대조한다. `food`는 새 원본의 50종·1,200건과 API의 가중 합계를 대조한다. `validate_publication_values`는 core→공개 SQLite/Parquet 보존을 검증한다. 각 도구의 `--help`로 경로를 지정한다.

`freeze_rebuilt_performance`로 실제 release의 조회 조건과 해시를 고정한 뒤 `benchmark_explorer_performance benchmark --runs 100 --concurrent`를 실행한다. `--allow-missing-omics`는 미리보기 전용이며 최종 판정에 사용하지 않는다. API 반복 측정과 별도로 실제 브라우저·최대 HTML/CSV/JSON·서버 접속 없는 HTML 탐색·이전 release 복원을 확인한다. 모든 검증 보고서는 대상 release와 해시를 명시한다.

대형 SQLite의 전체 무결성 검사는 `NUTRIGENOMICS_INTEGRITY_CACHE_MIB`로 읽기 캐시를 64~4096 MiB 범위에서 지정할 수 있다. 기본값은 128 MiB다. 이 설정은 검사 범위나 웹 응답 캐시를 바꾸지 않으며, 닫힌 파일을 새 연결로 여는 전체 `integrity_check`와 `foreign_key_check`는 그대로 수행한다.

실패하면 이전 서비스 release를 유지한다. 새 원본을 자동 전문가 승인 상태로 만들거나 분석 실패를 결과 0건으로 바꾸지 않는다. 서버 내부 서비스는 loopback에 바인딩하고 터널로 확인한다. 이 재구축 코드의 존재 자체는 DOI 등록·기관 검토·논문 제출 준비 완료를 뜻하지 않는다.
