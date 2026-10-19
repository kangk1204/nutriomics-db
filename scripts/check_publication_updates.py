"""Monthly source preflight: validator changes are candidates, never scientific approvals."""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

from nutrigenomics.source_updates import atomic_json, fetch_source


def check(registry, output, previous=None, session=None, snapshot_root=None):
    entries = json.loads(Path(registry).read_text(encoding="utf-8"))["sources"]
    old = {r["id"]: r for r in json.loads(Path(previous).read_text(encoding="utf-8")).get("sources", [])} if previous else {}
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
                    if snapshot_root is not None:
                        snapshot = fetch_source(entry, snapshot_root, session=client)
                        result["snapshot"] = snapshot
                        previous_hash = old.get(entry["id"], {}).get("snapshot", {}).get("sha256")
                        result["status"] = ("content_unchanged" if previous_hash == snapshot["sha256"] else
                                            "content_snapshot_initial" if previous_hash is None else
                                            "content_changed_review_required")
                        result["affected_analysis_ids"] = entry.get("affected_analysis_ids", [])
                        result["dependency_scope_status"] = "declared" if "affected_analysis_ids" in entry else "requires_dependency_audit"
                except (requests.RequestException, ValueError, OSError) as exc:
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
    p.add_argument("--snapshot-root", type=Path, help="Actually fetch/hash registered source files; HEAD alone is not content verification")
    a = p.parse_args()
    print(json.dumps(check(a.registry, a.output, a.previous, snapshot_root=a.snapshot_root), indent=2))
