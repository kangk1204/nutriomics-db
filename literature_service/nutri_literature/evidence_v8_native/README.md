# Native DrugProt·CG v8 보조 실험

이 패키지는 `evidence_v8` 핵심 파일·모델·기본 엔진을 변경하지 않는다. DrugProt과 CG의 원래 주석 레이블을 각각 학습하며 BioRED 유형으로 변환하지 않는다. 생성형 모델 호출은 없다.

## 현재 지원 범위

- DrugProt: 정답 참여자가 표시된 문맥의 native 다중 관계 분류. NONE 후보는 관계 부재 감독에만 사용한다.
- CG: 제공된 주석 trigger–argument 후보의 native 이벤트 유형/역할 분류. 미주석 CG 후보를 NONE으로 만들지 않는다.
- 두 과제 모두 정답 참여자 조건이다. 자동 개체 조건은 별도 어댑터가 없어 **미측정**이다.
- CG의 전체 중첩 이벤트 구성, 참여자 탐지, 공식 전체 이벤트 평가를 수행한 것으로 표시하지 않는다. Arg1 순서를 인과 방향으로 해석하지 않는다.

## 입력과 실행

공식 train 자료에서 이미 감사된 `auxiliary/train.json`과 `manifest.json`을 입력한다. 준비 단계는 CPU만 사용하고 원본을 수정하지 않는다. 추가 보호 목록은 학습을 제외할 PMID·원문 해시만 포함해야 한다. DAPT용 전체 공개 정답 제외 목록에는 native 학습 문헌 자체도 포함되므로 이를 native 감독 학습의 제외 목록으로 사용하지 않는다.

```text
python scripts/evidence_v8_native.py prepare --source <과거auxiliary디렉토리> --protection <source-only heldout 보호목록> --home <별도runtime>
python scripts/evidence_v8_native.py audit --home <별도runtime>
python scripts/evidence_v8_native.py smoke --home <별도runtime>
python scripts/evidence_v8_native.py encode --home <별도runtime>
python scripts/evidence_v8_native.py train-drugprot --home <별도runtime>
python scripts/evidence_v8_native.py train-cg --home <별도runtime>
python scripts/evidence_v8_native.py freeze --home <별도runtime>
```

`EVIDENCE_V8_NATIVE_HOME`과 `EVIDENCE_V8_NATIVE_GPU_UUID`를 사용한다. 핵심 v8 HOME 환경변수는 변경하지 않는다. 같은 GPU에 다른 학습 작업이 있으면 대기한다. 자원 정책의 명시적·검증된 소규모 데스크톱 예외만 허용한다. H200/A6000을 거부하고 GPU 13GiB·RSS 8GiB·별도 GPU 장부를 사용한다.

## 모델과 검증

고정 BiomedBERT의 CLS·두 참여자 구절 표현에 선형 분류기를 학습하는 기준선과, 같은 인코더·native 출력층을 미세조정하는 구성을 비교한다. 선형 C는 0.1/1/10, 학습률은 1e-5/2e-5, 최대 5 epoch·2회 검증 무개선 조기 종료를 사용한다. 문헌 단위 외부 5-fold·내부 3-fold에서 모델과 수락 임계값을 선택한다.

학습 fold에서 양성이 없는 유형은 출력하지 않지만 검증 정답 분모에서 제외하지 않는다. 내부 평가에는 TP/FP/FN·micro P/R/F1·유형별 값과 macro F1을 기록한다. 같은 참여자 쌍의 여러 주석은 native 다중 레이블로 묶고 원래 행 수와 고유 후보 수를 구분한다.

이 입력은 원문의 일부 문맥만 갖고 있으므로 중복 감사 범위는 PMID와 이용 가능한 문맥의 정확 일치다. PMCID·전체 원문·연구 가족에 의한 추가 중복은 미확인이다. 내부 교차검증을 독립 공식 최종 성능으로 표현하지 않는다.

## 추가 시험

공식 참여자 시험 입력은 `data/evaluation-inputs.json`의 source-only `pairs`와 별도의 `evaluation-gold.json`으로 제공한다. `predict`는 정답 레이블이 입력에 있으면 거부한다. `evaluate`는 전체 예측·해시·동결 계약을 확인한 뒤에만 정답을 읽는다. 자동 개체 조건의 위치 대응 어댑터가 없으면 해당 조건은 실행하지 않는다.

핵심 v8에 가중치를 전이하거나 기본 후보 엔진을 바꾸는 작업은 수행하지 않는다. native 과제가 실제 개선됐는지와 전이 효과는 각각 별도로 검증해야 한다.
