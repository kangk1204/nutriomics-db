# 로컬 접속 유지

`scripts/run_explorer_tunnel.py`는 지정한 서버의 포트 하나를 `127.0.0.1`로 연결한다. SSH가 종료되면 다시 연결한다. 서버의 웹서비스가 내려간 경우에는 `unavailable`로 기록한다. 서버 복구·재부팅이나 전역 SSH/Tailscale 설정 변경은 수행하지 않는다.

실행할 때 SSH 실행 파일, 승인된 서버 주소, 양쪽 포트, 확인된 known-hosts 파일과 SHA-256, 예상 데이터 release를 명시한다. 호스트 키 확인을 끄지 않는다. 이미 사용 중인 포트는 중단하거나 가져오지 않는다.

```text
python -m scripts.run_explorer_tunnel --help
```

실행 중 상태는 지정한 디렉터리의 `status.json`, 접속 오류는 `ssh.log`에 남는다. 같은 디렉터리에 `STOP` 파일을 만들면 해당 세션과 그 세션이 실행한 SSH만 종료한다. 다른 프로세스를 PID만으로 찾아 종료하지 않는다. 오래된 `session.lock`은 자동 삭제하지 않으며, 중복 실행을 막는다.

현재 연구 환경의 실행 인자와 복구 시험은 `outputs/publication/2026-10-23/food-search-ko/`에 보관한다. 이 경로는 공개 데이터셋의 출처나 과학적 검수 자료가 아니다.
