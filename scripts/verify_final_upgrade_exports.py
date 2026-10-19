"""Measure selected maximum-graph exports and inspect their contents without executing HTML.

This operator is read-only on the release and service. Static HTML inspection is
explicitly separate from an actual browser/offline execution test.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from nutrigenomics.source_updates import atomic_json
from scripts.benchmark_explorer_performance import digest


class StaticHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csp, self.external_assets = None, []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "meta" and attrs.get("http-equiv", "").casefold() == "content-security-policy":
            self.csp = attrs.get("content", "")
        for key in (("src",) if tag in {"script", "iframe", "img", "audio", "video", "source"} else ("href",) if tag == "link" else ()):
            value = attrs.get(key)
            if value and not value.startswith(("data:", "blob:")):
                self.external_assets.append({"tag": tag, "attribute": key, "value": value})


def html_snapshot(body):
    html = body.decode("utf-8")
    parser = StaticHTML()
    parser.feed(html)
    match = re.search(r"window\.__NUTRIOMICS_SNAPSHOT__\s*=", html)
    if not match:
        raise ValueError("Embedded snapshot is absent")
    snapshot, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
    issues = []
    if parser.external_assets:
        issues.append("external_asset_dependency")
    if not parser.csp or "connect-src 'none'" not in parser.csp or "default-src 'none'" not in parser.csp:
        issues.append("offline_csp_not_closed")
    if re.search(r"@import\s|url\(\s*['\"]?https?://", html, re.I):
        issues.append("external_css_dependency")
    return snapshot, issues


def selected_graph(spec, responses):
    candidates = []
    for case in spec["cases"]:
        if case["path"].endswith("/graph") and case.get("params", {}).get("max_nodes") == 250 and case["params"].get("max_edges") == 750:
            path = Path(responses) / (case["id"] + "-response.json")
            if path.is_file():
                graph = json.loads(path.read_text(encoding="utf-8"))
                candidates.append((len(graph.get("edges", [])), len(graph.get("nodes", [])), case["id"], graph))
    if not candidates:
        raise ValueError("No measured maximum-cap graph is available")
    _, _, name, graph = max(candidates, key=lambda item: item[:3])
    if graph.get("release_id") != spec["baseline_release"] or not graph.get("nodes"):
        raise ValueError("Selected graph release differs or has no nodes")
    return name, graph


def native_expectations(database, node_ids, edge_ids, rights):
    """Read only selected records and the small source policy catalogue."""
    policies = json.loads(Path(rights).read_text(encoding="utf-8")).get("policies", []) if rights else []
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        sources = {row["id"]: json.loads(row["metadata_json"]) for row in db.execute("SELECT * FROM source")}
        def permitted(identifier):
            record = sources.get(identifier, {})
            if record.get("export_allowed") is False:
                return False
            allowed = (record.get("export_allowed") is True or record.get("redistribution_verified") is True
                       or record.get("rights_status") == "verified")
            for policy in policies:
                if record.get("source") == policy.get("source") and identifier.startswith(policy.get("source_id_prefix", "\0")):
                    return policy.get("export_allowed") is True
            return allowed
        edges, measurements = {}, {}
        for ids, table, column, target in ((edge_ids, "edge", "id", edges), (node_ids, "measurement", "food", measurements)):
            for offset in range(0, len(ids), 400):
                batch = ids[offset:offset + 400]
                for row in db.execute("SELECT * FROM " + table + " WHERE " + column + " IN (" + ",".join("?" for _ in batch) + ")", batch):
                    item = dict(row)
                    if table == "edge":
                        source_ids = json.loads(item["source_ids_json"])
                        item["permitted"] = bool(source_ids) and all(permitted(identifier) for identifier in source_ids)
                        target[item["id"]] = item
                    elif permitted(item["source_id"]):
                        target[item["id"]] = item
        permitted_source_ids = {identifier for identifier in sources if permitted(identifier)}
    return {"edges": edges, "measurements": measurements, "permitted_source_ids": permitted_source_ids}


def inspect_snapshot(snapshot, graph, expected):
    issues = []
    if snapshot.get("release_id") != graph["release_id"] or snapshot.get("offline") is not True:
        issues.append("snapshot_release_or_offline_flag_changed")
    for key in ("nodes", "edges"):
        actual = [row.get("id") for row in snapshot.get(key, [])]
        required = [row["id"] for row in graph[key]]
        if len(actual) != len(set(actual)) or set(actual) != set(required):
            issues.append("selected_" + key + "_changed")
    nodes = {row["id"] for row in snapshot.get("nodes", [])}
    for edge in snapshot.get("edges", []):
        native = expected["edges"].get(edge["id"])
        if not native or edge.get("subject") != native["subject"] or edge.get("object") != native["object"] or not {edge.get("subject"), edge.get("object")} <= nodes:
            issues.append("edge_identity_or_endpoint_changed:" + str(edge.get("id")))
            continue
        if native["permitted"]:
            if edge.get("export_allowed") is not True or edge.get("relation") != native["relation"]:
                issues.append("permitted_edge_changed:" + edge["id"])
        elif (edge.get("export_allowed") is not False or edge.get("tier") != "restricted"
              or edge.get("relation") != "restricted_source_reference" or set(edge.get("context", {})) != {"note"}):
            issues.append("restricted_source_payload_leaked:" + edge["id"])
    actual_measurements = {}
    for detail in snapshot.get("node_details", {}).values():
        for measurement in detail.get("measurements", []):
            actual_measurements[measurement["id"]] = measurement
        for key in ("effects", "methylation_annotations", "regulatory_evidence", "verified_identifier_links"):
            if any(row.get("export_allowed") is not True for row in detail.get(key, [])):
                issues.append("unapproved_detail_export:" + key)
    if set(actual_measurements) != set(expected["measurements"]):
        issues.append("permitted_measurement_scope_changed")
    for identifier, item in actual_measurements.items():
        original = expected["measurements"].get(identifier)
        if not original or any(item.get(key) != original[key] for key in ("food", "component", "amount", "unit", "basis", "status", "source_id")):
            issues.append("measurement_value_changed:" + identifier)
    def scan(value):
        if isinstance(value, dict):
            if str(value.get("id", "")).startswith("ClinicalResult:") and "endpoint" in value:
                if not value.get("source_ids") or not set(value["source_ids"]) <= expected["permitted_source_ids"]:
                    issues.append("unapproved_clinical_value_export")
            for nested in value.values():
                scan(nested)
        elif isinstance(value, list):
            for nested in value:
                scan(nested)
        elif isinstance(value, str) and (value.startswith(("/home/", "/mnt/", "/tmp/")) or re.match(r"^[A-Za-z]:[\\/]", value)):
            issues.append("operational_path_leaked")
    scan(snapshot)
    return sorted(set(issues))


def inspect_csv(body, snapshot):
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    issues = []
    if any(row.get("release_id") != snapshot["release_id"] for row in rows):
        issues.append("csv_release_changed")
    for key, kind in (("nodes", "node"), ("edges", "edge")):
        selected = [row["id"] for row in rows if row["record_type"] == kind]
        if len(selected) != len(set(selected)) or set(selected) != {row["id"] for row in snapshot[key]}:
            issues.append("csv_" + key + "_changed")
    expected = {item["id"]: item for detail in snapshot.get("node_details", {}).values() for item in detail.get("measurements", [])}
    measured = {row["id"]: row for row in rows if row["record_type"] == "measurement"}
    if set(expected) != set(measured):
        issues.append("csv_measurement_scope_changed")
    for edge in snapshot["edges"]:
        actual = next((row for row in rows if row["record_type"] == "edge" and row["id"] == edge["id"]), {})
        if any(actual.get(key) != edge.get(key) for key in ("subject", "object", "relation", "tier", "review_status")) or json.loads(actual.get("context", "{}")) != edge["context"]:
            issues.append("csv_edge_payload_changed:" + edge["id"])
    for identifier, measurement in measured.items():
        original = expected.get(identifier, {})
        if any(measurement.get(key) != ("" if original.get(key) is None else str(original[key]))
               for key in ("subject", "component", "amount", "unit", "basis", "status", "source_id") if key != "subject") or measurement.get("subject") != original.get("food"):
            issues.append("csv_measurement_value_changed:" + identifier)
    effects = {(kind, detail["id"], *(str(item.get(key) or "") for key in
               ("series_accession", "feature_type", "feature_id", "contrast", "analysis_version"))): item
               for detail in snapshot.get("node_details", {}).values()
               for kind, name in (("expression_effect", "effects"), ("probe_annotation_effect", "methylation_annotations"))
               for item in detail.get(name, [])}
    actual_effects = {}
    for row in rows:
        if row["record_type"] in {"expression_effect", "probe_annotation_effect"}:
            key = (row["record_type"], row["subject"], *(row.get(name, "") for name in
                   ("series_accession", "feature_type", "feature_id", "contrast", "analysis_version")))
            actual_effects[key] = row
            original = effects.get(key)
            if original is None or json.loads(row["context"]) != original or any(
                row.get(name) != ("" if original.get(name) is None else str(original[name])) for name in
                ("effect_estimate", "standard_error", "p_value", "q_value", "effect_unit", "n_people", "n_samples")):
                issues.append("csv_effect_value_changed:" + row.get("feature_id", ""))
    if set(effects) != set(actual_effects):
        issues.append("csv_effect_scope_changed")
    return sorted(set(issues))


def verify(base, database, spec_path, responses, output, rights=None, source_manifest=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    if digest(spec["cases"]) != spec["case_sha256"]:
        raise ValueError("Frozen cases changed")
    name, graph = selected_graph(spec, responses)
    payload = {"release_id": graph["release_id"], "node_ids": [row["id"] for row in graph["nodes"]],
               "edge_ids": [row["id"] for row in graph["edges"]], "filters": {"scope": "selected_maximum_graph"}, "positions": {}}
    expected = native_expectations(database, payload["node_ids"], payload["edge_ids"], rights)
    report = {"verified_at": datetime.now(UTC).isoformat(), "release_id": graph["release_id"], "case_sha256": spec["case_sha256"],
        "selected_case": name, "graph_caps": {"nodes": 250, "edges": 750}, "actual_selection": {key: len(graph[key]) for key in ("nodes", "edges")},
        "source_manifest_sha256": hashlib.sha256(Path(source_manifest).read_bytes()).hexdigest() if source_manifest else None,
        "browser_offline_execution": {"verified": False, "reason": "file:// browser execution blocked by policy; no bypass attempted"},
        "scope": "Measured HTTP exports plus static payload/rights/CSP/dependency checks; no browser execution or scientific expert validation", "formats": {}, "passed": False}
    bodies, snapshots = {}, {}
    for format in ("json", "csv", "html"):
        request_body = json.dumps({**payload, "format": format}, ensure_ascii=False).encode("utf-8")
        atomic_json(output / (format + "-request.json"), {**payload, "format": format})
        started = time.perf_counter()
        row = {"request_sha256": hashlib.sha256(request_body).hexdigest(), "passed": False}
        try:
            request = urllib.request.Request(base.rstrip("/") + "/api/v1/explorer/export", data=request_body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=50) as response:
                body = response.read()
                row.update(status=response.status, response_release=response.headers.get("X-NutriOmics-Release"), content_type=response.headers.get("Content-Type"))
            row.update(seconds=time.perf_counter() - started, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
            (output / ("maximum-graph." + format)).write_bytes(body)
            bodies[format] = body
            issues = []
            if row["response_release"] != graph["release_id"]:
                issues.append("response_release_mismatch")
            if row["seconds"] > 10:
                issues.append("export_exceeds_10_seconds")
            if format == "json":
                snapshots[format] = json.loads(body)
                issues.extend(inspect_snapshot(snapshots[format], graph, expected))
            elif format == "html":
                snapshots[format], html_issues = html_snapshot(body)
                issues.extend(html_issues)
                issues.extend(inspect_snapshot(snapshots[format], graph, expected))
                if snapshots.get("json") != snapshots[format]:
                    issues.append("html_embedded_snapshot_differs_from_json")
            elif "json" in snapshots:
                issues.extend(inspect_csv(body, snapshots["json"]))
            else:
                issues.append("json_snapshot_unavailable_for_csv_parity")
            row.update(issues=issues, passed=not issues)
        except Exception as error:
            row.update(seconds=time.perf_counter() - started, error_type=type(error).__name__, error=str(error))
        report["formats"][format] = row
        atomic_json(output / "export-validation.json", report)
    report["passed"] = all(row["passed"] for row in report["formats"].values()) and len(report["formats"]) == 3
    atomic_json(output / "export-validation.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("base", "database", "spec", "responses", "output"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--rights")
    parser.add_argument("--source-manifest")
    args = parser.parse_args()
    result = verify(args.base, args.database, args.spec, args.responses, args.output, args.rights, args.source_manifest)
    print(json.dumps({"passed": result["passed"], "formats": result["formats"]}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)
