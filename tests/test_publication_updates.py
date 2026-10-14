import json

from scripts.check_publication_updates import check


def test_monthly_preflight_never_promotes_or_confuses_http_failure_with_unchanged(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"sources": [
        {"id": "open", "enabled": True, "url": "https://example.org/data", "allowed_hosts": ["example.org"]},
        {"id": "failed", "enabled": True, "url": "https://example.org/failed", "allowed_hosts": ["example.org"]},
        {"id": "manual", "enabled": False, "url": "https://example.org/manual"},
    ]}))
    class Response:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"ETag": "new"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Client:
        def head(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            return Response(503 if url.endswith("failed") else 200)
    previous = tmp_path / "before.json"
    previous.write_text(json.dumps({"sources": [{"id": "open", "etag": "old", "last_modified": None}]}))
    result = check(registry, tmp_path / "after.json", previous, Client())
    assert [r["status"] for r in result["sources"]] == ["possible_change_download_and_review_required", "source_check_unavailable", "manual_source"]
    assert not result["automatic_expert_approval"] and not result["automatic_publication"]
