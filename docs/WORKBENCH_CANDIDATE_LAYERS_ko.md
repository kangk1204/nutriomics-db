# 예측·문헌 후보 계층의 재구축과 검증

이 확장은 기존 사람 중재와 정량 오믹스 결과를 수정하지 않는다. SCREEN GraphRegLR과 PubMed 문헌을 새로 확보하고 별도의 근거 계층을 만든다. 다운로드·원본 대조는 전문가의 과학적 검수를 대신하지 않는다.

## 계층과 해석

| 계층 | 원본 | 허용되는 해석 |
|---|---|---|
| `regulatory_predicted` / `predicted` | SCREEN V4-hg38, GraphRegLR 행 | K562 또는 GM12878의 계산 예측. 식이 반응·사람 임상 효과가 아니다. |
| `literature_candidate` / `extracted` | 버전이 고정된 PMID의 새 PubMed title/abstract | 명시적 관계 문장의 자동 추출 후보. 의미·대상군·섭취 조건 검수는 대기 상태다. |
| 기존 사람 식이중재 | 기존 release의 연구·대비·효과 | 해당 연구 설계·용량·조직·시점에 한정한 관찰 결과. |

GraphRegLR 원본 마지막 필드는 원문 문자열로 보존한다. thresholded ENCODE 파일에서 유래한 SCREEN의 필드이며 효과량·P/FDR·개인 건강 확률로 변환하지 않는다. `1`을 100% 임상 신뢰도로 읽으면 안 된다. 원본의 다른 method 행, 정확한 중복 행, 고유 prediction 기록 수를 별도로 기록한다. 파일명만 보고 모든 행을 GraphRegLR로 분류하지 않는다.

문헌의 종·조직·용량·기간·건강 효과는 확정 값으로 채우지 않는다. 추출 규칙이 인식한 원문 표현은 candidate payload에만 남긴다. 원문 문자 위치 일치는 문장의 의미가 맞다는 증거가 아니다. 같은 PMID의 여러 후보는 독립 연구 여러 편이 아니다. 철회된 논문의 후보는 관계 그래프에서 제외하고 문서 기록에 남긴다.

문헌 후보는 화면의 **근거 종류 → 자동 문헌 후보**에서 표시한다. 이때 해당 연결망에 자동 추출 후보가 포함된다. 일반 연결망에서는 기본적으로 제외한다. 예측·추출 후보의 상세 패널에는 검증 한계와 출처를 표시한다.

## 재구축

기존 코드와 데이터 release를 보존하고 새 영구 디렉터리를 사용한다. 기존 597편 목록은 문서 식별자 선택에만 쓰며, 이전 초록이나 추출 관계를 복사하지 않는다. 현재 사람 연구의 PMID도 합친다.

```bash
python -m scripts.reacquire_candidate_layers \
  --root <new-acquisition-directory> --source <source-directory> \
  --hgnc <verified-hgnc-complete-set.tsv>

python -m scripts.build_candidate_extension \
  --parent <verified-explorer-release> --root <new-acquisition-directory> \
  --hgnc <verified-hgnc-complete-set.tsv> \
  --output <new-internal-release> --release-id <new-internal-release-id>

python -m scripts.build_candidate_extension \
  --parent <verified-public-service-release> --root <new-acquisition-directory> \
  --hgnc <verified-hgnc-complete-set.tsv> \
  --output <new-public-release> --release-id <new-public-release-id> --public

# Link only exact native IDs recorded by HGNC; retain original nodes.
python -m scripts.bridge_candidate_identifiers \
  --parent <new-internal-release> --root <new-acquisition-directory> \
  --hgnc <verified-hgnc-complete-set.tsv> \
  --output <linked-internal-release> --release-id <linked-internal-release-id>

python -m scripts.bridge_candidate_identifiers \
  --parent <new-public-release> --root <new-acquisition-directory> \
  --hgnc <verified-hgnc-complete-set.tsv> \
  --output <linked-public-release> --release-id <linked-public-release-id> --public
```

완료된 수집 디렉터리와 release는 덮어쓰지 않는다. 다운로드 중단은 원본 해시가 일치하는 배치만 재사용하며, 다음 갱신은 날짜가 다른 새 디렉터리에서 실행한다. ENCODE metadata·이용정책, SCREEN 설명·다운로드 페이지도 해시와 함께 보존한다. schema 변경, 새 파일 accession, 비정상 수치, 다른 method는 묵시적으로 받아들이지 않는다.

## 이용조건과 공개 범위

