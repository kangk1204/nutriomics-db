# 최종 제출 보고·과학적 검수 패키지

이 파이프라인은 실제 release의 canonical contract와 `science_record`를 읽어 한국어 인쇄용 HTML, JSON/CSV 주장 장부, 과학적 검수 파일, 분석 metadata 감사를 생성한다. 원 DB와 분석 파일은 읽기만 한다. 보고서 생성이 전문가 승인, DOI 등록 또는 투고 준비 완료를 뜻하지 않는다.

## 실행

```powershell
.venv-win/Scripts/python.exe scripts/build_submission_report.py --internal-contract <internal-contract.json> --public-contract <public-contract.json> --science <final-public-science.json> --clinical-summary <clinical-summary.json> --validation-receipts <dated-receipts.json> --output outputs/publication/2026-10-19/submission-report
```

`--science`는 최종 SQLite 경로나 `{ "science_records": [{"id": ..., "kind": ..., "metadata_json": ...}] }` 형식의 읽기 전용 추출본이다. contract는 `nutrigenomics.release_contract.build_release_contract`가 만든 실제 최종 자료를 사용한다. 집계가 다르면 보고서를 생성하지 않는다. 검증 영수증은 파일명, 실제 SHA256, 기준일과 적용 release를 기록한다. 과거 성능은 날짜를 표시하고 최종 release 통과로 승계하지 않는다.

Python에서 `build_review_package(db, output)`를 호출하면 SQL 연결 또는 추출본에서 모든 대상 기록을 다시 생성할 수 있다. 기존 210건을 고정 상수로 사용하지 않으며 새 `clinical_endpoint_result`, `clinical_study_audit`도 포함한다. 레코드 내용의 canonical SHA256에 판정을 결속한다. 원문 검수는 생성된 `review/scientific-review.html`에서 수행한다.

## 사람 검수

- 기록별 원 논문·표·보충 시료표 위치와 이유를 입력하고, 검수자 이름과 사람 검수자의 직접 확인을 기록한다.
- `accept / correct / exclude / unconfirmed / pending` 중 선택한다. 수정 판정은 수정안을 필수로 기록한다. 실제 데이터 수정은 새 release에서 수행하고 새 해시로 재검수한다.
- 저장한 판정은 `--decisions <scientific-review-decisions.json>`으로 검증한다. 해시 불일치, 알 수 없는 기록, 중복, 원문 위치 누락, AI 판정 또는 날짜 오류는 거부한다.
- 판정은 기록 집합 SHA256를 키로 브라우저에 자동 저장된다. 새 세션에서 복원하고 저장한 JSON을 가져올 수 있다. 다른 release의 기록·변경된 해시·중복·AI 승인은 가져오지 않는다. 브라우저 저장이 제한될 수 있으므로 JSON도 별도 보관한다.
- 오프라인 검수 파일은 자기 선언이며 검수자의 신원을 인증하지 않는다. 교수님 단독 검수는 독립 검증 또는 검수자 간 일치도로 보고하지 않는다.
- 자동 metadata 감사는 선언된 모델, 시료 범위, 통계 척도, FDR family와 고정 1kb 영역 표기를 점검하는 보조 결과다. 원 논문 해석과 실제 설계행렬 타당성 검토는 사람 검수에 남긴다.

## 보고 범위

`submission-report.json`은 내부와 공개 규모, 현재 검수 현황, 날짜별 검증, 남은 제출 조건을 분리한다. `claim-ledger.csv/json`은 기재 수치의 release와 단위를 보존한다. `legacy-claims.json`은 55,379 / 510,620 / 200마커 / 약 2,700명 / AUC 0.88 / cg04983977의 과거 문구를 별도 증빙으로 관리한다. 과거 준비 보고서 문구는 원 DB 집계 또는 모델 평가 증명으로 승격하지 않는다.

봉인 질의의 정답을 읽지 않으며 금표준 정답, precision 95%, 독립 평가 결과를 생성하지 않는다. 데이터·코드 DOI와 저자·연구비·기관 윤리 판단은 사람이 최종 확인한다. `journal_submission_ready`는 이 자동 파이프라인에서 항상 false다.

## 고정 임상 원본 재확보·지속 갱신

```bash
python -m scripts.reacquire_clinical_sources --output <new-source-directory>
python -m scripts.build_clinical_source_facts --sources <new-source-directory> --output <new-candidate.json>
python -m scripts.reacquire_clinical_sources --check-latest --output <new-monthly-check-directory>
```

고정된 5개 원본의 해시가 모두 같아야 현재 수치표를 재생성한다. 새 내용은 별도 내용 해시로 보존하고, 이용조건·표 구조·수치 재검토 후 새 큐레이션 버전을 만든다. 원본 변경으로 기존 사람 검수가 자동 승계되지 않는다. 정확한 최초 생산 코드도 `source_snapshots/clinical_layers`에 보존한다.

공개 앱의 `/api/v1/public/files`는 현재 release manifest의 파일명·크기·SHA256만 반환한다. `/api/v1/public/files/<path>`는 그 목록에 있는 파일만 제공하고 Range 다운로드를 지원한다. DB·CSV·Parquet 다운로드는 전문가 검수 완료나 DOI 등록을 의미하지 않는다. 내부 release나 임의 서버 경로로 대체하지 않는다.
