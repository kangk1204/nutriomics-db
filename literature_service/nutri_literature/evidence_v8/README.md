# NutriOmics Evidence v8

생성형 모델 호출 없이 실행하는 독립 실험 패키지다. 기본 엔진·과거 모델·서버 작업을 변경하지 않는다. 모델 성능이 개선됐다는 주장은 `evaluation.json`이 만들어진 후에만 가능하다.

## 실행

`EVIDENCE_V8_HOME`은 이전 실험과 다른 경로여야 한다. Python 3.12, 고정 requirements, RTX 50 계열을 지원하는 torch 2.10 cu128 환경을 사용한다.

```text
python scripts/bootstrap_evidence_v8.py --source-root <검증한 r2 runtime 스냅샷> --output <v8 runtime> --resolve-models
python scripts/evidence_v8.py probe --home <v8 runtime>
python scripts/evidence_v8.py smoke-ner --home <v8 runtime>
python scripts/evidence_v8.py smoke-id --home <v8 runtime>
python scripts/evidence_v8.py audit --home <v8 runtime>
python scripts/evidence_v8.py prepare --home <v8 runtime>
python scripts/evidence_v8.py train-ner --home <v8 runtime>
python scripts/evidence_v8.py train-id --home <v8 runtime>
python scripts/evidence_v8.py train-relations --home <v8 runtime>
python scripts/evidence_v8.py freeze --home <v8 runtime>
python scripts/evidence_v8.py predict --home <v8 runtime>
```

`evaluate`는 원래 로컬 작업공간에서 100편 전체 예측의 해시를 검증한 뒤 실행한다. 학습 서버에 개발 정답이나 봉인 정답을 복사하지 않는다. `resume`은 다음 미완료 단계 하나를 실행하고, `status`는 저장 상태만 읽는다. 코드·입력·모델이 동결 후 변경되면 새 실험 경로가 필요하다.

## 구현한 학습

- SapBERT revision을 먼저 확인·고정하고, 공식 별칭 검색과 임베딩 검색의 200개 후보를 결합한다. 학습·추론 모두 원래 ID가 포함된 50개＋NIL을 사용한다. 정답 ID를 추가하지 않는다.
- NLM 외부 415편으로 v8 자체 2종 BIO NER를 학습한다. 주석되지 않은 다른 개체 종류는 손실에서 제외한다. NER 기본 구성은 고정된 3 epoch·LR 2e-5다.
- ID 외부 5-fold·내부 3-fold의 각 학습 집합마다 상류 NER를 다시 학습한다. 검증 그룹 전체를 제외하고, ID 학습행도 추가 5-fold NER 예측으로 생성한다. 기존 r2 OOF의 fold 불일치를 새 실험에 전달하지 않는다.
- ID 선형 listwise 모델과 BiomedBERT 교차 인코더를 비교한다. 실제 개체이지만 ID 검색에 실패한 사례는 개체 유효성 양성을 유지하고 ID 순위 손실만 마스킹한다.
- 자동 ID 연결과 철회 정책은 종류별·동작별로 분리한다. 최소 50건·Wilson 95% 하한 90% 미달 시 순위만 제공한다. 사람·생쥐·랫드의 단일 종 근거와 후보 종이 맞지 않으면 유전자 변경을 금지한다.
- 관계 존재·다중 유형·방향·직접 근거 헤드를 분리하고 최대 8개 문맥을 문서 단위로 결합한다. 문서 정답을 모든 문장의 양성으로 복제하지 않는다. 상류 ID가 틀려도 위치로 대응한 실제 관계 의미는 양성이다.
- 문서 관계 가설에 원문 위치를 연결하지만 직접 이벤트나 관측 결과로 자동 확정하지 않는다. 직접 근거 주석이 없으면 근거 헤드에 가짜 감독 신호를 만들지 않는다.
- 최종 3시드 앙상블의 정책은 별도의 훈련 내부 OOF 앙상블 점수로 다시 보정한다. 개발 100편으로 설정을 선택하지 않는다.

엄격한 상류 NER 재학습 때문에 본 학습은 느리다. 문헌 그룹 구성에 따라 약 160개의 서로 다른 작은 NER 적합과 수십 개의 ID 신경망 적합이 발생할 수 있다. 각 학습 집합·입력·가중치·소스 해시로 완료 체크포인트를 재사용한다. 소규모 실제 GPU 시험으로 속도·최고 메모리를 측정한 뒤 전체 소요를 판단해야 한다.

## 독립 보조 실험

`clinical`은 공식 literal P/I/O span으로 3개 CPU CRF를 학습한다. `clinical-role`은 PubMed RCT의 원래 문장 역할을 학습한다. 두 출력은 분리되며 Results 문장이라고 관측 결과를 확정하지 않는다. 두 단계에는 `data/clinical-train.json`, `rct-train.json`, 정확한 SHA가 기록된 `clinical-audit.json`이 필요하다.

`dapt --corpus <manifest 또는 디렉토리>`는 모든 공개 정답 자료가 제외됐고 영어·이용조건·정확 토큰화가 확인된 코퍼스만 사용한다. 대기 중인 candidate 파일은 읽지 않는다. 최대 5천만 토큰을 한 번 순회하며, 학습 결과는 기본 인코더로 자동 채택하지 않는다. 지도 과제의 원래 인코더 대비 제거 실험이 추가로 필요하다.

## 아직 완료하지 않은 항목

- DrugProt/CG native task의 새로운 v8 학습·표현 전이와 공식 이벤트 평가는 미연결이다.
- GNorm2 비교, MedCPT 검색 비교, 추가 사전학습 인코더의 지도 평가 비교는 미연결이다.
- 완전 자동 ICO·군·용량·시점 연결의 새로운 v8 학습·평가는 미연결이다. 임상 BIO 성능으로 이를 대신하지 않는다.
- 같은 100편의 반복 개발 비교는 독립 최종 성능이 아니다. 봉인 시험은 기본 차단되며 전체 자료 중복 감사와 모든 필수 목표의 검증이 선행돼야 한다.
- 영양 문헌의 전문가 정확도, 검수 시간 절감, 7일 실제 운영은 이 학습 코드로 입증되지 않는다.

## 안전한 실행

H200/A6000과 등록된 금지 UUID를 거부한다. 같은 GPU의 다른 계산 프로세스, 가용 RAM 16GiB 미만, 디스크 50GiB 미만이면 기존 작업을 종료하지 않고 대기한다. 작업 RSS 8GiB·GPU 13GiB를 적용하고 v8 자체 GPU 장부를 기록한다. r2의 72시간 장부는 읽거나 수정하지 않는다. 완료·대기·실패를 구분하며 미준비 자료나 의존성 누락을 성공으로 처리하지 않는다.
