# 문헌 API 사용 안내

API는 웹 화면에서 사용하는 문헌·요약·관계 후보를 다른 프로그램에서 조회하는 방법입니다. **서버 실행 → 연결 확인 → 문헌 검색 → 상세·관계 조회 → 보고서 저장** 순서로 시작하면 됩니다. Python, PowerShell, curl 중 편한 도구 하나만 사용해도 됩니다.

## 1. 어떤 주소를 쓰나요?

| 서비스 | 기본 주소 | 사용 대상 |
|---|---|---|
| 공개 문헌 조회 | `http://127.0.0.1:8064/api/v1/explorer/literature` | 로그인 없이 공개 가능한 snapshot 조회 |
| 내부 문헌 조회 | `http://127.0.0.1:8063/api/v1/explorer/literature` | 개인 계정 로그인 후 검수용 snapshot 조회 |
| 내부 관리 | `http://127.0.0.1:8063/api/v1/literature-admin` | 로그인·검수·작업 등록 |

`127.0.0.1`은 지금 명령을 실행하는 컴퓨터입니다. 서버가 다른 컴퓨터에 있으면 [SSH 포워딩](LOCAL_TUNNEL_ko.md)을 먼저 연결하고 포워딩한 포트를 사용하세요. 코드만 내려받은 새 컴퓨터에는 DB가 없으므로, [실행 안내](LITERATURE_RUNBOOK_ko.md)의 환경·DB 초기화와 snapshot 연결을 먼저 수행합니다.

공개 서버는 다음과 같이 실행합니다. 경로 세 개는 실제 준비한 release·snapshot·웹 빌드 경로로 바꿉니다.

```bash
export NUTRIOMICS_PUBLIC_RELEASE="/absolute/path/to/release"
export NUTRIOMICS_ALLOW_CANDIDATE=1
export NUTRIOMICS_LITERATURE_SNAPSHOT="/absolute/path/to/public-literature-snapshot"
export NUTRIGENOMICS_EXPLORER_DIST="$PWD/frontend/explorer/dist"
python -m uvicorn nutrigenomics.public_service:app_factory --factory --host 127.0.0.1 --port 8064
```

PowerShell에서는 환경변수를 다음처럼 설정합니다.

```powershell
$env:NUTRIOMICS_PUBLIC_RELEASE = 'C:/data/nutriomics/release'
$env:NUTRIOMICS_ALLOW_CANDIDATE = '1'
$env:NUTRIOMICS_LITERATURE_SNAPSHOT = 'C:/data/nutriomics/public-literature-snapshot'
$env:NUTRIGENOMICS_EXPLORER_DIST = "$PWD/frontend/explorer/dist"
python -m uvicorn nutrigenomics.public_service:app_factory --factory --host 127.0.0.1 --port 8064
```

위의 candidate 설정은 내부 연구용 release를 열어 보는 설정입니다. 공개 배포 설정은 [운영 안내](PUBLICATION_RUNBOOK_ko.md)를 따릅니다.

## 2. 브라우저에서 먼저 실행해 보기

