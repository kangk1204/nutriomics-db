"""Official Responses API using this application's separately authorized SIWC account.

Never calls ChatGPT web endpoints or consumes Codex credentials/API keys.
"""
from __future__ import annotations

import json
import time
import hashlib

import requests

from .chatgpt_auth import ChatGPTAuth, ChatGPTAuthError, RESOURCE


class ChatGPTRequestError(RuntimeError):
    """Contains only safe status metadata, never a server response body."""


def _http_error(status: int) -> ChatGPTRequestError:
    if status in (401, 403):
        return ChatGPTRequestError("ChatGPT 사용 권한을 확인하지 못했습니다. 계정을 다시 연결하세요.")
    if status == 429:
        return ChatGPTRequestError("연결 계정의 사용 한도에 도달했습니다. 결과를 보존했으며 나중에 재개할 수 있습니다.")
    return ChatGPTRequestError(f"OpenAI 공식 요청이 HTTP {status}로 실패했습니다. 성공 결과로 집계하지 않습니다.")


def available_models(auth: ChatGPTAuth, registration_id: str | None = None, *, session=None) -> list[dict]:
    http = session or requests.Session()
    try:
        response = http.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + auth.access_token(registration_id)},
                            timeout=(15, 45), allow_redirects=False)
        if response.status_code != 200:
            raise _http_error(response.status_code)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ChatGPTRequestError("연결 계정의 공식 모델 목록 형식을 확인하지 못했습니다.")
        rows = []
        for model in payload["models"]:
            if not isinstance(model, dict) or model.get("visibility") != "list":
                continue
            if isinstance(model.get("slug"), str) and model["slug"]:
                rows.append({"slug": model["slug"], "display_name": str(model.get("display_name") or model["slug"])})
        if not rows:
            raise ChatGPTRequestError("연결 계정에서 사용할 수 있는 모델이 없습니다.")
        return rows
    except (ChatGPTRequestError, ChatGPTAuthError):
        raise
    except Exception:
        raise ChatGPTRequestError("공식 모델 목록에 연결하지 못했습니다. 네트워크 상태를 확인하세요.") from None


def _response_text(response: dict) -> str:
    parts = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "refusal":
                raise ChatGPTRequestError("모델이 이 요청에 응답하지 않았습니다. 성공으로 집계하지 않습니다.")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)


class ChatGPTPlanClient:
    def __init__(self, auth: ChatGPTAuth, model: str, registration_id: str, *, reasoning_effort=None, session=None):
        self.auth = auth
        self.model = model
        self.registration_id = registration_id
        if reasoning_effort not in {None, "none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Unsupported reasoning effort")
        self.reasoning_effort = reasoning_effort
        self.http = session or requests.Session()

    def __call__(self, instructions: str, input: str) -> dict:
        """One full source-only request. Interrupted streams are never predictions."""
        started = time.monotonic()
        body = {"model": self.model, "instructions": instructions,
                "input": [{"role": "user", "content": input}], "store": False, "stream": True}
        if self.reasoning_effort is not None:
            body["reasoning"] = {"effort": self.reasoning_effort}
        response = None
        try:
            token = self.auth.access_token(self.registration_id)
            response = self.http.post(RESOURCE + "/responses", json=body,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "text/event-stream"},
                stream=True, timeout=(15, 240), allow_redirects=False)
            if response.status_code != 200:
                raise _http_error(response.status_code)
            # Some transports omit response headers while preserving the SSE
            # body. Still require a valid response.completed event below.
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "text/event-stream" not in content_type:
                raise ChatGPTRequestError("공식 응답이 완료된 스트림 형식이 아닙니다.")
            data_lines, completed, size, finalized = [], None, 0, {}
            for line in response.iter_lines(decode_unicode=True):
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                size += len(line or "")
                if size > 16 * 1024 * 1024:
                    raise ChatGPTRequestError("모델 응답이 허용 크기를 초과했습니다.")
                if line:
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    continue
                if not data_lines:
                    continue
                payload = "\n".join(data_lines)
                data_lines = []
                if payload == "[DONE]":
                    continue
                event = json.loads(payload)
                if event.get("type") in {"error", "response.failed", "response.incomplete"}:
                    raise ChatGPTRequestError("모델 응답이 실패하거나 중단됐습니다. 해당 논문부터 재개합니다.")
                if event.get("type") in {"response.refusal.delta", "response.refusal.done"}:
                    raise ChatGPTRequestError("모델이 이 요청에 응답하지 않았습니다.")
                if event.get("type") == "response.output_text.done":
                    key = (event.get("output_index", 0), event.get("content_index", 0))
                    if not isinstance(event.get("text"), str) or any(type(v) is not int for v in key):
                        raise ChatGPTRequestError("최종 텍스트 이벤트의 형식을 확인하지 못했습니다.")
                    if key in finalized and finalized[key] != event["text"]:
                        raise ChatGPTRequestError("최종 텍스트 이벤트가 서로 일치하지 않습니다.")
                    finalized[key] = event["text"]
                if event.get("type") == "response.completed":
                    completed = event.get("response")
                    break
            if not isinstance(completed, dict) or completed.get("status") != "completed":
                raise ChatGPTRequestError("응답 완료를 확인하지 못했습니다. 부분 응답은 평가하지 않습니다.")
            text = _response_text(completed)
            final_stream_text = "".join(finalized[k] for k in sorted(finalized))
            if text and final_stream_text and text != final_stream_text:
                raise ChatGPTRequestError("응답 완료 객체와 최종 텍스트가 일치하지 않습니다.")
            # The public API finalizes text in output_text.done. Some plan
            # transports return an empty output array in response.completed.
            # Use finalized text only AFTER overall completion is confirmed.
            text = text or final_stream_text
            if not text.strip():
                raise ChatGPTRequestError("모델의 최종 응답이 비어 있습니다.")
            return {"text": text, "model": self.model, "actual_model": completed.get("model"),
                    "response_id": completed.get("id"), "usage": completed.get("usage", {}),
                    "status": "completed", "elapsed_seconds": time.monotonic() - started,
                    "reasoning_effort": self.reasoning_effort,
                    "account_profile_sha256": hashlib.sha256(self.registration_id.encode()).hexdigest()}
        except (ChatGPTRequestError, ChatGPTAuthError):
            raise
        except Exception:
            raise ChatGPTRequestError("공식 ChatGPT 요청을 완료하지 못했습니다. 저장된 결과를 유지합니다.") from None
        finally:
            if response is not None:
                response.close()
