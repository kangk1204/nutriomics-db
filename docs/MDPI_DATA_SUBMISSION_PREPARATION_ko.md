# MDPI Data 투고 준비

Data Descriptor의 중심 산출물은 버전이 고정된 데이터, 자료 설명, 수집·처리·검증 방법, 재사용 조건 및 코드입니다. 공식 기준은 [Data 저자 안내](https://www.mdpi.com/journal/data/instructions)와 [저널 범위](https://www.mdpi.com/journal/data/about)를 확인합니다. 현재 문서는 지역 서버에 구축한 자원을 설명하는 투고 준비용 자료이며, 실제 제출·DOI 등록을 완료했다는 증빙이 아닙니다.

## 제출 패키지 구성

| 산출물 | 준비 방식 |
|---|---|
| Data Descriptor 영문 초안 | 실제 release와 허용 데이터 패키지의 manifest에서 규모를 읽어 생성 |
| 전체 내부 DB | 불변 release·source hash·별도 원본 오믹스 효과 키를 보존 |
| 재배포 가능한 데이터 | source별 정책을 적용한 SQLite subset; 전체 DB와 수록 범위를 별도로 기록 |
| Source catalog | 원본 ID·URL·release·해시·허용 상태, 제공하지 않은 값의 접근 경로 |
| Data dictionary | 실제 schema column 목록과 생물학적 의미·해석 제한 |
| Code archive | Python·R·React 소스, dependency lock, 업데이트·검증·내보내기 코드 |
| Technical validation | 자동 회귀 테스트, 음식 계산, original-source 행 보존, API·브라우저·offline·복원 검사 결과 |
| 해석 평가셋 | 200개 후보 source fact, 개발 70개/봉인 30개 질의; 전문가 검수 대기 상태 유지 |

본 패키지는 로그인 없는 로컬 Explorer와 별개로 재사용할 수 있도록 준비합니다. 내부 DB 행 수를 공개 subset의 규모로 쓰지 않습니다. 총 관계 수에는 음식 조성이 포함되므로 모두 ‘분자 상호작용’으로 부르지 않습니다. gene node 수는 서로 다른 namespace를 포함하며 unique biological gene 수로 해석하지 않습니다.

## 완료 전 확정할 항목

1. source별 상세 데이터 재배포 권한을 검토하고, 공개 파일의 실제 범위를 고정합니다. SCREEN/eQTM의 원본이 공개되어 있다는 사실만으로 blanket license를 부여하지 않습니다.
2. 저자·순서·CRediT·공식 영문 소속·연구비·이해관계·기관의 윤리 판단을 확정합니다. 자동화 도구 활동으로 저자 기여를 생성하지 않습니다.
3. 연구자 전문가가 원문 주장·용량·조직·방향·연구설계·불일치를 검수합니다. `source_checked`를 expert-approved로 바꾸지 않습니다.
4. 봉인 질의로 비교 평가를 한 뒤 분모·신뢰구간·비교 불가 범위를 보고합니다. 자동 정책 검사 점수를 인간 관련성 정확도로 바꾸지 않습니다.
5. 공개 repository에 검토된 data/code release를 등록하고 영구 DOI/URL·tag·license를 manuscript에 반영합니다. 현재 로컬 파일을 공개 저장소로 기재하지 않습니다.
6. 최종 표·그림·본문의 counts와 source catalog를 동일 release 기준으로 점검한 후 제출합니다.

## 주장 범위

현재 제공하는 것은 식품 조성과 사람 중재·조절 근거의 출처 추적 구조입니다. eQTM의 독립 코호트 재현은 식이 반응 예측의 외부 검증이 아닙니다. CRISPR 시험 목록은 positive regulatory effect 집합이 아닙니다. GraphRegLR는 검증된 식품 효능이 아닙니다. 건강 관련성·유전자 방향·CpG 변화에 개인의 이득/손해 점수를 부여하지 않습니다.

최신 내부 release와 permitted subset 규모는 각 manifest를 이용해 동적으로 기록합니다. 과거 과제의 ‘식품성분 55,379건·상호작용 510,620건’ 같은 별도 자산 수치는 이 release의 단위와 출처가 대응할 때만 비교합니다. 코호트·마커·AUC·LOOCV·후보물질 실적도 원본 증빙이 확인된 경우에만 본 자원의 성과로 사용합니다.
