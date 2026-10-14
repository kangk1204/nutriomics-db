"""Monthly source preflight: validator changes are candidates, never scientific approvals."""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

from nutrigenomics.source_updates import atomic_json


def check(registry, output, previous=None, session=None):
    entries = json.loads(Path(registry).read_text())["sources"]
    old = {r["id"]: r for r in json.loads(Path(previous).read_text()).get("sources", [])} if previous else {}
    client = session or requests.Session()
    results = []
    try:
        for entry in entries:
            result = {"id": entry["id"], "url": entry["url"], "scientific_review": "not_performed"}
            if not entry.get("enabled"):
                result["status"] = "manual_source"
            elif urlsplit(entry["url"]).scheme != "https" or urlsplit(entry["url"]).hostname not in entry["allowed_hosts"]:
                result["status"] = "registry_url_rejected"
            else:
                try:
                    with client.head(entry["url"], timeout=15, allow_redirects=False) as response:
                        result.update(http_status=response.status_code, etag=response.headers.get("ETag"), last_modified=response.headers.get("Last-Modified"))
                    if result["http_status"] != 200:
                        result["status"] = "source_check_unavailable"
                    elif not result["etag"] and not result["last_modified"]:
                        result["status"] = "content_hash_download_required"
                    elif not old.get(entry["id"]):
                        result["status"] = "initial_validator_snapshot"
                    elif all(result.get(k) == old[entry["id"]].get(k) for k in ("etag", "last_modified")):
                        result["status"] = "validators_unchanged_content_not_reverified"
                    else:
                        result["status"] = "possible_change_download_and_review_required"
                except requests.RequestException as exc:
                    result.update(status="source_check_unavailable", error_type=type(exc).__name__)
            results.append(result)
    finally:
        if session is None:
            client.close()
    report = {"checked_at": datetime.now(UTC).isoformat(), "sources": results,
              "automatic_publication": False, "automatic_expert_approval": False,
              "next_step": "fetch immutable snapshots; compare content hashes/schema/values; human review; build and validate a new release"}
    atomic_json(output, report)
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", type=Path, default=Path("config/update_sources.json"))
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--previous", type=Path)
    a = p.parse_args()
    print(json.dumps(check(a.registry, a.output, a.previous), indent=2))
