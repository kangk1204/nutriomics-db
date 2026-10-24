"""Bounded network-only prefetch of identical frozen, source-only requests.

Projection and scoring remain single writers in chatgpt_evaluation. Completed
responses are atomically checkpointed before making them visible to that writer.
No gold, baseline, or dictionary enters a model request.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading

from .chatgpt_client import ChatGPTPlanClient
from .chatgpt_extraction import INSTRUCTIONS, source_input
from .contract_v7 import canonical, sha


SAFE_RESPONSE_FIELDS = ("text", "model", "response_id", "usage", "status", "finish_reason", "incomplete", "test_only",
                        "account_profile_sha256", "actual_model", "elapsed_seconds", "reasoning_effort")


def _atomic_checkpoint(path, value):
    path = Path(path)
    data = canonical(value).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("Immutable response checkpoint differs")
        return
    fd, name = tempfile.mkstemp(prefix="completed-response-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # The outer OS batch lock excludes other CLI/UI writers. Each future
        # owns one PMID; temporary partial files never look like responses.
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class PrefetchedComparison:
    def __init__(self, auth, output, *, registration_id, workers=4, client_factory=ChatGPTPlanClient):
        if not 1 <= workers <= 4:
            raise ValueError("Network prefetch supports one to four requests per model")
        self.output = Path(output)
        from .chatgpt_evaluation import _contract
        self.contract, _validated_dictionary = _contract(self.output)
        del _validated_dictionary
        self.model = self.contract["model"]
        self.reasoning_effort = self.contract["reasoning_effort"]
        self.identity = sha(canonical(self.contract))
        self.auth, self.profile, self.client_factory = auth, registration_id, client_factory
        self._stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chatgpt-http")
        self.futures = {}
        jobs = []
        for pmid in self.contract["source_hashes"]:
            if (self.output / "responses" / ("paper-" + pmid + ".json")).exists():
                continue
            source = json.loads((self.output / "sources" / ("paper-" + pmid + ".json")).read_bytes())
            if source["source_hash"] != sha(source["text"]):
                raise ValueError("Source changed before prefetch")
            text = source_input(source)
            request = {"model": self.model, "instructions": INSTRUCTIONS, "input": text}
            if self.reasoning_effort is not None:
                request["reasoning"] = {"effort": self.reasoning_effort}
            _atomic_checkpoint(self.output / "requests" / ("paper-" + pmid + ".json"),
                               {"contract_sha256": self.identity, "request_sha256": sha(canonical(request)), **request})
            jobs.append((pmid, text))
        for pmid, text in jobs:
            self.futures[sha(text)] = self.pool.submit(self._request, pmid, text)

    def _request(self, pmid, text):
        if self._stop.is_set():
            raise RuntimeError("Prefetch stopped after an earlier failed request")
        request = {"model": self.model, "instructions": INSTRUCTIONS, "input": text}
        if self.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.reasoning_effort}
        fingerprint = sha(canonical(request))
        try:
            client = self.client_factory(self.auth, self.model, self.profile, reasoning_effort=self.reasoning_effort)
            received = client(INSTRUCTIONS, text)
            response = {key: received.get(key) for key in SAFE_RESPONSE_FIELDS}
            if (not isinstance(response["text"], str) or response["status"] != "completed"
                    or response["model"] != self.model or response["reasoning_effort"] != self.reasoning_effort):
                raise ValueError("Incomplete or mismatched prefetch response")
            response.update(request_sha256=fingerprint, contract_sha256=self.identity)
            _atomic_checkpoint(self.output / "responses" / ("paper-" + pmid + ".json"), response)
            return received
        except Exception:
            self._stop.set()
            raise

    def __call__(self, instructions, input):
        if instructions != INSTRUCTIONS:
            raise ValueError("Prefetch prompt differs from the frozen experiment")
        return self.futures[sha(input)].result()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
