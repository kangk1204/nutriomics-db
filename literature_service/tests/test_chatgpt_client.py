import json

import pytest

from nutri_literature.chatgpt_client import ChatGPTPlanClient, ChatGPTRequestError, available_models


class Auth:
    def access_token(self, registration_id=None):
        assert registration_id == "new-account"
        return "synthetic-test-token"


class Response:
    status_code = 200
    headers = {"Content-Type": "text/event-stream; charset=utf-8"}
    closed = False

    def __init__(self, events=None, payload=None):
        self.events = events or []
        self.payload = payload

    def iter_lines(self, **kwargs):
        for item in self.events:
            yield "data: " + json.dumps(item)
            yield ""

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class HTTP:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def post(self, url, **kwargs):
        assert url == "https://api.openai.com/v1/responses"
        self.kwargs = kwargs
        return self.response

    def get(self, url, **kwargs):
        assert url == "https://api.openai.com/v1/models"
        return self.response


def test_official_contract_and_completed_output_only():
    response = Response([{"type": "response.output_text.delta", "delta": "ignored-partial"},
                         {"type": "response.completed", "response": {"status": "completed", "model": "test",
                          "id": "response-test", "usage": {"input_tokens": 10},
                          "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]}}])
    http = HTTP(response)
    result = ChatGPTPlanClient(Auth(), "test", "new-account", session=http)("rules", "source")
    assert result["text"] == "{}" and response.closed
    assert http.kwargs["json"] == {"model": "test", "instructions": "rules", "store": False,
                                    "stream": True, "input": [{"role": "user", "content": "source"}]}
    assert http.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("event", [None, {"type": "response.failed"}, {"type": "response.incomplete"}])
def test_incomplete_stream_is_not_prediction(event):
    response = Response([event] if event else [{"type": "response.output_text.delta", "delta": "{}"}])
    with pytest.raises(ChatGPTRequestError):
        ChatGPTPlanClient(Auth(), "test", "new-account", session=HTTP(response))("rules", "source")
    assert response.closed


def test_model_catalog_is_account_specific_ordered():
    response = Response(payload={"models": [{"slug": "hidden", "visibility": "hidden"},
        {"slug": "first", "display_name": "First", "visibility": "list"},
        {"slug": "second", "visibility": "list"}]})
    assert [r["slug"] for r in available_models(Auth(), "new-account", session=HTTP(response))] == ["first", "second"]


def test_rate_limit_is_sanitized_and_not_retried():
    response = Response(payload={"secret": "must-not-be-shown"})
    response.status_code = 429
    with pytest.raises(ChatGPTRequestError) as error:
        ChatGPTPlanClient(Auth(), "test", "new-account", session=HTTP(response))("rules", "source")
    assert "must-not-be-shown" not in str(error.value)
    assert response.closed


def test_luna_max_is_sent_explicitly():
    response = Response([{"type": "response.completed", "response": {"status": "completed",
                         "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]}}])
    http = HTTP(response)
    result = ChatGPTPlanClient(Auth(), "gpt-6-luna", "new-account", reasoning_effort="max", session=http)("rules", "source")
    assert http.kwargs["json"]["reasoning"] == {"effort": "max"}
    assert result["reasoning_effort"] == "max"


def test_missing_transport_header_still_requires_completed_sse():
    response = Response([{"type": "response.completed", "response": {"status": "completed",
                         "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]}}])
    response.headers = {}
    assert ChatGPTPlanClient(Auth(), "test", "new-account", session=HTTP(response))("rules", "source")["text"] == "{}"


def test_declared_nonstream_response_is_rejected():
    response = Response()
    response.headers = {"Content-Type": "application/json"}
    with pytest.raises(ChatGPTRequestError):
        ChatGPTPlanClient(Auth(), "test", "new-account", session=HTTP(response))("rules", "source")


def test_finalized_text_and_completed_status_required_together():
    text = {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "{}"}
    response = Response([text, {"type": "response.completed", "response": {"status": "completed", "output": []}}])
    assert ChatGPTPlanClient(Auth(), "test", "new-account", session=HTTP(response))("rules", "source")["text"] == "{}"
    with pytest.raises(ChatGPTRequestError):
        ChatGPTPlanClient(Auth(), "test", "new-account", session=HTTP(Response([text])))("rules", "source")