GraphRegLR은 SCREEN의 원본 행과 released ENCODE 파일 `ENCFF431TFM`, `ENCFF140XYG`의 대응을 확인하고, [ENCODE 데이터 이용정책](https://www.encodeproject.org/about/data-use-policy/)을 근거로 자료 재사용을 허용한다. 이 판단은 해당 데이터에만 적용하며 코드 라이선스를 데이터에 적용하지 않는다. SCREEN과 GraphReg 원 연구의 인용을 유지한다.

PubMed 초록·파생 주장에 대한 문서별 재배포 조건은 미확정이다. 내부 연구 화면에는 출처와 후보를 제공하고, 공개 패키지에는 문서 식별자·출처·후보 개수·제한 사유만 포함한다. 원문과 관계 후보를 공개 허용 상태로 자동 승격하지 않는다. HTML/CSV/JSON도 같은 제한을 적용한다. 원문 이용조건이 확인되면 별도 새 release에서 범위를 넓힌다.

## 검증

대형 근거 표의 깊은 페이지는 정량 결과를 복제하지 않는 별도 순번 색인으로 제공할 수 있다. 기존 release 파일은 수정하지 않는다. 원본 전체 SHA-256과 파일 변경 상태를 묶고, 원래 `layer,study_id,tissue,id` 순서의 모든 ID를 대조한다. 요청은 해당 순번의 ID만 읽고 원본 기록을 조회한다. 색인이 다른 release를 가리키거나 원본이 변경되면 오류로 반환하며 0건으로 바꾸지 않는다. 색인 파일도 응답 캐시의 변경 감지 대상이다.

```bash
python -m scripts.build_evidence_query_index \
  --source <release>/<explorer.sqlite-or-nutriomics.sqlite3> \
  --manifest <release>/manifest.json --output <new-index.sqlite3>
export NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX=<new-index.sqlite3>
```

필터가 없는 전체/계층별 페이지에 이 순번 색인을 사용한다. 유전자·조직·연구·검수·문자열 조건은 기존 조건 검사를 유지한다. 반복 성능 평가 중 실제 HDD 대기로 발생한 시간 초과는 실패 기록으로 보존하고, 수정 뒤 같은 질의 집합을 다시 평가한다. 서버의 다른 작업이나 전역 디스크·메모리 설정은 변경하지 않는다.

색인 schema 2는 모든 근거의 양끝 ID도 원본과 대조한다. 항목 상세·HTML/CSV/JSON에서는 작은 ID 색인에서 실제 표시할 근거를 정한 뒤 해당 원본 행만 읽는다. 고연결 유전자에서 100개 미리보기를 정렬하려고 수천 개의 원문 payload를 읽는 일을 줄인다. 자기 자신을 연결한 관계는 한 번만 집계하며 원래의 정렬과 총수를 유지한다. 효과량·통계값·원문은 이 색인에 복제하지 않는다.

`source-accuracy.json`은 수록된 모든 GraphRegLR 행의 ID·원본 필드·세포주·문자열 점수·계층과 문헌 후보의 문자 위치·PMID·방향·검수 상태를 대조한다. 이것은 원본 보존 검사이며 전문가 의미 정확도 평가는 아니다.

SQLite authorizer로 기존 조성·연구·정량 결과·효과 참조 테이블의 쓰기를 금지한다. 기존 core DB도 변경하지 않는다. 그래프·검색·노출 색인만 재구축하고 기존 효과 참조의 release 바인딩을 새 버전으로 갱신한다. 닫힌 SQLite 파일의 integrity/FK 검사와 manifest SHA-256을 확인한다.

같은 유전자의 NCBI Gene·Ensembl·HGNC ID는 공식 HGNC 기록에서 유일하게 대응할 때만 별도의 식별자 관계로 연결한다. 이름·별칭만으로 노드를 합치거나 효과를 옮기지 않는다. 문헌의 유전자 인식은 ID 대응이 가능하더라도 자동 후보 상태를 유지한다. HDD의 무결성 검사에서는 해시가 같은 RAM 검사본을 사용할 수 있으며, 검사본 해시·전체 integrity/FK 결과와 영구 파일의 불변성을 기록한다. HTTP 성능 평가는 영구 디스크의 release를 사용한다.

```bash
python -m scripts.freeze_candidate_performance --release <new-release> --output <fixed-cases.json>
python -m scripts.benchmark_explorer_performance benchmark \
  --base http://127.0.0.1:<port> --spec <fixed-cases.json> \
  --output <measurement-directory> --runs 100 --concurrent \
  --source-manifest <new-release>/manifest.json
```

기존 검색·매칭·오믹스 페이지와 예측 고연결 노드, 세포주 필터, 깊은 근거 페이지, 문헌 후보 포함/제외를 같은 고정 평가에서 검사한다. 준비 요청은 warm 반복에서 제외한다. 서버 내부 HTTP와 SSH 터널을 구분하고, 한 HDD에서 대형 구축과 성능 평가를 동시에 실행하지 않는다. 80/200 및 250/750 표시 한도를 줄여 통과시키지 않는다. 실패 결과는 보존하고, 모든 필수 검증을 통과한 release만 활성화한다.

전문가 문헌 검수, 질병 인과성 검증, 개인 예측 모델, DOI 등록은 이 기술 검증으로 완료되지 않는다.
