# 수정된 과학 기록을 실제 공개 SQLite에 결합하기

`build_composed_public_release.py`는 불변 native 공개 release와 조립된 수정 패키지의 과학 기록을 **새 디렉터리의 native 공개 SQLite**에 결합한다. 원본 release, 운영 포트, 기존 RAM replica 또는 기존 보조색인을 수정하지 않는다. raw 자료 재구축이나 R 재분석을 수행하는 명령이 아니다.

## 입력과 실행

```sh
PYTHONPATH=/path/to/frozen-code python /path/to/frozen-code/scripts/build_composed_public_release.py \
  --base /path/to/native-public-v2 \
  --candidate /path/to/materialized-submission-candidate-v15 \
  --output /path/to/NEW-composed-release \
  --release-id NEW-RELEASE-ID \
  --expected-base-manifest-sha256 BASE_MANIFEST_SHA256 \
  --expected-candidate-filemap-sha256 CANDIDATE_FILEMAP_SHA256 \
  --classifications /path/to/source-classifications.json \
  --expected-classifications-sha256 SOURCE_CLASSIFICATIONS_SHA256 \
  --review-dependencies /path/to/review-dependencies.json \
  --expected-review-dependencies-sha256 REVIEW_DEPENDENCIES_SHA256
```

`--candidate`는 `filemap.json`, `supplement/release-science.sqlite3` 등이 실제로 있는 조립된 패키지 루트다. 준비용 reference filemap만 있는 디렉터리와 혼동하지 않는다. native base의 SHA는 candidate filemap과 base manifest에서 같아야 한다. 출처별 quantitative rights 정책이 허용한 selector만 수록 가능하다. 기존 source deny를 candidate 상태 문자열로 우회할 수 없다.

명시적 사람 판정이 있으면 `--decisions FILE --expected-decisions-sha256 SHA`를 추가한다. 입력은 `reporting_review.validate_review_decisions`의 **record-level 배열**이며 각 항목에 `record_kind`, `record_id`, `record_sha256`, `source_dependency_sha256`, 판정 및 사람의 검수 선언·원문 위치·이유·timezone 날짜가 필요하다. task bundle, AI 제안 또는 집계표를 승인으로 변환하지 않는다. 미지정 시 사람 승인 수는 0이다. 이전 payload SHA에 대한 판정은 거부한다.

`pending` 이외의 모든 사람 판정에는 `--review-dependencies` 및 해당 파일의 기대 SHA가 **필수**다. dependency envelope는 `nutriomics-review-dependencies-v1`이며 최종 검수 대상 전체의 현재 payload와 원문·분석 proof 의존관계를 결속한다. payload가 같아도 원문 SHA·URL·위치 또는 관련 canonical 기록이 바뀌면 이전 판정을 거부한다. 동일 원본 bytes가 보존 cache로 이동한 경우의 경로 진단 정보는 원문 내용의 동일성과 구분한다. `scientific_review_dependency` 테이블, API의 `source_dependency_binding`, 생성된 검수 HTML과 저장 JSON이 같은 stamp를 사용한다. 사람 판정 0인 과거 v1 candidate는 새 dependency 필드가 없어도 승인을 추정하지 않고 읽을 수 있다.

분류는 `nutriomics-source-classifications-v1` envelope를 사용한다. 현재 지원하는 source-only 분류는 `exploratory_confounded_extraction_time`, `supplement_mapping_unresolved`다. 각 기록의 현재 SHA, 출처 URL·위치·파일 SHA와 `primary_dietary_evidence_eligible=false`를 요구한다. 보충제 대응이 미확인이라면 `supplement_attribution_established=false`다. `source_checked`는 사람 승인이 아니다.

## 비용과 보호

기존 17.7GB native DB를 출력 파일에 복사하면서 SHA를 검사하고 대상 전체를 재읽어 검증한다. 이후 metadata 및 명시적 노출 등록·보조색인 이외 쓰기를 SQLite authorizer가 금지한다. 기존 node의 표시명·종류·식별자·degree는 수정하지 않는다. 누락된 canonical 노출 노드는 TEMP trigger가 사전 확정한 8개 열의 정확한 값으로만 삽입할 수 있으며 기존 노드의 `INSERT OR REPLACE`도 거부한다. effect·measurement·원본 selector·edge·perf_adj의 값과 native ID는 그대로 보존한다. Python 복사 버퍼는 4MiB, SQLite page cache는 64MiB이며 builder는 `:memory:` SQLite 전체 DB를 만들지 않는다. 다만 선택한 출력 파일시스템이 tmpfs이면 약20GB 출력 전체가 메모리 기반 scratch를 점유하므로 이 비용은 별도 계산해야 한다.

첨부 행렬·권리 파일도 원본 SHA와 대조해 복사한다. 실제 effect row count 확인 및 quick/integrity 검사는 build 비용이며 HTTP 성능에 포함하지 않는다. 실행자는 디스크·가용 메모리·I/O 예산을 확인하고, 다른 대량 작업과 겹치지 않는 별도 유한 시간 operator로 실행해야 한다. 중단·오류 시 `.NAME.composing`과 실패 상태를 남기고 최종 디렉터리를 활성화하지 않는다. 자동 재시도 시 같은 대상 경로를 덮어쓰지 않는다.

