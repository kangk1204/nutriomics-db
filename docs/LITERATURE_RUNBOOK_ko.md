# 문헌 플랫폼 실행 안내

저장소 루트에서 실행합니다. 기본 설치와 현재 성능은 [README](../README.md), 기준일별 수치는 [상태 JSON](status/literature-2026-10-24.json)에 있습니다.

## 환경과 초기화

Python 3.12 가상환경에 루트 패키지와 문헌 패키지를 함께 설치합니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]" -e "./literature_service[dev]"
export LITERATURE_HOME="$PWD/literature_service/runtime"
export LITERATURE_CONFIG="$PWD/literature_service/config/topics.json"
nutri-literature init
```

Windows PowerShell에서는 다음과 같이 설정합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]" -e "./literature_service[dev]"
$env:LITERATURE_HOME = "$PWD/literature_service/runtime"
$env:LITERATURE_CONFIG = "$PWD/literature_service/config/topics.json"
nutri-literature init
```

DB는 `$LITERATURE_HOME/data/literature.sqlite3`, 원본 응답·보고서·백업·로그는 같은 home 아래에 생성됩니다. `NCBI_EMAIL`, `NCBI_API_KEY`는 실행 환경에서 설정합니다. 설정 예시는 [환경변수 예시](../literature_service/.env.example)에 있습니다.

## 수집과 처리

```bash
nutri-literature collect
nutri-literature process --stage oa --limit 100
nutri-literature process --stage extract --engine heuristic-v1 --limit 100
nutri-literature report --engine heuristic-v1
```

`--limit`는 한 배치의 작업 수입니다. 연속 처리에는 `worker`를 사용합니다.

```bash
nutri-literature worker --stage oa
nutri-literature worker --stage extract --engine heuristic-v1
```

영양유전체 전반의 확대 검색은 별도 검색 버전과 체크포인트로 진행합니다.

```bash
python literature_service/scripts/platform_expand_v8.py audit
python literature_service/scripts/platform_expand_v8.py collect --continuous
python literature_service/scripts/platform_expand_v8.py reconcile
```

`reconcile`은 검색 PMID와 저장 문헌을 대조합니다. 미수집 문헌이 있으면 종료 코드 2와 대조 결과를 남깁니다. 실패 상태와 본문 공개 상태는 문헌별로 조회합니다.

## 웹 빌드와 snapshot

Node.js 24와 pnpm 11.25.0을 설치한 뒤 실행합니다.

```bash
pnpm -C frontend/explorer install --frozen-lockfile
pnpm -C frontend/explorer build
python -m scripts.build_literature_snapshot \
  --source "$LITERATURE_HOME/data/literature.sqlite3" \
  --output "$LITERATURE_HOME/snapshots/dev-public" \
  --release-id literature-dev-public
python -m scripts.build_literature_snapshot \
  --source "$LITERATURE_HOME/data/literature.sqlite3" \
  --output "$LITERATURE_HOME/snapshots/dev-internal" \
  --release-id literature-dev-internal --internal
```

출력 디렉터리는 snapshot 버전마다 새로 지정합니다. 공개 snapshot은 이용조건에 맞는 문헌 필드를 포함하고 내부 snapshot은 검수용 정보를 포함합니다.

공개 앱은 검증된 기존 NutriOmics release를 입력으로 사용합니다. release 생성 방법은 [공개 운영 안내](PUBLICATION_RUNBOOK_ko.md)에 있습니다.

```bash
export NUTRIOMICS_PUBLIC_RELEASE="/absolute/path/to/release"
export NUTRIOMICS_ALLOW_CANDIDATE=1
export NUTRIOMICS_LITERATURE_SNAPSHOT="$LITERATURE_HOME/snapshots/dev-public"
export NUTRIGENOMICS_EXPLORER_DIST="$PWD/frontend/explorer/dist"
python -m uvicorn nutrigenomics.public_service:app_factory \
  --factory --host 127.0.0.1 --port 8064
```

접속 경로는 `/explorer/`, 문헌 화면은 `/explorer/?view=literature`, 상태 확인은 `/healthz`입니다. 시작 시 release 파일의 무결성을 확인합니다. 대규모 release는 이 검사를 마친 후 요청을 받습니다.

내부 앱은 별도 터미널에서 실행합니다.

```bash
export NUTRIOMICS_INTERNAL_LITERATURE_SNAPSHOT="$LITERATURE_HOME/snapshots/dev-internal"
export NUTRIGENOMICS_EXPLORER_DIST="$PWD/frontend/explorer/dist"
python -m uvicorn nutrigenomics.literature_management:app_factory \
  --factory --host 127.0.0.1 --port 8063
```

공개 조회 API는 `/api/v1/explorer/literature` 아래의 `stats`, `papers`, `papers/{pmid}`, `events`, `benchmarks`, `reports`입니다. 관리 API는 내부 앱의 `/api/v1/literature-admin`에 있습니다. 조회는 저장된 snapshot을 사용합니다.

