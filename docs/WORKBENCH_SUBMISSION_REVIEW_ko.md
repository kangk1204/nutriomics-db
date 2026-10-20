# Data Descriptor 후보판: 원문 사전점검과 사람 검수

2026-10-20. 이 절차는 자동 점검, 원문 기반 제안, 실제 사람 판정을 분리한다.
수집 성공·해시 일치·원문 수치 대조를 전문가 승인으로 바꾸지 않는다.

## 과학적 사전점검

```bash
python -m scripts.precheck_scientific_submission \
  --review scientific-review.json \
  --facts primary-paper-fact-candidates.json \
  --catalog source-catalog.json --evidence-root . \
  --authoritative-science science-records.authoritative.jsonl \
  --output scientific-precheck
```

입력 파일은 해당 데이터 검토 패키지의 명시된 버전·해시를 사용한다. 9개 연구와
14개 분석 실행의 검수 묶음, 기록별 원문 위치·출처 해시·제안·미확인 사항을
생성한다. 알려진 수정 이외의 기록 변경이나 해시 불일치는 거부한다. 기존
동결 DB를 수정하지 않는다. `human-decisions.json`은 사람의 판정이 채워지기
전까지 모두 대기 상태다. 같은 연구에 속한다는 이유만으로 오믹스 변화가
임상 결과를 매개했다고 표시하지 않는다.

이전 200개 fact 후보에는 저장소·식별자·분석 이력 항목도 있다. 이를 원문
과학적 사실 200개 또는 독립 검증 200회로 보고하지 않는다. DNAm 고정 1kb
영역 검정은 발견·검증된 DMR 집계와 구분한다.

## 음식 기준

```bash
python -m scripts.precheck_food_semantics \
  --review food-semantic-review.json \
  --internal-panel kfind_food_selection.json \
  --public-panel food-selection.json --output food-review
```

코드·원문 음식명·선정 파일의 일치와 문자 그대로의 조리상태 단서는 기계
점검한다. 음식명만으로 조리상태, 밀도, 가식부 수율을 추정하거나 사람 승인
상태를 만들지 않는다. 공개 50종과 내부 50종은 다른 패널이며 각각의 출처와
재배포 범위를 유지한다.

## 조회 평가

```bash
python -m scripts.prepare_retrieval_review \
  --benchmark benchmark-review --curator human_exposures.v1.json \
  --science final-public-science.json --output benchmark-review-v2.2
python -m scripts.evaluate_retrieval_review --package benchmark-review-v2.2 \
  --partition holdout --human-gold completed-human-gold.json --validate-only
```

질의만 포함한 최종 30개, 시스템 답변을 포함하지 않는 출처 목록, 빈 정답
양식을 사용한다. 원래 sealed 파일을 열지 않는다. 원래 큐레이션 해시의
정확한 파일이 없었던 문제와 분할 변경을 기록한다. 새 분할은 개발 6개 연구와
최종 3개 연구의 원 논문이 겹치지 않도록 고정한다. 대상자·코호트의 독립성까지
검증했다는 뜻은 아니다.

실행 전 사람이 원문을 읽고 관련·무관·판단 불가와 원문 위치를 확정한다.
수정된 정답·질의·출처·release 해시와 일회용 승인 영수증이 일치할 때만 최종
평가를 실행한다. 실행 명령은 생성된 `REVIEW_README_ko.md`를 따른다. 정답이
없으면 과학적 precision/recall은 null이며, 규칙 일치율을 대신 쓰지 않는다.
식품을 선택할 수 없는 질의는 근거 0건과 분리한다. 최종 결과로 규칙을
재조정하면 새 버전과 새 동결 평가가 필요하다.

## 공개용 패키지

```bash
python -m scripts.prepare_submission_package prepare --help
python -m scripts.prepare_submission_package assemble --help
```

원본 59개 파일을 복제하지 않고 동결 해시와 기존 전체 검증 영수증에 연결해
준비할 수 있다. 이 경우 원본이 바뀌지 않았는지 별도로 검사하고 이를 새 전체
해시 계산으로 표현하지 않는다. 실제 조립은 목적 디렉터리에 복사하면서 전체
해시를 재검증하고 완료된 디렉터리만 원자적으로 확정한다.

`science-records.authoritative.jsonl`과 소형 `science.authoritative.sqlite3`
등 이름은 생성된 manifest에 따르며, 이 수정본을 과학 메타데이터의 기준으로
사용한다. 원래 대형 SQLite는 이전 메타데이터를 포함한 동결 파일로 보존한다.
두 파일의 역할을 혼동하지 않는다. 변경 전후 해시, 수정 이유, 원문 위치를
기록한다. 오믹스 행렬을 다시 계산하거나 분석 키를 바꾸지 않는다.

원래 release 구축 코드, 실제 분석 producer 원본, 이전 reader 코드와 새 검수
워크플로 코드의 역할을 구분한다. 새 코드가 과거 분석을 생성했다고 주장하지
않는다. MIT는 자체 코드에, CC BY 4.0은 명시적으로 허용된 자체 큐레이션·분석에
적용한다. 외부 자료의 제한은 파일·출처 단위로 유지한다.

DOI·저자·윤리 판단·이용조건 최종 확인·사람 정답·논문 투고는 자동으로 생성하지
않는다. GitHub 코드 공개나 기술 테스트 통과만으로 제출 준비 완료가 되지 않는다.
공개 웹 운영, 실제 오프라인 HTML 시험, 이전 내부 release 복원 등 아직 통과하지
못한 프로젝트 조건은 별도 미완료 항목으로 유지한다.