`build-progress.json`은 복제·메타데이터·첨부·export·`sqlite_quick_check`·`sqlite_foreign_key_check`·계약·SHA 검사를 각각 표시한다. HDD random I/O를 피하기 위해 승인된 project-owned tmpfs에 먼저 새 release를 만들 수 있지만, 기존 RAM replica와 후속 분석의 메모리까지 합쳐 여유를 검토해야 한다. 해당 scratch는 영구 보관이나 디스크 성능 증거가 아니다. 동일 quick/FK 검사를 완료한 manifest를 기대 SHA로 고정한 뒤 다음 명령으로 영구 디렉터리에 그대로 복제한다.

```sh
PYTHONPATH=/path/to/frozen-code python /path/to/frozen-code/scripts/promote_verified_release.py \
  --source /path/to/completed-tmpfs-release \
  --output /path/to/NEW-permanent-release \
  --expected-manifest-sha256 COMPLETED_MANIFEST_SHA256 \
  --receipt /path/to/operator-evidence/permanent-copy.json
```

promoter는 선언된 모든 파일과 원본 manifest를 전체 복사·대상 SHA 재읽기 검증하고, 원본 fingerprint/WAL·경로·symlink·부분 복사를 검사한 뒤 새 디렉터리로 원자적 rename한다. 영수증은 release 밖에 저장하며 manifest를 다시 작성하지 않는다. 이 동작 자체는 서비스 활성화가 아니다. 서비스의 새 source SHA·경로에 결속한 색인과 readiness 확인을 마친 다음 별도 운영 절차로 전환한다. 검증이 끝난 scratch의 해제는 해당 작업이 더 이상 참조하지 않는지 확인한 뒤 별도 절차로 진행한다.

## 결과와 상태

- 새 SQLite의 `science_record`가 수정된 canonical 자료다. `curation_record_digest`는 현재 payload SHA를 보존한다.
- `scientific_review_decision`과 `source_classification`은 별도 hash-bound 선언이다. canonical payload를 다시 바꾸거나 기존 승인을 이전하지 않는다.
- `effect_catalog`는 기존 다섯 부분 결과 키와 공개 행렬의 상대 경로를 보존한다. 값 복제가 아닌 파일 참조·selector 사전이다.
- API, DB metadata, `dataset-metadata.json`, `release-contract.json`, `manifest.json` 및 현재 CSV에 같은 새 release ID와 실제 DB 집계를 쓴다. base와 수정 패키지는 별도 provenance로 남는다.
- canonical `science_record(kind='exposure')`의 모든 등록을 `Exposure:<원본 ID>` 노드로 보존한다. source의 명시적 재배포 허용과 원본 ID를 검사하며 기존 `ClinicalStudy:*` 문맥 노드로 대신하지 않는다. 누락 노드만 `kind=diet`, 원본 label·food_terms·노출·대비·주장·source ID, `degree=0`으로 추가한다. 연결선·식품 조성·효능·건강 범위를 추정해서 만들지 않으므로 고립 노드는 정상 결과다.
- `composition/exposure-node-ledger.json`과 SQLite meta의 같은 원장에 추가/재사용 ID, canonical payload SHA, node SHA, source metadata SHA 및 `edges_added=0`을 기록한다. composition은 원장 SHA와 등록·추가 수를 결속한다. 현재 입력의 19개 중 2개가 누락됐다는 감사 결과는 입력별 사례이며 숫자를 코드에 강제하지 않는다.
- 있는 `node_kind`, FTS content/trigram, `perf_exposure_match`에는 새 노드의 읽기 투영만 추가한다. 기존 adjacency와 관계는 그대로 유지하며 startup에서 모든 canonical 노드의 정확한 ID·출처 권한·FTS 검색·same_record 대응을 검증한다. node 및 해당 보조색인의 CSV도 새 SQLite에서 내보내므로 이전 파일이 남지 않는다. `tier=all`은 고립 노출 검색을 포함하며, 관계 종류별 검색 필터가 연결선 없는 노출을 제외하는 동작과 구분한다.
- API의 `science_review_context`는 canonical SHA, 명시적 사람 판정, source-only 분류와 수록 disposition을 구분한다. `exclude`, `correct`, `unconfirmed`는 기본 식이 효능 근거로 승격하지 않는다. 감사 가능성을 위해 canonical 데이터와 수치는 삭제하지 않는다.
- 빌드 성공은 **candidate 생성 성공**이다. `submission_ready=false`, DOI 없음, 미공개 상태를 유지한다. 사람·권리·최종 평가·외부 공개 검증을 자동으로 완료 처리하지 않는다.

공개 서비스는 새 디렉터리를 `public_service.create_app`에 전달한다. `allow_candidate=True`는 candidate 탐색을 허용하는 옵션이며 사람 승인이 아니다. startup에서 canonical273·selector94 등의 실제 소형 집계와 dependency SHA를 검사하고, 파일·DB·API release가 다르면 기동을 거부한다. 숫자273/94는 이번 입력의 규모이며 구현에서 다른 자료를 그 숫자로 강제하지 않는다.

새 DB의 path/inode가 달라지므로 **기존 path-bound external query/ordered/RAM index를 새 source의 색인인 것처럼 재사용하지 않는다.** 별도 새 파생 경로에 정확한 source SHA/fingerprint로 구축하고 기존 것과 구분한다. byte-copy로 보존된 native row IDs 및 그래프 구조의 내부 보조색인은 새 release에 재결속한다. 실제 staging 검증과 복원 확인은 builder와 별도의 운영 단계다.
