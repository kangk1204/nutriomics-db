"""Render the five Streamlit flows against a running local API without writes.

Run on the Linux host with its production venv:
    .venv/bin/python scripts/ui_smoke.py

AppTest opens an independent UI session. It never logs in, reviews evidence,
starts jobs, or changes the running services or the production database.
"""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import requests
from streamlit.testing.v1 import AppTest


def checked(app, flow):
    if app.exception:
        # Exception text is implementation detail; no session or credential data is emitted.
        names = [type(exception).__name__ for exception in app.exception]
        raise RuntimeError(f"UI exception in {flow}: {','.join(names)}")
    errors = [element.value for element in app.error]
    if errors:
        raise RuntimeError(f"UI error in {flow}: {errors}")
    return {
        "flow": flow,
        "title": [element.value for element in app.title],
        "subheaders": [element.value for element in app.subheader],
        "metrics": [{"label": metric.label, "value": metric.value} for metric in app.metric],
        "dataframes": len(app.dataframe),
        "plotly_charts": len(app.get("plotly_chart")),
        "exceptions": 0,
    }


def run(api_url, app_path):
    parsed = urlparse(api_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Smoke tests require an HTTP loopback API.")
    health = requests.get(api_url + "/healthz", timeout=10)
    health.raise_for_status()
    if health.json().get("status") != "ok":
        raise RuntimeError("API health check did not pass.")
    os.environ["LITERATURE_API_URL"] = api_url
    app = AppTest.from_file(str(app_path), default_timeout=60).run(timeout=60)
    results = [checked(app, "문헌 검색")]
    paper_list = requests.get(api_url + "/api/v1/papers", params={"limit": 1}, timeout=20)
    paper_list.raise_for_status()
    papers = paper_list.json()["items"]
    for flow in ("논문별 요약", "Evidence Map", "검수 대기열", "실행 상태·리포트"):
        next(radio for radio in app.radio if radio.label == "작업").set_value(flow).run(timeout=60)
        if flow == "논문별 요약" and papers:
            next(number for number in app.number_input if number.label == "문헌 ID").set_value(
                papers[0]["paper_id"]
            )
            next(button for button in app.button if button.label == "문헌 열기").click().run(timeout=60)
        if flow == "Evidence Map":
            next(select for select in app.selectbox if select.label == "근거 상태").set_value(
                "자동 후보"
            ).run(timeout=60)
        results.append(checked(app, flow))
        if flow == "실행 상태·리포트":
            report_buttons = [button for button in app.button if button.label == "HTML 파일 준비"]
            if report_buttons:
                report_buttons[0].click().run(timeout=60)
                checked(app, "리포트 다운로드 준비")
                results[-1]["report_download_prepared"] = True
    return {
        "api_health": "ok",
        "readonly": True,
        "flows_passed": len(results),
        "results": results,
        "detail_opened": bool(papers),
        "note": "생성·검수·인증 쓰기 작업을 실행하지 않은 독립 AppTest 세션입니다.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8010")
    parser.add_argument("--app", type=Path, default=Path(__file__).resolve().parents[1] / "ui" / "app.py")
    args = parser.parse_args()
    result = run(args.api.rstrip("/"), args.app)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
