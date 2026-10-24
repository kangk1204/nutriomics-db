# SQLite 읽기 캐시 운영

메모리에 남는 응답과 SQLite 페이지 캐시는 다릅니다. 이 모듈은 검증된 읽기 전용 DB의 페이지를 짧은 연결 사이에 유지합니다. 원본 파일이나 효과 행렬을 복사하지 않습니다.

서버 준비 단계에서 원본 SHA-256과 닫힌 파일의 inode·크기·mtime·ctime·WAL 상태를 확인한 뒤 `sqlite_read_cache.initialize`를 호출합니다. `verified_fingerprints`는 그 검증에 연결된 값이어야 합니다. 사용 예시는 다음과 같습니다.

```python
from nutrigenomics.sqlite_read_cache import initialize
initialize({database_path: 1792, index_path: 256}, verified_fingerprints)
```

전체 한도는 2,048 MiB이며, 사용 가능한 메모리 24 GiB를 남겨야 합니다. 실제 할당량은 읽은 페이지에 따라 늘어납니다. 숫자는 데이터베이스별 MiB 한도입니다. 연결마다 원래 읽기 전용 URI와 `cache=shared`를 사용하고, keeper 연결은 서비스 종료까지 유지합니다. 전역 SQLite 설정·mount·quota·다른 서비스는 변경하지 않습니다.

원본 또는 WAL이 바뀌거나 없어지면 HTTP 응답 캐시를 읽기 전에도 503을 반환합니다. 기존 값이나 0건으로 대체하지 않습니다. 메모리 캐시가 source manifest 검증이나 과학적 검수를 대신하지 않습니다. 초기 파일 읽기와 서비스 준비 시간은 warm HTTP 성능과 별도로 보고합니다.

`initialize`를 호출하지 않으면 기존 연결 방식이 유지됩니다. 별도의 물리적 복제본과 동시에 사용할 수 없습니다. Windows에서는 열려 있는 SQLite 파일의 삭제가 OS에서 거부될 수 있어, 실제 삭제 후 차단 검사는 Linux에서 수행합니다.

<details><summary>English</summary>
The opt-in shared page cache retains pages across short read-only connections, with a 2,048 MiB total limit and a 24 GiB host-memory reserve. Startup must first verify the source hash and exact closed-source fingerprint. Source/WAL changes invalidate cached responses. No source data, global configuration or filesystem quota is modified. Initialization and cold I/O are not warm-request latency.
</details>
