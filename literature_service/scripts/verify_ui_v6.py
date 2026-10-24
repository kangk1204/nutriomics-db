"""Render v6 readonly Streamlit flows using an owned short-lived loopback API."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    import requests
    from streamlit.testing.v1 import AppTest

    url = "http://127.0.0.1:8016"
    try:
        requests.get(url + "/healthz", timeout=1)
    except requests.RequestException:
        pass
    else:
        raise ValueError("Preview port is already in use; no existing service is changed")
    env = {
        **os.environ,
        "LITERATURE_HOME": str(ROOT / "runtime"),
        "LITERATURE_API_URL": url,
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }
    log = ROOT / "runtime/v6/validation/ui-api.log"
    with log.open("ab") as stream:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "nutri_literature.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8016",
            ],
            cwd=ROOT,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if requests.get(url + "/healthz", timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.2)
        else:
            raise RuntimeError("Owned preview API did not become healthy")
        os.environ["LITERATURE_API_URL"] = url
        app = AppTest.from_file(str(ROOT / "ui/app.py"), default_timeout=45).run()
        next(s for s in app.selectbox if s.label == "추출 엔진").set_value("heuristic-v6").run()
        results = []
        for flow in (
            "문헌 검색",
            "논문별 요약",
            "Evidence Map",
            "검수 대기열",
            "실행 상태·리포트",
            "알고리즘 개선",
        ):
            next(s for s in app.radio if s.label == "작업").set_value(flow).run(timeout=45)
            if app.exception or app.error:
                raise RuntimeError(
                    "V6 readonly UI failed in " + flow + ": " + str([e.value for e in app.error])
                )
            results.append({"flow": flow, "exceptions": len(app.exception), "errors": len(app.error)})
        result = {
            "passed": True,
            "engine": "heuristic-v6",
            "flows": results,
            "readonly": True,
            "render_method": "Streamlit AppTest; pixel appearance not audited",
            "actual_v6_predictions_present": False,
        }
        (ROOT / "runtime/v6/validation/ui-smoke.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


if __name__ == "__main__":
    main()
