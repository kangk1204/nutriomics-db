# 출처를 보존하는 RAM 읽기 복제본

이 기능은 HDD의 임의 읽기 지연을 줄이기 위한 선택적 운영 계층이다. 연구 데이터의 새로운 release나 효과 행렬의 변환본을 만들지 않는다. 영구 디렉터리의 SQLite와 원본 식별자·출처·이용조건은 계속 기준이며, 같은 바이트의 전체 파일을 기존 tmpfs에서 읽는다. RAM 파일은 재부팅 후 없어질 수 있고 공개 데이터 패키지의 필수 파일이 아니다.

## 생성

Linux의 기존 tmpfs에 연구 프로젝트 전용 디렉터리를 새로 만든다. 마운트·전역 캐시·mlock·다른 작업의 메모리 설정을 변경하지 않는다. 계획 JSON의 `files`마다 `source`, `name`, `scope`, `role`, `sha256`을 명시한다. `releases`에는 scope별 현재 release ID를 넣는다. source는 닫힌 SQLite여야 하며 1바이트라도 내용이 있는 WAL은 거부한다. 색인의 별도 JSON 영수증은 `companions`로 지정한다.

```bash
python -m scripts.create_source_replicas --plan replica-plan.json \
  --ram-root /dev/shm/PROJECT-UNIQUE-REPLICAS --receipts permanent/replica-receipts-v1
```

scope/role의 허용 조합은 다음과 같다.

| scope | role |
|---|---|
| internal | internal_release, internal_core, internal_evidence |
| public | public_release, public_evidence, public_omics |

도구는 복사를 한 번에 하나씩 수행한다. 원본 SHA 전후, 복사 스트림 SHA, 복제본의 독립 SHA와 원본 fingerprint가 모두 일치해야 map을 발행한다. 디렉터리는 0700, 복제본은 0400이며, 데이터 파일 전체 상한은 42GiB다. 복사 전·복사 중 가용 메모리 최소 24GiB와 cgroup 한도, tmpfs 여유를 검사한다. 별도 operator 예산은 3,600초이며 HTTP 10초/export 45초 예산을 바꾸지 않는다. 실패 시 map을 발행하지 않고 `.partial`과 영구 실패 영수증을 보존한다. 부분 파일은 자동으로 새 데이터로 승격하지 않는다.

## 실행과 정합성

각 서비스에 자기 scope의 map만 전달한다.

```bash
export NUTRIOMICS_SOURCE_REPLICA_MAP=permanent/replica-receipts-v1/public-map.json
export NUTRIOMICS_SOURCE_REPLICA_SCOPE=public
# 기존 release·원본 DB·보조색인 경로 설정은 그대로 유지한다.
python -m scripts.serve_verified_release --application public \
  --release PUBLIC_RELEASE --receipt startup.json --port 8059
```

공개 map에는 내부 core가 들어갈 수 없다. 내부 map과 공개 map의 합집합을 공개 서비스에 전달하지 않는다. startup은 scope·release·원본/복제본 fingerprint·복제본 전체 SHA를 독립 확인한다. 쿼리와 내보내기의 논리적 경로와 원본 결속은 영구 원본을 유지한다. `read_connection`과 SQLAlchemy의 등록된 원본만 readonly/immutable RAM 읽기로 전환하며, 별도의 변경 가능한 식사 저장 DB에는 적용하지 않는다. 기존 보조색인은 바이트가 동일하므로 원본 경로의 결속을 그대로 검증한다. 과거 색인의 경로를 새 경로로 덮어쓰거나 fingerprint를 다시 표시하지 않는다.

모든 `/api/v1/` 요청과 응답 헤더 전, Explorer의 캐시 hit/singleflight 반환 전, 그리고 새 Explorer 응답 완료 전에 원본·복제본·map·색인 영수증의 존재/fingerprint와 nonempty WAL을 검사한다. 원본이 교체되거나 WAL이 생기면 캐시된 성공을 반환하지 않고 503을 반환한다. 이 오류는 근거 0건을 뜻하지 않는다. 새 원본에는 새 SHA 검증·복사·map·startup이 필요하다. 이미 보낸 응답 헤더/부분 응답을 취소해 원자적으로 되돌린다고 주장하지 않는다.

## 증빙과 종료

`copy.json`은 각 SHA/fingerprint, 원본/복제 경로, 파일별·전체 wall/CPU 시간, 표본 peak RSS, 실제 파일 용량, 최소 가용 메모리, cgroup 및 시작/종료 PSI를 기록한다. startup의 복제본 SHA 시간과 원본 checksum·preview 시간을 HTTP 성능과 구분한다. 이전 HDD 첫 조회·timeout 증거를 대체하지 않는다. 새 환경의 실제 최대 export가 먼저 통과한 뒤 고정 질의의 warm/동시 성능과 전체 JSON 동등성을 다시 검증한다.

검증된 기존 서비스는 새 서비스가 준비될 때까지 유지한다. map이 없거나 RAM 파일이 사라지면 자동으로 오래된 복제본이나 느린 원본에 fallback하지 않는다. 해당 서비스는 readiness를 거부하고 복제본을 재생성해야 한다. 정상 종료·교체 전에는 해당 map을 쓰는 프로세스가 없는지 확인한 뒤, 영구 영수증을 보존하고 정확한 프로젝트 tmpfs 경로의 폐기 파일만 제거한다. 다른 사용자·서비스의 tmpfs 경로는 변경하지 않는다.