1. [서버 상태](http://127.0.0.1:8064/healthz)를 열어 `status: "ok"`를 확인합니다.
2. [문헌 연결 상태](http://127.0.0.1:8064/api/v1/explorer/literature/stats)를 열어 `status: "available"`과 `release_id`를 확인합니다.
3. [대화형 API 설명서](http://127.0.0.1:8064/docs)에서 **Literature → GET /papers → Try it out → Execute**를 누릅니다.
4. `page_size`를 `5`로 입력하면 문헌 다섯 편을 확인할 수 있습니다.

`/healthz`는 서버가 실행 중이라는 뜻이고, 문헌 자료가 연결됐는지는 `/stats`로 확인합니다. `status: "not_connected"`이면 문헌 snapshot 환경변수를 설정하고 서버를 다시 시작합니다. API 정의 전체는 `/openapi.json`에서도 읽을 수 있습니다.

## 3. 첫 문헌의 요약과 관계 읽기

### PowerShell

아래 예제는 DB에 실제로 있는 첫 PMID를 사용합니다.

```powershell
$api = 'http://127.0.0.1:8064/api/v1/explorer/literature'
$stats = Invoke-RestMethod "$api/stats"
$stats | Select-Object status, release_id, generated_at
$stats.counts

$result = Invoke-RestMethod "$api/papers?page=1&page_size=5"
$result.items | Select-Object pmid, title, pub_date, available_engines
if ($result.items.Count -gt 0) {
    $pmid = $result.items[0].pmid
    $paper = Invoke-RestMethod "$api/papers/${pmid}?engine=heuristic-v1"
    $paper.summary | ConvertTo-Json -Depth 15
    $events = Invoke-RestMethod "$api/events?pmid=$pmid&engine=heuristic-v1&page_size=50"
    $events.items | Select-Object subject, predicate, object, quote, status
}
```

### Python

```bash
python -m pip install requests
```

```python
import requests

api = "http://127.0.0.1:8064/api/v1/explorer/literature"

def get(path, **params):
    response = requests.get(api + path, params=params, timeout=30)
    response.raise_for_status()
    return response.json()

stats = get("/stats")
print("연결:", stats["status"], "자료 버전:", stats.get("release_id"))
result = get("/papers", page=1, page_size=5, engine="heuristic-v1")
print("전체 검색 결과:", result["total"])
for card in result["items"]:
    print(card["pmid"], card["title"])

if result["items"]:
    pmid = result["items"][0]["pmid"]
    paper = get(f"/papers/{pmid}", engine="heuristic-v1")
    print("저장된 엔진:", paper["available_engines"])
    print("요약:", paper.get("summary"))
    events = get("/events", pmid=pmid, engine="heuristic-v1")
    for event in events["items"]:
        print(event["subject"], event["predicate"], event["object"])
        print("원문:", event["quote"], "위치:", event["start"], event["end"])
```

### curl

```bash
curl --fail-with-body "http://127.0.0.1:8064/api/v1/explorer/literature/papers?page=1&page_size=5"
curl --fail-with-body --get "http://127.0.0.1:8064/api/v1/explorer/literature/papers" \
  --data-urlencode "q=vitamin D" --data-urlencode "year_from=2020" \
  --data-urlencode "engine=heuristic-v1" --data-urlencode "page_size=10"
```

Windows에서 curl을 사용할 때는 `curl.exe`로 실행하면 PowerShell 별칭과 구분할 수 있습니다.

## 4. 문헌 검색 조건

`GET /papers`는 다음 조건을 조합합니다. Python의 `params`, PowerShell의 URI 인코딩 또는 curl의 `--data-urlencode`를 쓰면 공백·그리스 문자·기호가 있는 검색어도 안전하게 전달됩니다.

| 매개변수 | 기본값·범위 | 의미 |
|---|---|---|
| `q` | 빈 문자열, 최대 240자 | 제목·공개 초록의 단어 검색. 최대 12개 단어를 AND로 검색 |
| `topic` | 생략 | 주제 ID. 연결된 `/stats`의 `collection.topics`에서 확인 |
| `year_from`, `year_to` | `2010`, `2100`; 각각 1500–2100 | 출판 연도 범위. 연도 미상 문헌도 포함 |
| `engine` | `heuristic-v1` | 조회할 저장 추출 엔진 |
| `fulltext` | 생략 | `true`: 본문 확보, `false`: 미확보, 생략: 전체 |
| `gene`, `chemical` | 생략, 최대 120자 | 해당 엔진 관계의 주체·대상 ID와 정확히 일치하는 문헌 |
| `species` | 생략, 최대 80자 | 저장된 종 문자열과 정확히 일치 |
| `corpus`, `query_version` | 생략, 최대 100자 | 수집 코퍼스와 검색식 버전. `/stats`의 `corpora`에서 확인 |
| `processing_status` | 생략 | `completed`, `pending`, `running`, `waiting`, `failed`, `cancelled`, `not_processed` |
| `page` | `1` 이상 | 페이지 번호 |
| `page_size` | 기본 50, 최대 200 | 한 페이지의 문헌 수 |

기본 주제 ID는 `HTN`, `DASH`, `omega3`, `vitD`, `caffeine`, `cocoa_flavanol`입니다. 종과 개체 ID는 실제 응답에서 가져와 사용하세요. `gene=VDR`처럼 이름을 넣는 것과 표준 ID를 넣는 것은 같은 검색이 아닙니다. 이름 검색은 우선 `q=VDR`을 사용하고, 정확한 관계 검색은 `/events`에서 확인한 ID를 사용합니다.

```python
result = get("/papers", q="blood pressure", year_from=2020, year_to=2026,
             fulltext=True, page=1, page_size=50)
print(result["filters"], result["total"])
```

결과의 `total`은 검색 조건에 맞는 전체 문헌 수, `items`는 현재 페이지입니다. 주요 응답 항목은 다음과 같습니다.

| 필드 | 읽는 방법 |
|---|---|
| `release_id` | 현재 연결된 자료 snapshot의 버전 |
| `pmid`, `doi`, `pmcid` | 문헌 식별자 |
| `title`, `pub_date`, `topics` | 제목·출판일·주제 |
| `available_engines` | 해당 문헌에 실제 저장된 엔진 목록 |
| `summary` | 선택한 엔진의 저장 요약. 결과가 없거나 공개가 제한되면 `null` |
| `metadata.source_url` | 원 출처 링크 |
| `metadata.license`, `rights_status` | 원문 이용조건과 공개 상태 |
| `source_sha256` | 저장 원문의 해시 |

목록 응답은 긴 본문을 포함하지 않으며, 초록은 `abstract_preview`로 표시될 수 있습니다. 상세 API는 `abstract`, `source_text`, `corpora`, `processing_status`를 제공합니다. 공개 조건에 따라 원문·인용·요약이 비어 있어도 서지정보는 조회할 수 있습니다. 존재하지 않는 엔진을 지정하면 오류 대신 요약이 `null`일 수 있으므로 `available_engines`를 먼저 확인합니다.

## 5. 여러 페이지를 CSV로 저장하기

같은 서버 snapshot을 유지하면서 페이지를 순서대로 읽습니다. 이 예제는 문헌 목록만 저장하며, 모든 본문을 한 번에 요청하지 않습니다.

```python
import csv

page = 1
release_id = None
with open("papers.csv", "w", encoding="utf-8-sig", newline="") as output:
    writer = csv.DictWriter(output, fieldnames=["pmid", "title", "pub_date", "oa_status"])
    writer.writeheader()
    while True:
        batch = get("/papers", year_from=2010, page=page, page_size=200)
        release_id = release_id or batch["release_id"]
        if batch["release_id"] != release_id:
            raise RuntimeError("조회 중 자료 버전이 바뀌었습니다. 새 파일로 다시 시작하세요.")
        for paper in batch["items"]:
            writer.writerow({key: paper.get(key) for key in writer.fieldnames})
        if page * batch["page_size"] >= batch["total"] or not batch["items"]:
            break
        page += 1
print("저장 완료:", release_id)
```

## 6. 관계와 원문 위치 확인

`GET /events`의 조건은 `pmid`, `engine`, `status`, `entity`, `corpus`, `query_version`, `page`, `page_size`입니다. `entity`는 관계의 주체 또는 대상 ID와 정확히 일치합니다. 목록과 동일하게 기본 50건·최대 200건입니다.

`event_id`는 후보 버전의 식별자입니다. `subject`, `predicate`, `object`는 주체·관계·대상이고, `status`는 검수 상태입니다. `payload`에는 원본 후보, 교정값, 규칙과 승인 정보가 담깁니다. 자동 관계와 사람 승인은 구분해 읽습니다.

`start`는 0부터 시작하는 문자 위치이고 `end`는 포함하지 않는 끝 위치입니다. UTF-8 바이트 수가 아닙니다. Python에서는 같은 `source_text`에 대해 다음과 같이 확인합니다.

```python
events = get("/events", pmid=pmid, engine="heuristic-v1", page_size=200)
paper = get(f"/papers/{pmid}", engine="heuristic-v1")
for event in events["items"]:
    text = paper.get("source_text")
    if text is not None and paper["source_sha256"] == event["source_sha256"]:
        assert text[event["start"]:event["end"]] == event["quote"]
```

JavaScript 문자열의 위치는 UTF-16 단위이므로 이모지 등 보조평면 문자가 있는 원문은 `Array.from(source_text).slice(start, end).join('')`으로 확인합니다. 공개 제한 원문은 내부 계정으로 해당 내부 snapshot에서 확인합니다.

## 7. 성능표와 보고서 다운로드

`GET /benchmarks`는 해당 snapshot에 저장된 평가만 반환합니다. `items[].metrics`의 정밀도·재현율·F1은 0–1 값이며 `0.5078125`는 50.78%입니다. `tp`, `fp`, `fn`과 문헌·정답·예측 건수를 함께 확인합니다. 평가가 완료되지 않은 엔진은 성능 행이 없을 수 있습니다.

```powershell
$benchmarks = Invoke-RestMethod "$api/benchmarks"
$benchmarks.items | Select-Object arm, papers, gold_relations, predicted_relations, metrics
Invoke-WebRequest "$api/reports/current?format=html" -OutFile 'literature-report.html'
Invoke-WebRequest "$api/reports/current?format=csv" -OutFile 'model-metrics.csv'
Invoke-WebRequest "$api/reports/current?format=csv&table=inventory" -OutFile 'collection-counts.csv'
Invoke-WebRequest "$api/reports/current?format=json" -OutFile 'literature-report.json'
```

형식은 `html`, `md`, `csv`, `json`입니다. CSV의 `table=metrics`는 성능, `table=inventory`는 수집·처리 집계입니다. `GET /reports`에서는 현재 snapshot 보고서의 형식과 생성 시각을 확인할 수 있습니다. API 보고서는 **실행 서버의 snapshot**을 사용하고, 저장소의 [심사용 고정 성과표](reviewer/RESULTS.md)는 **2026-10-24에 고정한 완료 평가**를 사용합니다.

## 8. 내부 로그인·검수·작업 관리

공개 조회에는 개인 계정이나 API 키가 필요하지 않습니다. 내부 관리는 로컬 검수 계정을 사용합니다. 이 로그인은 ChatGPT 계정 로그인과 별개입니다.

### 내부 서버와 계정 준비

```bash
export LITERATURE_HOME="/absolute/path/to/literature-runtime"
export LITERATURE_CONFIG="$PWD/literature_service/config/topics.json"
nutri-literature create-account reviewer1
nutri-literature create-account operator1
python literature_service/scripts/platform_worker.py --grant operator1 --role operator
export NUTRIOMICS_INTERNAL_LITERATURE_SNAPSHOT="/absolute/path/to/internal-literature-snapshot"
python -m uvicorn nutrigenomics.literature_management:app_factory --factory --host 127.0.0.1 --port 8063
```

계정 생성 시 암호를 입력합니다. 별도 터미널에서 같은 `LITERATURE_HOME`·`LITERATURE_CONFIG`로 `python literature_service/scripts/platform_worker.py`를 실행하면 등록된 작업을 처리합니다.

### 로그인과 조회

```python
import getpass
import requests
import uuid

root = "http://127.0.0.1:8063"
admin = root + "/api/v1/literature-admin"
session = requests.Session()
response = session.post(admin + "/session", json={
    "username": input("계정: "), "password": getpass.getpass("암호: ")}, timeout=30)
response.raise_for_status()
csrf = response.json()["csrf"]
headers = {"X-CSRF-Token": csrf}
response = session.get(admin + "/session", timeout=30)
response.raise_for_status()
print(response.json())
```

서버는 8시간 세션 쿠키를 발급합니다. `requests.Session()`이 쿠키를 유지하며, 상태 변경 요청에는 로그인 응답의 `csrf`를 `X-CSRF-Token` 헤더로 보냅니다. Bearer 토큰으로 바꿔 보내지 않습니다.

### 검수 저장

내부 `/events`에서 읽은 실제 `event_id`와 `source_sha256`를 사용합니다. 검수자가 원문을 직접 확인한 뒤 호출합니다. 아래 `event`는 직접 확인한 후보 객체입니다.

```python
response = session.post(admin + "/reviews", headers=headers, json={
    "event_id": event["event_id"],
    "source_sha256": event["source_sha256"],
    "decision": "unconfirmed",
    "comment": "원문을 확인했으며 비교군 정보의 추가 검토가 필요함.",
    "human_confirmation": True,
}, timeout=30)
response.raise_for_status()
print(response.json())
```

판정은 `approved`(승인), `corrected`(교정), `rejected`(기각), `unconfirmed`(미확인)입니다. 교정 시 `corrected_payload`를 추가하며 해당 엔진의 의미 필드·원문·조건 검사를 거칩니다. 원문이나 다른 사람의 검수 버전이 바뀌면 `409`가 반환되므로 새 내부 snapshot을 연결한 뒤 후보를 다시 확인합니다. 검수 저장 후 조회용 snapshot을 갱신해야 공개·내부 조회 결과에 반영됩니다.

### 작업 등록과 상태 조회

`reviewer`는 검수, `operator`는 검수와 작업 등록을 수행합니다. 다음 예제는 보고서 작업을 대기열에 등록합니다.

```python
response = session.post(admin + "/jobs", headers={
    **headers, "Idempotency-Key": str(uuid.uuid4())},
    json={"kind": "build-report", "params": {}}, timeout=30)
response.raise_for_status()
job = response.json()
print(job["job_id"], job["status"])  # 처음에는 queued

response = session.get(admin + "/jobs", timeout=30)
response.raise_for_status()
for row in response.json()["items"]:
    print(row["job_id"], row["kind"], row["status"], row.get("progress"))
```

작업 종류는 `collect`, `reprocess`, `train-id`, `train-relation`, `evaluate-development`, `build-report`, `refresh-snapshot`입니다. `params`는 `engine`, `topic`만 받으며 현재 허용 엔진은 `heuristic-v1`, `relation-repair-v6-gpu-r2`입니다. v8 학습은 [별도 학습 CLI](LITERATURE_RUNBOOK_ko.md)를 사용합니다. 학습 API는 기존 GPU 작업·접속·준비된 평가 산출물에 따라 `waiting`이 될 수 있습니다.

동일한 논리 작업을 재전송할 때는 처음 사용한 `Idempotency-Key`를 그대로 사용합니다. 다른 작업에는 새 키를 사용합니다. 요청은 `202`와 작업 ID를 즉시 반환하며, worker가 별도로 실행합니다. `/jobs`는 최근 100개 작업을 반환하며 개별 작업은 ID로 찾습니다.

```python
job_id = job["job_id"]
# 취소할 때만 실행. 실행 중 작업은 체크포인트를 보존하며 중단합니다.
response = session.post(f"{admin}/jobs/{job_id}/cancel", headers=headers, timeout=30)
response.raise_for_status()
# failed / waiting / cancelled 작업을 다시 시작할 때 사용합니다.
response = session.post(f"{admin}/jobs/{job_id}/resume", headers=headers, timeout=30)
response.raise_for_status()
# 로그아웃
response = session.delete(admin + "/session", headers=headers, timeout=30)
response.raise_for_status()
```

## 9. 자주 만나는 응답과 해결 방법

| 상황 | 원인·해결 |
|---|---|
| 연결 거부·시간 초과 | 서버 프로세스, 포트, SSH 포워딩과 `/healthz` 확인 |
| `/stats`의 `not_connected` 또는 `503` | 문헌 snapshot 경로를 설정한 뒤 재시작. 공개·내부 snapshot은 구분해서 연결 |
| `401` | 내부 계정 로그인 또는 세션 만료. 다시 로그인하고 쿠키 유지 |
| `403` | 운영자 권한, CSRF 헤더, 브라우저의 동일 origin 확인 |
| `404` | 해당 snapshot에 PMID·후보·작업이 없거나 공개 앱에 관리 API를 요청함 |
| `409` | 검수 원문·버전 충돌, 같은 작업 키의 입력 변경, 재개 불가 상태 등을 `detail`에서 확인 |
| `422` | 연도 역전, page_size 200 초과, 잘못된 처리 상태·형식·검수 입력 확인 |
| `429` | 최근 15분 로그인 실패 8회 이상. 제한이 풀린 뒤 계정·암호를 확인 |
| 문헌은 있지만 `summary: null` | 저장 엔진과 공개 조건 확인. 본문 확보와 추출 완료는 별도 상태 |
| 작업이 계속 `queued` | 별도 worker 실행과 동일 `LITERATURE_HOME` 설정 확인 |
| 작업이 `waiting` | `/jobs`의 `error`·`progress`에서 자원·기존 작업·입력 준비 상태 확인 |

요청 예제와 실제 라우트·인증·원문 위치 검증은 [API 검증 기록](reviewer/api-validation.json)에 기록합니다. 소스 기준은 [공개 조회](../nutrigenomics/literature_api.py)와 [내부 관리](../nutrigenomics/literature_management.py)입니다.
