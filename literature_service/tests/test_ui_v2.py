"""Execute the Streamlit review flow against synthetic API records."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
import test_api_v2 as fixtures

AppTest = pytest.importorskip("streamlit.testing.v1", reason="Streamlit UI is installed on the Linux service runtime").AppTest

workspace = fixtures.workspace
v2_workspace = fixtures.v2_workspace


def test_engine_comparison_and_abstention_queue_render(v2_workspace, monkeypatch):
    client, _ = v2_workspace
    def request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        return client.request(method, urlsplit(url).path, **kwargs)
    monkeypatch.setattr(requests, "request", request)
    app = AppTest.from_file(str(Path(__file__).parents[1] / "ui" / "app.py"), default_timeout=15).run()
    assert not app.exception
    next(widget for widget in app.selectbox if widget.label == "추출 엔진").select("heuristic-v2").run()
    assert not app.exception
    next(widget for widget in app.radio if widget.label == "작업").set_value("논문별 요약").run()
    next(widget for widget in app.button if widget.label == "문헌 열기").click().run()
    assert not app.exception
    assert "v1·v2 비교" in [tab.label for tab in app.tabs]
    assert any("heuristic-v2" in caption.value for caption in app.caption)
    next(widget for widget in app.radio if widget.label == "작업").set_value("검수 대기열").run()
    assert not app.exception
    assert any("보류 1건" in caption.value for caption in app.caption)
    assert any("ambiguous_roles" in warning.value for warning in app.warning)
    assert any(widget.label.startswith("연구 조건 수정") for widget in app.text_area)
    next(widget for widget in app.radio if widget.label == "작업").set_value("실행 상태·리포트").run()
    assert not app.exception
    assert any("엔진별 요약" in caption.value for caption in app.caption)
