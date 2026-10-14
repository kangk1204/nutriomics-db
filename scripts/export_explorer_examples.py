"""Export source-restricted examples and verify their self-contained structure."""

import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.external_assets = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if (tag == "script" and attr.get("src")) or (
            tag == "link" and attr.get("rel") == "stylesheet"
        ):
            self.external_assets.append(attr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    def get(path):
        with urllib.request.urlopen(args.base + path, timeout=60) as response:
            return json.load(response)

    meta = get("/api/v1/explorer/meta")
    (output / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    receipt = {
        "release_id": meta["release_id"],
        "examples": [],
        "direct_file_browser_test": "not_completed_tool_policy",
    }
    for stem, seed in [
        ("NutriOmics_Explorer_rice", "K-FIND:15100065:R101-013000300-0000:a409194825f9"),
        ("NutriOmics_Explorer_walnut", "FDC:2346394"),
    ]:
        graph = get("/api/v1/explorer/graph?" + urllib.parse.urlencode({"seed_id": seed}))
        payload = {
            "release_id": meta["release_id"],
            "node_ids": [n["id"] for n in graph["nodes"]],
            "edge_ids": [e["id"] for e in graph["edges"]],
            "filters": {"domain": "all", "kind": "food", "q": seed, "include_extracted": False},
        }
        files = {}
        for extension in ("html", "json", "csv"):
            request = urllib.request.Request(
                args.base + "/api/v1/explorer/export",
                data=json.dumps({**payload, "format": extension}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                blob = response.read()
            (output / f"{stem}.{extension}").write_bytes(blob)
            files[extension] = {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
        html = (output / f"{stem}.html").read_text(encoding="utf-8")
        assets = Assets()
        assets.feed(html)
        assert not assets.external_assets
        match = re.search(
            r"<script>window\.__NUTRIOMICS_SNAPSHOT__=(.*?);</script>", html, re.DOTALL
        )
        assert match
        embedded = json.loads(match.group(1))
        snapshot = json.loads((output / f"{stem}.json").read_text(encoding="utf-8"))
        assert embedded == snapshot and snapshot["offline"] is True
        assert snapshot["release_id"] == meta["release_id"]
        ids = {n["id"] for n in snapshot["nodes"]}
        assert all(
            e["subject"] in ids and e["object"] in ids and e["source_ids"]
            for e in snapshot["edges"]
        )
        withheld = [e for e in snapshot["edges"] if e.get("export_allowed") is False]
        assert all(e["relation"] == "restricted_source_reference" for e in withheld)
        item = {
            "stem": stem,
            "seed": seed,
            "nodes": len(ids),
            "edges": len(snapshot["edges"]),
            "withheld_edges": len(withheld),
            "external_script_stylesheet_assets": assets.external_assets,
            "embedded_snapshot_matches_json": True,
            "files": files,
        }
        if seed == "FDC:2346394":
            measurements = snapshot["node_details"][seed].get("measurements", [])
            assert measurements, "USDA example must contain permitted measurements"
            item["permitted_seed_measurements"] = len(measurements)
        receipt["examples"].append(item)
    (output / "export-verification.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
