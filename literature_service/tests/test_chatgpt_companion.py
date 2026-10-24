import re
import subprocess
import sys

from fastapi.testclient import TestClient

from nutri_literature.chatgpt_batch import batch_lock, batch_busy
from nutri_literature.chatgpt_companion import create_companion


class Auth:
    def status(self):
        return {"active_registration_id": None, "accounts": []}

    def select_account(self, registration_id):
        raise AssertionError("account must not change")

    def sign_out(self, registration_id):
        raise AssertionError("account must not sign out")


def setup(tmp_path):
    client = TestClient(create_companion(tmp_path / "home", tmp_path / "out", auth=Auth()),
                        base_url="http://127.0.0.1:8520")
    page = client.get("/")
    token = re.search(r'const key="([^"]+)"', page.text)[1]
    return client, {"X-Companion-Key": token}, page


def test_loopback_csrf_and_render_boundary(tmp_path):
    client, headers, page = setup(tmp_path)
    assert page.status_code == 200 and "Continue with ChatGPT" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert client.get("/status").status_code == 403
    assert client.get("/status", headers=headers).status_code == 200
    assert client.get("/status", headers={**headers, "Origin": "https://untrusted.example"}).status_code == 403
    assert client.get("/", headers={"Host": "untrusted.example:8520"}).status_code == 403
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_external_batch_prevents_account_mutation_and_duplicate_start(tmp_path):
    client, headers, _ = setup(tmp_path)
    with batch_lock(tmp_path / "out"):
        assert client.get("/status", headers=headers).json()["running"]
        for endpoint, payload in [("select", {"registration_id": "a"}), ("logout", {"registration_id": "a"}),
                                  ("connect", {}), ("compare", {"model": "gpt-6-luna", "reasoning_effort": "max"})]:
            assert client.post("/" + endpoint, json=payload, headers=headers).status_code == 409


def test_process_lock_releases_without_stale_pid_problem(tmp_path):
    assert not batch_busy(tmp_path)
    code = "from nutri_literature.chatgpt_batch import batch_busy;import sys;print(batch_busy(sys.argv[1]))"
    with batch_lock(tmp_path):
        assert subprocess.check_output([sys.executable, "-c", code, str(tmp_path)], text=True).strip() == "True"
    assert subprocess.check_output([sys.executable, "-c", code, str(tmp_path)], text=True).strip() == "False"
