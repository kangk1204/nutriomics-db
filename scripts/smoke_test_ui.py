from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


def main() -> None:
    app_path = Path(__file__).resolve().parents[1] / "ui" / "app.py"
    app = AppTest.from_file(str(app_path), default_timeout=60).run()
    if app.exception:
        messages = "\n".join(element.value for element in app.exception)
        raise AssertionError(f"Streamlit UI raised an exception:\n{messages}")

    tab_labels = [tab.label for tab in app.tabs]
    expected = ["식사 분석", "근거 탐색", "연구 상세", "분석·내보내기"]
    if tab_labels != expected:
        raise AssertionError(f"Expected UI tabs {expected!r}, got {tab_labels!r}")

    app.selectbox(key="study_detail_accession").set_value("GSE54325").run()
    app.button(key="study_detail_load").click().run()
    if app.exception:
        messages = "\n".join(element.value for element in app.exception)
        raise AssertionError(f"Study detail flow raised an exception:\n{messages}")
    detail_metric_labels = [metric.label for metric in app.get("metric")]
    if detail_metric_labels != [
        "연구에서 보고한 사람 수",
        "GEO 시료 기록",
        "재분석에 사용한 사람 수",
    ]:
        raise AssertionError(f"Study detail counts were not rendered distinctly: {detail_metric_labels!r}")

    download_labels = [button.label for button in app.get("download_button")]
    if "식사 기록 JSON" not in download_labels or "식사 기록 CSV" not in download_labels:
        raise AssertionError("The meal export controls are not available in the export tab")
    app.get("download_button")[1].click().run()
    if app.exception:
        messages = "\n".join(element.value for element in app.exception)
        raise AssertionError(f"Meal CSV export raised an exception:\n{messages}")

    app.radio(key="export_kind").set_value("연구 효과 결과").run()
    study_options = app.selectbox(key="export_study").options
    target_study = next((label for label in study_options if label.startswith("GSE127530 ·")), None)
    if target_study is None:
        raise AssertionError("GSE127530 is missing from the UI study export choices")
    app.selectbox(key="export_study").set_value(target_study)
    app.select_slider(key="export_limit").set_value(1_000)
    app.run()
    if app.exception:
        messages = "\n".join(element.value for element in app.exception)
        raise AssertionError(f"Study effect export controls raised an exception:\n{messages}")
    evidence_downloads = [button.label for button in app.get("download_button")]
    if "연구 효과 JSON" not in evidence_downloads or "연구 효과 CSV" not in evidence_downloads:
        raise AssertionError("The study effect export controls are not available")
    app.get("download_button")[-1].click().run()
    if app.exception:
        messages = "\n".join(element.value for element in app.exception)
        raise AssertionError(f"Study CSV export raised an exception:\n{messages}")

    print(
        {
            "status": "passed",
            "tabs": tab_labels,
            "study_detail_metrics": detail_metric_labels,
            "meal_export_buttons": [
                label for label in download_labels if label.startswith("식사 기록")
            ],
            "study_export_buttons": [
                label for label in evidence_downloads if label.startswith("연구 효과")
            ],
            "study_export_smoke": "GSE127530 CSV download handler passed",
        }
    )


if __name__ == "__main__":
    main()
