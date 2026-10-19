"""Static export checks must reject restricted values without executing HTML."""
import copy
import io
import csv
import json

import pytest

from scripts import verify_final_upgrade_exports as validation
from test_explorer_api import explorer as explorer, export  # noqa: F401


def fixture_snapshot(explorer):
    snapshot = export(explorer["client"]).json()
    graph = {"nodes": snapshot["nodes"], "edges": snapshot["edges"], "release_id": snapshot["release_id"]}
    expected = validation.native_expectations(explorer["db"], [row["id"] for row in snapshot["nodes"]],
                                              [row["id"] for row in snapshot["edges"]], None)
    return snapshot, graph, expected


def test_real_export_static_inspection_retains_allowed_values_and_blocks_restricted_sources(explorer):
    snapshot, graph, expected = fixture_snapshot(explorer)
    assert not validation.inspect_snapshot(snapshot, graph, expected)
    html = export(explorer["client"], format="html").content
    embedded, issues = validation.html_snapshot(html)
    assert not issues and embedded == snapshot
    assert not validation.inspect_csv(export(explorer["client"], format="csv").content, snapshot)


@pytest.mark.parametrize("fault,code", [
    ("restricted", "restricted_source_payload_leaked:restricted"),
    ("measurement", "measurement_value_changed:m-open"),
    ("effect", "unapproved_detail_export:effects"),
    ("clinical", "unapproved_clinical_value_export"),
    ("operational", "operational_path_leaked"),
])
def test_snapshot_detector_catches_rights_numeric_and_operating_path_faults(explorer, fault, code):
    snapshot, graph, expected = fixture_snapshot(explorer)
    if fault == "restricted":
        edge = next(row for row in snapshot["edges"] if row["id"] == "restricted")
        edge["context"] = {"secret_original_value": 12345}
    elif fault == "measurement":
        snapshot["node_details"]["food:rice"]["measurements"][0]["amount"] = 98765
    elif fault == "effect":
        snapshot["node_details"]["food:rice"]["effects"] = [{"export_allowed": False}]
    elif fault == "clinical":
        snapshot["node_details"]["food:rice"]["metadata"]["clinical_endpoints"] = [
            {"id": "ClinicalResult:fixture", "endpoint": {}, "source_ids": ["closed"]}]
    else:
        snapshot["node_details"]["food:rice"]["metadata"]["leak"] = "/home/private/research/data"
    assert code in validation.inspect_snapshot(snapshot, graph, expected)


def test_html_checker_detects_network_dependencies_and_does_not_execute_script():
    body = b'''<html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; connect-src 'none'"></head>
    <script>window.__NUTRIOMICS_SNAPSHOT__={"offline":true};</script>
    <script src="https://example.org/runtime.js"></script><script>throw new Error('never execute');</script></html>'''
    snapshot, issues = validation.html_snapshot(body)
    assert snapshot == {"offline": True}
    assert issues == ["external_asset_dependency"]


def test_csv_detector_rejects_changed_value_and_closed_context(explorer):
    snapshot, _, _ = fixture_snapshot(explorer)
    content = export(explorer["client"], format="csv").content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(content)))
    for row in rows:
        if row["record_type"] == "measurement" and row["id"] == "m-open":
            row["amount"] = "999"
        if row["record_type"] == "edge" and row["id"] == "restricted":
            row["context"] = '{"secret":true}'
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader();writer.writerows(rows)
    issues = validation.inspect_csv(stream.getvalue().encode(), snapshot)
    assert "csv_measurement_value_changed:m-open" in issues
    assert "csv_edge_payload_changed:restricted" in issues


def test_selection_uses_actual_largest_measured_maximum_cap_graph(tmp_path):
    spec = {"baseline_release": "r1", "cases": []}
    for name, size, maximum in (("small-max", 2, True), ("large-max", 4, True), ("not-max", 6, False)):
        spec["cases"].append({"id": name, "path": "/api/v1/explorer/graph", "params":
                              {"max_nodes": 250 if maximum else 80, "max_edges": 750 if maximum else 200}})
        (tmp_path / (name + "-response.json")).write_text(json.dumps({"release_id": "r1", "nodes": [{"id": str(i)} for i in range(size)],
                                                                    "edges": [{"id": str(i)} for i in range(size)]}))
    name, graph = validation.selected_graph(spec, tmp_path)
    assert name == "large-max" and len(graph["nodes"]) == 4
