import json
import threading
import time

import pytest

from nutri_literature.chatgpt_prefetch import PrefetchedComparison
from nutri_literature.chatgpt_extraction import INSTRUCTIONS, source_input
from nutri_literature.contract_v7 import canonical, sha


def experiment(tmp_path, monkeypatch):
    sources = []
    for pmid in ("1", "2", "3", "4"):
        source = {"pmid": pmid, "text": "public source " + pmid, "source_hash": sha("public source " + pmid)}
        directory = tmp_path / "sources"
        directory.mkdir(exist_ok=True)
        (directory / ("paper-" + pmid + ".json")).write_text(canonical(source))
        sources.append(source)
    contract = {"model": "gpt-6-luna", "reasoning_effort": "max",
                "source_hashes": {s["pmid"]: sha(canonical(s)) for s in sources}}
    (tmp_path / "contract.json").write_text(canonical(contract))
    monkeypatch.setattr("nutri_literature.chatgpt_evaluation._contract", lambda path: (contract, {}))
    return sources


def test_prefetch_is_bounded_and_preserves_exact_request_response_contract(tmp_path, monkeypatch):
    sources = experiment(tmp_path, monkeypatch)
    lock = threading.Lock()
    stats = {"active": 0, "peak": 0, "calls": 0}

    class Client:
        def __init__(self, auth, model, profile, reasoning_effort):
            assert model == "gpt-6-luna" and reasoning_effort == "max" and profile == "other-account"

        def __call__(self, instructions, text):
            assert instructions == INSTRUCTIONS and "gold" not in json.loads(text)
            with lock:
                stats["active"] += 1
                stats["peak"] = max(stats["peak"], stats["active"])
                stats["calls"] += 1
            time.sleep(.03)
            with lock:
                stats["active"] -= 1
            return {"text": "{}", "status": "completed", "model": "gpt-6-luna", "reasoning_effort": "max",
                    "test_only": True, "Authorization": "synthetic-secret-must-not-save"}

    with PrefetchedComparison(None, tmp_path, registration_id="other-account", workers=2, client_factory=Client) as pending:
        for source in sources:
            assert pending(INSTRUCTIONS, source_input(source))["status"] == "completed"
    assert stats["calls"] == 4 and stats["peak"] == 2
    for path in (tmp_path / "responses").glob("*.json"):
        response = json.loads(path.read_bytes())
        request = json.loads((tmp_path / "requests" / path.name).read_bytes())
        assert response["request_sha256"] == request["request_sha256"]
        assert request["reasoning"] == {"effort": "max"}
        assert "synthetic-secret" not in path.read_text()
    assert not list((tmp_path / "responses").glob("*.tmp"))


def test_frozen_contract_is_validated_before_any_network_call(tmp_path, monkeypatch):
    experiment(tmp_path, monkeypatch)
    def rejected(path):
        raise ValueError("contract differs")
    monkeypatch.setattr("nutri_literature.chatgpt_evaluation._contract", rejected)
    with pytest.raises(ValueError, match="contract differs"):
        PrefetchedComparison(None, tmp_path, registration_id="test")
