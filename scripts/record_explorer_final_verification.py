"""Consolidate inspectable receipts; do not replace browser tests with static checks."""

import hashlib
import json
import re
from pathlib import Path


def main():
    workspace = Path(__file__).resolve().parents[1]
    output = workspace / "outputs/explorer"
    verification = output / "verification"
    selected = output / "NutriOmics_Explorer_selected.html"
    html = selected.read_text(encoding="utf-8")
    match = re.search(r"<script>window\.__NUTRIOMICS_SNAPSHOT__=(.*?);</script>", html, re.DOTALL)
    assert match
    snapshot = json.loads(match.group(1))
    assert snapshot["release_id"] == "explorer-20261003-v3" and snapshot["offline"]
    assert snapshot["filters"]["domain"] == "all"
    assert snapshot["filters"]["include_extracted"] is False
    assert any(n["label"] == "흰쌀밥" for n in snapshot["nodes"])
    assert len(snapshot["nodes"]) == len(snapshot["positions"]) == 80
    assert len(snapshot["edges"]) == 200
    assert len({n["id"] for n in snapshot["nodes"]}) == 80
    performance = json.loads(
        (verification / "production-v3/api-performance.json").read_text(encoding="utf-8")
    )
    assert performance["all_p95_under_1s"]
    parity = json.loads(
        (verification / "production-v3/food50-api-parity.json").read_text(encoding="utf-8")
    )
    assert (
        parity["food_count"] == 50
        and parity["composition_count"] == 1200
        and not parity["mismatches"]
    )
    exports = json.loads(
        (output / "final-exports/export-verification.json").read_text(encoding="utf-8")
    )
    for item in exports["examples"]:
        for extension, expected in item["files"].items():
            path = output / f"final-exports/{item['stem']}.{extension}"
            assert path.stat().st_size == expected["bytes"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"]
    runtime = workspace / "nutrigenomics/explorer.py"
    deployed = json.loads((verification / "latest-deployment.json").read_text(encoding="utf-8"))
    assert (
        hashlib.sha256(runtime.read_bytes()).hexdigest()
        == deployed["code_sha256"]["nutrigenomics/explorer.py"]
    )
    archive = workspace / "research/2026-10-03/explorer/deploy/explorer-code-20261003-v3.tar.gz"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == deployed["archive_sha256"]
    receipt = {
        "release_id": "explorer-20261003-v3",
        "api_pid": deployed["api_pid"],
        "production_url": "http://127.0.0.1:18000/explorer/",
        "archive_sha256": deployed["archive_sha256"],
        "targeted_pytest_passed": 92,
        "earlier_complete_suite_passed": 523,
        "typecheck_and_production_and_offline_build": "passed",
        "ui_receipt": {
            "food_search_and_table_selection": "passed",
            "graph_gene_selection_table_detail_sync": "passed",
            "edge_candidate_context_inspected": "passed",
            "expression_and_separate_dnam_annotations_inspected": "passed",
            "component_keyboard_enter_selection": "passed",
            "health_table_selection": "passed",
            "responsive_tabs_and_2_column_path_at_390x844": "passed",
            "console_errors_and_warnings_observed": [],
            "gui_html_download": {
                "status": "saved_in_native_Downloads_and_copied_to_outputs",
                "event_capture": "IAB download event timed out; actual file verified independently",
                "sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
                "nodes": 80,
                "edges": 200,
                "saved_positions": 80,
            },
            "render_measurements_ms": [
                {
                    "seed": "rice",
                    "displayed_nodes": 5,
                    "total_nodes": 80,
                    "layout": 3.2,
                    "data_to_two_animation_frames": 35.8,
                },
                {
                    "seed": "niacin",
                    "displayed_nodes": 4,
                    "total_nodes": 80,
                    "layout": 3.2,
                    "data_to_two_animation_frames": 31.9,
                },
                {
                    "seed": "hypertension",
                    "displayed_nodes": 7,
                    "total_nodes": 80,
                    "layout": 5.1,
                    "data_to_two_animation_frames": 33.4,
                },
                {
                    "seed": "rice_mobile_final",
                    "displayed_nodes": 5,
                    "total_nodes": 80,
                    "layout": 4.9,
                    "data_to_two_animation_frames": 26.7,
                },
            ],
            "render_limit": "Two-animation-frame instrumentation, selected examples; not an all-queries browser benchmark.",
            "final_file_offline_browser_check": "not_completed: CUA blocks file://; no bypass attempted",
        },
        "food_parity": {"foods": 50, "composition_records": 1200, "mismatches": 0},
        "api_performance_receipt": "production-v3/api-performance.json",
        "restore_receipt": "rollback-state.json",
        "active_receipt": "production-state-final.json",
        "full_completion_gate": "offline direct-file/network-block browser validation remains outstanding",
    }
    (verification / "final-verification.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "release_id": receipt["release_id"],
                "api_pid": receipt["api_pid"],
                "exports_match_server_hashes": True,
                "gui_positions": 80,
                "full_completion_gate": receipt["full_completion_gate"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
