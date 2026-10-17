"""Measure real exports and legacy API compatibility; browser checks remain separate."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from scripts.benchmark_explorer_performance import digest
from scripts.performance_release_gate import sha


def request(base, path, payload=None, timeout=45):
    req = urllib.request.Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            result = {"status": response.status, "seconds": time.perf_counter() - started,
                      "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                      "server_timing": response.headers.get("Server-Timing"),
                      "release_header": response.headers.get("X-NutriOmics-Release")}
            return result, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": None, "seconds": time.perf_counter() - started, "error": type(exc).__name__}, b""


def common(cases, manifest, release):
    spec = json.loads(Path(cases).read_text(encoding="utf-8"))
    if digest(spec["cases"]) != spec["case_sha256"]:
        raise ValueError("Frozen evaluation cases changed")
    return {"release_id": release, "case_sha256": spec["case_sha256"], "source_manifest_sha256": sha(manifest)}


def validate_snapshot(snapshot, graph):
    failures = []
    if snapshot.get("release_id") != graph["release_id"]:
        failures.append("release")
    for field in ("nodes", "edges"):
        if [v["id"] for v in snapshot.get(field, [])] != [v["id"] for v in graph[field]]:
            failures.append("selection:" + field)
    for edge in snapshot.get("edges", []):
        if edge.get("tier") == "restricted":
            if edge.get("export_allowed") is not False or edge.get("relation") != "restricted_source_reference":
                failures.append("restricted_edge:" + edge["id"])
        elif edge.get("export_allowed") is not True:
            failures.append("unpermitted_edge:" + edge["id"])
    for key, detail in snapshot.get("node_details", {}).items():
        for field in ("effects", "methylation_annotations", "regulatory_evidence", "verified_identifier_links"):
            if any(row.get("export_allowed") is not True for row in detail.get(field, [])):
                failures.append("unpermitted_detail:" + key + ":" + field)
    return failures


def parse_html_snapshot(html):
    prefix = "<script>window.__NUTRIOMICS_SNAPSHOT__="
    if "connect-src 'none'" not in html or '<script src=' in html or '<link rel="stylesheet"' in html:
        raise ValueError("HTML must contain its runtime and prohibit network connections")
    start = html.index(prefix) + len(prefix)
    value, length = json.JSONDecoder().raw_decode(html[start:])
    if not html[start + length:].startswith(";</script>"):
        raise ValueError("Unexpected embedded snapshot boundary")
    return value


def exports(base, measured, cases, manifest, output, graph_case='yogurt-max-graph'):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(cases).read_text(encoding='utf-8'))
    selected_case = next(c for c in spec['cases'] if c['id'] == graph_case)
    if (selected_case['params'].get('max_nodes'), selected_case['params'].get('max_edges')) != (250, 750):
        raise ValueError('Export validation requires a frozen 250-node/750-edge request')
    graph = json.loads((Path(measured) / (graph_case + "-response.json")).read_text(encoding="utf-8"))
    seed_id = selected_case['params']['seed_id']
    seed = next(n for n in graph['nodes'] if n['id'] == seed_id)
    context = {'seed_id': seed_id}
    if seed.get('kind') == 'food':
        context['intake_g'] = 150
    report = {**common(cases, manifest, graph["release_id"]), "formats": {}, "validation_errors": [],
              "offline_browser_pass": False, "offline_browser_status": "pending_actual_browser",
              "maximum_selection_verified": len(graph["nodes"]) == 250 and len(graph["edges"]) == 750,
              "display_limit_valid": 0 < len(graph['nodes']) <= 250 and len(graph['edges']) <= 750,
              "selection": {"nodes": len(graph["nodes"]), "edges": len(graph["edges"])},
              "graph_case": graph_case, "seed_id": seed_id,
              "requested_limits": {key: selected_case['params'].get(key) for key in ('max_nodes','max_edges')}}
    payload = {"release_id": graph["release_id"], "node_ids": [n["id"] for n in graph["nodes"]],
               "edge_ids": [e["id"] for e in graph["edges"]],
               "positions": {n["id"]: {"x": i * 12, "y": (i % 7) * 90} for i, n in enumerate(graph["nodes"])},
               "filters": {"domain": "all", "tier": "all", "include_extracted": False,
                           "scientific_context": context}}
    snapshots = {}
    for fmt in ("json", "csv", "html"):
        metrics, body = request(base, "/api/v1/explorer/export", {**payload, "format": fmt})
        report["formats"][fmt] = metrics
        (output / ("maximum-selection." + fmt)).write_bytes(body)
        if metrics["status"] != 200 or metrics.get("release_header") != graph["release_id"]:
            report["validation_errors"].append("http:" + fmt)
            continue
        try:
            if fmt in ("html", "json"):
                data = parse_html_snapshot(body.decode("utf-8")) if fmt == "html" else json.loads(body)
                snapshots[fmt] = data
                report["validation_errors"].extend(fmt + ":" + v for v in validate_snapshot(data, graph))
                if data.get("positions") != payload["positions"] or data.get("filters") != payload["filters"]:
                    report["validation_errors"].append("frozen_context:" + fmt)
            else:
                rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
                for field, kind in (("nodes", "node"), ("edges", "edge")):
                    if [r["id"] for r in rows if r["record_type"] == kind] != [r["id"] for r in graph[field]]:
                        report["validation_errors"].append("csv_selection:" + field)
                if any(r["release_id"] != graph["release_id"] for r in rows):
                    report["validation_errors"].append("csv_release")
        except (ValueError, KeyError, TypeError) as exc:
            report["validation_errors"].append("payload:" + fmt + ":" + type(exc).__name__)
    if set(snapshots) == {"json", "html"} and snapshots["json"] != snapshots["html"]:
        report["validation_errors"].append("html_json_mismatch")
    report["verified_at"] = datetime.now(UTC).isoformat()
    (output / "exports.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def compatibility(baseline, candidate, cases, manifest, release, output):
    report = {**common(cases, manifest, release), "comparisons": [], "passed": True}
    routes = ["/healthz", "/api/v1/foods?query=" + urllib.parse.quote("흰쌀밥") + "&limit=5",
              "/api/v1/foods?query=__no_such_food_20261010__&limit=5", "/api/v1/studies",
              "/api/v1/studies/GSE98645", "/api/v1/interventions"]
    for route in routes:
        a, before = request(baseline, route)
        b, after = request(candidate, route)
        try:
            equal = a["status"] == b["status"] == 200 and json.loads(before) == json.loads(after)
        except ValueError:
            equal = False
        report["comparisons"].append({"route": route, "before": a, "after": b, "equal": equal})
        report["passed"] = report["passed"] and equal
    report["verified_at"] = datetime.now(UTC).isoformat()
    Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["exports", "compatibility"])
    p.add_argument("--base", default="http://127.0.0.1:8020")
    p.add_argument("--baseline", default="http://127.0.0.1:8000")
    for name in ("cases", "manifest", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--measured", type=Path)
    p.add_argument("--release")
    p.add_argument("--graph-case", default='yogurt-max-graph')
    p.add_argument('--allow-unsaturated-selection', action='store_true', help='Report actual public scope when it cannot fill both limits; never lowers requested graph limits')
    a = p.parse_args()
    if a.mode == "exports":
        if a.measured is None:
            p.error("--measured required for the fixed maximum graph response")
        result = exports(a.base, a.measured, a.cases, a.manifest, a.output, a.graph_case)
        passed = not result["validation_errors"] and result['display_limit_valid'] and (result["maximum_selection_verified"] or a.allow_unsaturated_selection) and all(v["status"] == 200 and v["seconds"] <= 10 for v in result["formats"].values())
    else:
        if not a.release:
            p.error("--release required for compatibility report")
        result = compatibility(a.baseline, a.base, a.cases, a.manifest, a.release, a.output)
        passed = result["passed"]
    print(json.dumps({"passed": passed, "report": str(a.output), "browser_verified": False}, ensure_ascii=False))
    raise SystemExit(0 if passed else 1)
