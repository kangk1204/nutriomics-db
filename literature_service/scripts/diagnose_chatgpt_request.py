"""Safe protocol diagnosis: no credentials, response text, or private claims printed."""
import json
from pathlib import Path
import re
import sys
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.chatgpt_auth import ChatGPTAuth, RESOURCE
    auth = ChatGPTAuth()
    request_path = next((ROOT / "runtime/chatgpt-comparison").glob("*/requests/*.json"))
    saved = json.loads(request_path.read_bytes())
    model = sys.argv[1] if len(sys.argv) > 1 else saved["model"]
    effort = "max" if model == "gpt-6-luna" else "medium"
    response = requests.post(RESOURCE + "/responses",
        headers={"Authorization": "Bearer " + auth.access_token(), "Accept": "text/event-stream"},
        json={"model": model, "instructions": "Return one JSON object containing only the key ok with value true.", "input": [{"role": "user", "content": "Protocol smoke test."}],
              "store": False, "stream": True, "reasoning": {"effort": effort}},
        timeout=(15, 240), stream=True, allow_redirects=False)
    report = {"status": response.status_code, "content_type": response.headers.get("Content-Type"),
              "request_id_present": bool(response.headers.get("x-request-id")), "model": model}
    try:
        if "json" in response.headers.get("Content-Type", ""):
            payload = response.json()
            report["top_level_keys"] = list(payload) if isinstance(payload, dict) else [type(payload).__name__]
            if isinstance(payload, dict):
                for key in ("object", "status", "type"):
                    value = payload.get(key)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                        report[key] = value
                error = payload.get("error", {})
                if isinstance(error, dict):
                    report["error"] = {k: v for k in ("type", "code", "param")
                        if isinstance((v := error.get(k)), str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", v)}
                report["contains_output"] = isinstance(payload.get("output"), list)
                report["contains_data"] = isinstance(payload.get("data"), (list, dict))
        else:
            report["event_types"] = []
            for line in response.iter_lines():
                if line.startswith(b"data:"):
                    try:
                        event = json.loads(line[5:])
                        if isinstance(event, dict):
                            typ = event.get("type")
                            if isinstance(typ, str) and typ not in report["event_types"]:
                                report["event_types"].append(typ)
                            if typ == "response.completed":
                                report["completed_response_status"] = event.get("response", {}).get("status")
                                break
                            if typ in ("response.failed", "error", "response.incomplete"):
                                report["error_event"] = typ
                                break
                    except ValueError:
                        report["unparseable_event"] = True
    finally:
        response.close()
    print(json.dumps(report))


if __name__ == "__main__":
    main()
