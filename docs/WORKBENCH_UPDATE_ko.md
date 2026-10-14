# NutriOmics DB 구축·갱신 안내

음식 조성, 사람 식이중재 결과, 외부 조절 근거를 별도 계층으로 적재합니다. comprehensive는 지정한 공개 파일의 수록 범위를 뜻합니다. 모든 음식·조직·유전자·질환의 효과가 검증되었다는 뜻으로 사용하지 않습니다.

## 디렉터리와 release

승인된 서버의 `<project-root>`를 유지합니다. `source`는 서비스 코드, `data/db`는 기존 음식·오믹스 원본 DB, `data/explorer/releases/<release_id>`는 불변 탐색 DB, `data/explorer/current`는 활성 release 링크입니다. 수집 파일은 `update_cache/objects/<sha256>`에 저장하고 원본 URL·시각·해시·ETag·이용조건은 receipts에 보관합니다. 실패한 다운로드와 staging은 진단용으로 남습니다. 원본과 이전 release는 삭제하지 않습니다.

## 구축 범위

| 자료 | 실제 처리 범위 | 갱신 방식 |
|---|---|---|
| 기존 식품·오믹스 | 활성 release의 식품, 조성, CTD 관계, 중재·대비, 원본 효과 참조 | 기존 `build_explorer_release.py`와 분석별 적재 코드로 새 기본 release 생성 |
| SCREEN CRISPR | 지정 V4-hg38 파일의 시험된 쌍 전체 | HTTPS 조건부 다운로드, 모든 원본 행 파싱 |
| SCREEN GraphRegLR | 지정 파일에서 method가 GraphRegLR인 모든 행, 패널 제한 없음 | 다른 method와 중복은 제외 집계 |
| FHS eQTM | 출판된 ST1/ST2 선택 요약, ST3/ST4의 WHI/JHS 재현 결과 | 다운로드한 원본 workbook의 schema·해시 확인 후 별도 adapter |
| EvoImmunoPop | 원래 수록된 성인 일차 단핵구 eQTM | 원 출판물과 snapshot 버전을 유지; Atlas preprint와 별도 구분 |
| HGNC | approved 항목의 정확한 NCBI Gene–Ensembl 식별자. 두 ID가 기존 DB에 존재하며 일대일일 때만 연결 | 공식 전체 TSV 자동 수집, 중복·모호한 대응·version 접미사 불일치 제외. 이름 유사도로 합치지 않음 |
| Reactome hierarchy | 지정 다운로드 파일 snapshot | 자동 수집 가능, enrichment gene set 변경에는 별도 분석 필요 |
| RDA, EWAS, GEO 등 | registry의 현재 manual/disabled 범위 | 명시적 자료 URL·사용조건·schema 검토 후 adapter 추가 |

자동 수집 가능 여부와 실제 DB 통합 여부는 각각 보고합니다. 다운로드 페이지 자체를 데이터 파일로 적재하지 않습니다. 동일 사람의 반복 시료와 동일 PMID의 반복 관계는 독립 대상자·연구로 합산하지 않습니다.

## 반복 실행

프로젝트 Python 환경을 사용합니다. 전역 패키지나 R 환경을 수정하지 않습니다. 아래 명령은 서버 `source`에서 실행합니다.

```bash
./.venv/bin/python scripts/update_nutriomics.py plan --root ../update_cache
./.venv/bin/python scripts/refresh_workbench.py \
  --root ../data/explorer \
  --from-release ../data/explorer/current \
  --release-id explorer-YYYYMMDD-vN \
  --source screen-crispr --source screen-graphreg --source hgnc-identifiers
./.venv/bin/python scripts/update_nutriomics.py validate \
  --root ../data/explorer --release ../data/explorer/releases/explorer-YYYYMMDD-vN
```

동일 source 해시는 원본 bytes와 adapter 결과를 재사용합니다. 새 해시는 새 content object와 receipt를 만들고, 새 release에서 해당 source family의 이전 파생 관계를 대체합니다. 과거 release는 그대로 유지됩니다. 이름만 같은 유전자를 합치지 않습니다.

FHS 재현 표를 갱신하려면 다음을 실행하고 생성된 sidecar를 `refresh_workbench.py --sidecar`에 전달합니다.

```bash
./.venv/bin/python scripts/collect_fhs_replication.py \
  --workbook /path/to/original-supplement.xlsx --output /path/to/new-sidecar.sqlite3
```

## 검증과 운영 반영

DB 해시, SQLite integrity/foreign key, 포함·제외 집계, source schema, 조건·단위·결측 처리, 음식 50종의 계산 일치를 확인합니다. `tests/test_workbench.py`, `tests/test_hgnc_identifiers.py`와 기존 Explorer/API 테스트를 실행하고, 127.0.0.1:8020 staging 화면에서 검색·선택·대비·출처·export를 확인합니다. API 성능 측정은 데이터 크기와 질의를 함께 고정합니다. 사람의 문헌 해석 검수는 자동 테스트와 구분합니다.

서비스 코드까지 바뀌면 `deploy_explorer_server.py`로 코드·dist·release를 함께 백업 후 반영합니다. 데이터 링크만 바뀌는 경우의 원자적 전환·복원 명령은 다음과 같습니다.

```bash
./.venv/bin/python scripts/update_nutriomics.py promote \
  --root ../data/explorer --release ../data/explorer/releases/explorer-YYYYMMDD-vN
./.venv/bin/python scripts/update_nutriomics.py rollback \
  --root ../data/explorer --release ../data/explorer/releases/PREVIOUS_RELEASE
```

`promote`는 해시·구조를 검증하지만 논문 해석이나 화면 품질을 승인하지 않습니다. 위 staging 검증 후 실행합니다. source schema가 바뀌면 adapter는 실패하도록 두고, 기존 운영 release를 유지합니다. 특정 시각의 반복 작업은 이 명령을 연구실의 기존 스케줄러에서 실행할 수 있습니다. 스케줄러·클라우드 공개는 이 구현에 포함하지 않습니다.

## 확장 규칙

새 adapter는 `ng_release`, `ng_entity`, `ng_study`, `ng_evidence`를 출력합니다. effect/P/FDR/CI의 정의·원문 위치·가설군이 확인되지 않으면 null과 결측 상태를 유지합니다. source-checked와 expert-approved는 별개입니다. 질환 관련성·eQTM·예측 조절 연결을 개인 건강 효과로 승격하지 않습니다. 배포권이 명시적으로 확인된 행만 상세 export를 허용합니다.

공개 논문 패키지는 허용 데이터, source catalog, schema, code, 환경 lock, 재현 명령, 해시, 제한 사유를 포함합니다. 재배포 이용조건 미확인 자료의 상세 수치는 원본 사이트에서 다시 확보하도록 합니다. 공개 DOI와 최종 저자·기여·기관 판단은 실제 등록 후 기록합니다.
