"""The development comparison page distinguishes exploratory measurements."""
from pathlib import Path
from urllib.parse import urlsplit
import pytest
import requests
import test_api as api_fixtures

AppTest=pytest.importorskip('streamlit.testing.v1').AppTest
workspace=api_fixtures.workspace


def test_development_page_no_prediction_or_promotion(workspace,monkeypatch):
    client,_=workspace
    def request(method,url,**kwargs):
        kwargs.pop('timeout',None)
        return client.request(method,urlsplit(url).path,**kwargs)
    monkeypatch.setattr(requests,'request',request)
    app=AppTest.from_file(str(Path(__file__).parents[1]/'ui/app.py'),default_timeout=15).run()
    next(w for w in app.radio if w.label=='작업').set_value('알고리즘 개선').run()
    assert not app.exception
    assert any('동일 개발 논문' in value.value for value in app.info)
    assert not any(w.label=='heuristic-v3' for w in app.selectbox)
