# 심사용 문헌 성과·API 검증

2026-10-24에 README, 완료 성과표, API 안내와 문헌 모델 화면을 함께 대조했다. 새 학습 점수를 추가한 작업이 아니라, 저장된 완료 평가를 재검산하고 평가 조건·버전·사용법을 정리한 작업이다.

| 검증 | 결과 | 범위 |
|---|---|---|
| 관계 존재 예측 재검산 | 4,362쌍 · AUROC 0.8760460794545022 일치 | 정답 개체 제공 224편, 저장 점수에서 직접 재계산 |
| 관계 존재 임계값 검산 | TP 681 / FP 319 / FN 433 / TN 2,929 | 원래 임계값 0.75 |
| 과제별 성능 검산 | 대표 구성 5개 일치 | TP·FP·FN에서 정밀도·재현율·F1 재계산 |
| 자료 규모 | 상태 JSON과 고정 보고서 대조 일치 | 고유 문헌·초록·공개 본문·원문 대응 추출 |
| API 사용 예제 | 42개 요청 통과 | 격리한 합성 DB, 실제 FastAPI 라우트·인증·검수·작업 상태 |
| 관련 회귀 테스트 | 38개 통과 | snapshot·공개 범위·내부 관리·문서 게시·재실행 |
| TypeScript·웹 빌드 | 통과 | production 및 독립 HTML용 runtime 빌드 |
| 브라우저 | 확인 | 대표 성과·지표 해설·접힌 버전 이력·전체 성능표 표시 |
| 문서 링크 | 33개 상대 링크 확인 | README·API 안내·성과표·버전 이력 |

NER은 개체 언급, 임상 결과 구절은 토큰, 전체 자동 관계는 원시 ID와 관계 유형이 일치한 관계가 집계 단위다. 서로 다른 평가의 개수를 더하거나 동일한 정확도 지표로 합치지 않는다. 전체 자동 관계 대표 결과는 100편·154건에서 TP 65 / FP 63 / FN 89이다. AUROC 0.876은 정답 개체를 제공한 관계 존재 분류 결과와 연결한다.

README와 모델 화면의 내부 개발 목표를 제거했다. 기존 snapshot에 저장된 목표도 공개 조회의 성능 응답에서 제외하고, 원래 저장 결과와 학습 정책은 보존한다. 이후 평가 문서 갱신에서도 내부 목표가 다시 생기지 않도록 검사했다.

API 검증은 네트워크 모델 호출과 실제 학습 worker를 시작하지 않는다. 검증용 두 문헌은 합성 자료이며 확보 문헌·과학적 성능에 더하지 않는다. Unicode 원문 위치·해시, 이용조건에 따른 공개 제한, 세션·CSRF·권한, 중복 작업, 취소·재개와 대표 오류 응답을 확인했다.

## 재현

저장소의 `[dev]` 의존성을 설치한 Python 환경에서 실행한다.

```bash
python scripts/build_reviewer_summary.py --check
python scripts/verify_literature_api_guide.py
python -m pytest tests/test_literature_platform.py tests/test_literature_management.py tests/test_v8_result_docs.py -q
pnpm -C frontend/explorer build
```

성과 원자료·해시는 [summary.json](summary.json), 요청별 API 결과는 [api-validation.json](api-validation.json)에 있다. 원자료 TSV와 저장된 결과를 바꾸면 재검산 결과와 생성 문서를 함께 갱신해야 한다.