## 계정과 작업 worker

```bash
nutri-literature create-account reviewer1
nutri-literature create-account operator1
python literature_service/scripts/platform_worker.py --grant operator1 --role operator
python literature_service/scripts/platform_worker.py
```

계정 생성 시 암호를 입력합니다. 기본 역할은 `reviewer`이며 검수를 수행합니다. `operator`는 허용된 수집·학습·평가·보고서 작업을 등록할 수 있습니다. 로그인 세션은 8시간, HttpOnly 쿠키와 CSRF 토큰을 사용합니다. 작업 ID로 진행·재개·취소 상태를 조회합니다.

서버에서는 `supervisord`로 공개 앱·내부 앱·worker·확대 수집 프로세스를 관리합니다. [배포 스크립트](../scripts/deploy_literature_integration.py)는 버전별 소스와 실행 설정을 준비합니다. 접속 포워딩은 [로컬 접속 안내](LOCAL_TUNNEL_ko.md)를 참고하세요.

## v8 학습 환경과 단계

학습 환경은 웹 환경과 분리합니다. GPU에 맞는 PyTorch 빌드를 설치하고 고정 의존성을 적용합니다.

```bash
python3.12 -m venv .venv-v8
source .venv-v8/bin/activate
python -m pip install -r literature_service/requirements-evidence-v8.txt
python literature_service/scripts/bootstrap_evidence_v8.py \
  --source-root /absolute/path/to/audited-r2-inputs \
  --output /absolute/path/to/v8-experiment --resolve-models
```

입력은 허용된 학습·개발 원문, 보호 목록, 공식 별칭 DB, 지식 manifest, 준비된 후보를 포함하는 감사 완료 r2 입력 snapshot입니다. DB·가중치·원문은 별도 버전 저장소에서 가져옵니다. 정확한 입력 계약은 [v8 데이터 구현](../literature_service/nutri_literature/evidence_v8/data.py)과 [구현 기록](../literature_service/docs/implementation-v8.md)에 있습니다.

```bash
python literature_service/scripts/evidence_v8.py audit --home /absolute/path/to/v8-experiment
python literature_service/scripts/evidence_v8.py probe --home /absolute/path/to/v8-experiment
python literature_service/scripts/evidence_v8.py smoke-ner --home /absolute/path/to/v8-experiment
python literature_service/scripts/evidence_v8.py prepare --home /absolute/path/to/v8-experiment
python literature_service/scripts/evidence_v8.py train-ner --home /absolute/path/to/v8-experiment
python literature_service/scripts/evidence_v8.py train-id --home /absolute/path/to/v8-experiment
python literature_service/scripts/evidence_v8.py train-relations --home /absolute/path/to/v8-experiment
python literature_service/scripts/evidence_v8.py status --home /absolute/path/to/v8-experiment
```

고정 소스 manifest·입력 manifest·후보 생성 설정·fold·가중치를 실행별로 기록합니다.

학습 설정을 선택한 뒤 `freeze`, `predict`, `verify-export`를 실행합니다. 저장 예측은 `evaluate-saved`로 채점합니다. 서버 학습부터 로컬 채점·보고까지 연결하려면 [finish_evidence_v8.py](../scripts/finish_evidence_v8.py)를 사용합니다. 인자는 각 CLI의 `--help`로 확인할 수 있습니다.

동일 비교는 개발 100편·정답 관계 154건의 원시 ID와 관계 유형 일치 기준으로 수행합니다. 과거 예측·평가는 별도 버전으로 저장합니다. `state.json`, `gpu-ledger.json`, 체크포인트, 예측 manifest, 평가 출력으로 진행 상황을 추적합니다.

## 검증과 보고서

구버전 v2~v5 구문·역할 검사는 `requirements-v5.lock`의 spaCy·scispaCy·구문 모델·PyTorch 환경을 사용합니다. 웹 실행 환경과 별도 가상환경에서 검사합니다.

```bash
python -m pytest tests/test_literature_platform.py tests/test_literature_management.py
python -m pytest tests/test_v8_streaming_json.py tests/test_v8_id_lifecycle.py
python -m pytest tests/test_evidence_v8_contract.py tests/test_evidence_v8_native.py
pnpm -C frontend/explorer typecheck
pnpm -C frontend/explorer build
```

관계 보고서는 평가 문헌·정답 수, TP/FP/FN, 정밀도·재현율·F1, ID 변경, 후보 손실, 기존 정탐 유지·상실·신규 복원을 포함합니다. 수집 보고서는 검색·저장·초록·본문·추출·검수 건수를 각각 집계합니다. 현재 학습 상태와 미완료 단계는 README와 날짜별 상태 JSON으로 갱신합니다.

이번 커밋의 검사 범위와 실행 환경은 [검증 기록](status/validation-2026-10-24.md)에 정리했습니다.
