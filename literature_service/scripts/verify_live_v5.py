"""Read-only API and Streamlit checks against the actual deployed service."""

import json
from pathlib import Path
import time

import requests
from streamlit.testing.v1 import AppTest

from nutri_literature.db import now


def main():
    root = Path("/home/keunsoo/projects/nutriomics-literature")
    base = "http://127.0.0.1:8010"
    record = {"at": now(), "kind": "v5-live-read-only-validation", "endpoints": {}, "ui": {}}
    health = requests.get(base + "/healthz", timeout=20).json()
    assert health["engine"] == "heuristic-v1" and "heuristic-v5" in health["engines"]
    record["health"] = health
    for endpoint in (
        "papers",
        "events",
        "evidence-map",
        "experiments",
        "experiments/diagnostics",
        "runs",
        "reports",
        "soak-status",
    ):
        started = time.perf_counter()
        params = {"engine": "heuristic-v5"}
        if endpoint == "reports":
            params["include_snapshot"] = False
        response = requests.get(base + "/api/v1/" + endpoint, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        record["endpoints"][endpoint] = {
            "status": response.status_code,
            "seconds": round(time.perf_counter() - started, 3),
            "keys": list(data) if isinstance(data, dict) else None,
        }
    app = AppTest.from_file(str(root / "ui/app.py"), default_timeout=40).run()
    assert not app.exception
    next(w for w in app.selectbox if w.label == "추출 엔진").select("heuristic-v5").run()
    assert not app.exception
    for page in (
        "문헌 검색",
        "논문별 요약",
        "Evidence Map",
        "검수 대기열",
        "알고리즘 개선",
        "실행 상태·리포트",
    ):
        next(w for w in app.radio if w.label == "작업").set_value(page).run()
        assert not app.exception, page
        if page == "논문별 요약" and any(w.label == "문헌 열기" for w in app.button):
            next(w for w in app.button if w.label == "문헌 열기").click().run()
            assert not app.exception
        record["ui"][page] = "passed"
    record["passed"] = True
    out = root / "runtime/experiments/v5/live-validation.json"
    with out.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
