# 설치 방법

[프로젝트 소개](../README.md) · [English](GETTING_STARTED.md) · [검토 안내](REVIEWER_GUIDE.ko.md)

## 전체 데이터 없이 코드 확인하기

Git, Python 3.12와 [uv](https://docs.astral.sh/uv/getting-started/installation/)를 설치합니다.
터미널에서 다음 명령을 순서대로 실행합니다.

```sh
git clone https://github.com/kangk1204/nutriomics-db.git
cd nutriomics-db
uv sync --frozen --extra dev
uv run pytest tests/test_composition_policy.py tests/test_public_service_isolation.py -q
```

이 검사는 작은 시험 자료로 성분 계산 규칙과 읽기 전용 서비스의 동작을 확인합니다.
전체 식품 DB를 다운로드하거나 오믹스 분석을 다시 실행하지 않습니다.
Python 검사 전체를 실행하려면 `uv run pytest -q`를 사용합니다.

## 웹 화면 빌드하기

Node.js 24와 `frontend/explorer/package.json`에 명시된 버전의 pnpm이 필요합니다.

```sh
cd frontend/explorer
pnpm install --frozen-lockfile
pnpm build
cd ../..
```

화면 파일은 `frontend/explorer/dist`에 생성됩니다.
이 과정은 화면을 빌드하며, DB를 내려받거나 채우지는 않습니다.

## 준비된 데이터를 내 컴퓨터에서 열기

이 단계에는 별도로 준비한 데이터 폴더가 필요합니다. 폴더에는 `dataset-metadata.json`,
`manifest.json`, `nutriomics.sqlite3`와 파일 목록에 기재된 자료가 있어야 합니다.
전체 데이터 다운로드는 아직 공개하지 않았습니다.

Windows PowerShell에서 다음과 같이 실행합니다. 데이터 경로는 실제 폴더로 바꿉니다.

```powershell
$env:NUTRIOMICS_PUBLIC_RELEASE='C:\data\prepared-release'
$env:NUTRIOMICS_ALLOW_CANDIDATE='1'
uv run python -m uvicorn nutrigenomics.public_service:app_factory --factory --host 127.0.0.1 --port 8000
```

Linux 또는 macOS에서는 다음과 같이 실행합니다.

```sh
export NUTRIOMICS_PUBLIC_RELEASE="/path/to/prepared-release"
export NUTRIOMICS_ALLOW_CANDIDATE=1
uv run python -m uvicorn nutrigenomics.public_service:app_factory --factory --host 127.0.0.1 --port 8000
```

브라우저에서 <http://127.0.0.1:8000/explorer/>를 열면 화면을,
<http://127.0.0.1:8000/docs>를 열면 API 설명을 볼 수 있습니다.
위 설정은 검토 중인 데이터를 내 컴퓨터에서 열기 위한 옵션입니다.
과학적 검수나 외부 공개의 완료를 뜻하지 않습니다.

## 자료 수집·분석·갱신하기

| 작업 | 안내 |
|---|---|
| 원본 수집과 재분석 | [재구축 방법](WORKBENCH_PUBLIC_REBUILD_ko.md) |
| 표·결과 값의 정의 확인 | [자료 사전](PUBLIC_DATA_DICTIONARY.md) |
| 검수 반영, 새 버전 생성과 복원 | [운영 방법](PUBLICATION_RUNBOOK_ko.md) |
| 원본 변경 확인 | [갱신 방법](WORKBENCH_UPDATE_ko.md) |

R 분석에는 각 분석에 기록된 R/Bioconductor 환경과 입력 자료가 필요합니다.
Python 설치만으로 R 환경이나 전체 연구 분석이 재현되지는 않습니다.
