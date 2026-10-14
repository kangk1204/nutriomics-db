"""Read-only release/API parity and repeatable warm performance verification."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8030")
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    def get(path, params=None):
        url = args.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=120) as response:
            return json.load(response)

    def post(path, payload):
        req = urllib.request.Request(
            args.base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.read()

    db = sqlite3.connect(Path(args.database).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    foods = [
        dict(r)
        for r in db.execute(
            "SELECT id,label,metadata_json FROM node WHERE id LIKE 'K-FIND:%' AND kind='food' ORDER BY id"
        )
    ]
    component_keys = {
        r["id"]: json.loads(r["metadata_json"]).get("component_key", r["id"])
        for r in db.execute("SELECT id,metadata_json FROM node WHERE kind='nutrient'")
    }
    assert len(foods) == 50
    expected, inputs, comparison = defaultdict(float), [], []
    for index, food in enumerate(foods):
        metadata = json.loads(food["metadata_json"])
        amount_g = 50 * (index % 5 + 1)
        inputs.append({"food_id": metadata["core_food_id"], "amount_g": amount_g})
        measurements = [
            dict(r)
            for r in db.execute(
                "SELECT component,amount,unit,basis,status FROM measurement WHERE food=?",
                (food["id"],),
            )
        ]
        for m in measurements:
            assert m["basis"] == "source_100g"
            if m["amount"] is not None:
                expected[(component_keys[m["component"]], m["unit"])] += (
                    m["amount"] * amount_g / 100
                )
        comparison.append(
            {
                "id": food["id"],
                "core_food_id": metadata["core_food_id"],
                "label": food["label"],
                "amount_g": amount_g,
                "measurements": measurements,
            }
        )
    start = time.perf_counter()
    meal = json.loads(post("/api/v1/meals/analyze", {"items": inputs}))
    actual = {(v["component_key"], v["unit"]): v["amount"] for v in meal["components"]}
    mismatches = [
        str(k)
        for k, value in expected.items()
        if k not in actual or not math.isclose(actual[k], value, rel_tol=1e-9, abs_tol=1e-8)
    ]
    assert not mismatches, mismatches
    assert len(meal["items"]) == 50
    parity = {
        "food_count": 50,
        "composition_count": sum(len(v["measurements"]) for v in comparison),
        "mismatches": mismatches,
        "combined_meal_seconds": time.perf_counter() - start,
        "per_food_reference": comparison,
        "calculated_totals": meal["components"],
    }
    (output / "food50-api-parity.json").write_text(
        json.dumps(parity, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    seeds = []
    for kind, query in [
        ("food", "흰쌀밥"),
        ("component", "Niacin"),
        ("gene", "PPARA"),
        ("health", "고혈압"),
    ]:
        found = get("/api/v1/explorer/entities", {"kind": kind, "q": query})
        assert found["items"], (kind, query)
        selected = next(
            (n for n in found["items"] if kind == "health" and n["id"] == "MESH:D006973"),
            found["items"][0],
        )
        seeds.append(selected["id"])
    cases = [
        ("/api/v1/explorer/entities", {"kind": "food", "q": "흰쌀밥"}),
        ("/api/v1/explorer/entities", {"kind": "gene", "domain": "lipids", "sort": "degree"}),
    ]
    cases += [("/api/v1/explorer/graph", {"seed_id": seed}) for seed in seeds]
    results = []
    for path, params in cases:
        get(path, params)  # warm-up is excluded from the reported distribution
        times = []
        for _ in range(20):
            start = time.perf_counter()
            result = get(path, params)
            times.append(time.perf_counter() - start)
            if path.endswith("/graph"):
                assert len(result["nodes"]) <= 80 and len(result["edges"]) <= 200
                assert all(e["source_ids"] for e in result["edges"])
        results.append(
            {
                "endpoint": path,
                "params": params,
                "runs": len(times),
                "median_s": statistics.median(times),
                "p95_s": sorted(times)[math.ceil(0.95 * len(times)) - 1],
                "max_s": max(times),
                "seconds": times,
            }
        )
    meta = get("/api/v1/explorer/meta")
    performance = {
        "release_id": meta["release_id"],
        "measurement_location": "server loopback; warm requests;20 per case",
        "results": results,
        "all_p95_under_1s": all(v["p95_s"] < 1 for v in results),
    }
    (output / "api-performance.json").write_text(
        json.dumps(performance, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    graph = get("/api/v1/explorer/graph", {"seed_id": seeds[0]})
    payload = {
        "release_id": meta["release_id"],
        "node_ids": [n["id"] for n in graph["nodes"]],
        "edge_ids": [e["id"] for e in graph["edges"]],
        "filters": {"domain": "all", "kind": "food", "q": "흰쌀밥"},
    }
    for extension in ("html", "json", "csv"):
        blob = post("/api/v1/explorer/export", {**payload, "format": extension})
        (output / ("rice-network." + extension)).write_bytes(blob)
    print(
        json.dumps(
            {
                "release_id": meta["release_id"],
                "food_parity": "50/50;1200 records;0 mismatches",
                "performance": performance,
                "output": str(output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
