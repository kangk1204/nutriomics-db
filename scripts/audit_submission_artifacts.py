"""Read-only cross-carrier audit of a release-bound publication candidate.

This checks inventories and packaging, not scientific approval or journal fit.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def performance_rows_match(rows, metrics):
    """Check plotted values, denominators and failure markers, not just counts."""
    try:
        observed = [(r["case"], int(r["measured_requests"]), float(r["p95_seconds"]),
                     float(r["target_seconds"]), int(r["errors"]), r["complete_error_free_series"])
                    for r in rows]
        expected = [(r["id"], r["metrics"]["n"], r["metrics"]["p95_s"], r["target_s"],
                     r["metrics"]["errors"], str(r["metrics"]["n"] == metrics["runs_per_case"]
                         and r["metrics"]["errors"] == 0)) for r in metrics["results"]]
        return observed == expected
    except (KeyError, TypeError, ValueError):
        return False


def audit(metadata, report, manuscript, figures, output):
    metadata, report, manuscript, figures = map(Path, (metadata, report, manuscript, figures))
    data, reporting = load(metadata), load(report)
    text = manuscript.read_text(encoding="utf-8")
    checks, inputs = [], {}

    def check(label, passed, detail=None):
        checks.append({"label": label, "passed": bool(passed), "detail": detail})

    def record(path):
        path = Path(path)
        inputs[str(path.resolve())] = {"bytes": path.stat().st_size, "sha256": digest(path)}

    for path in (metadata, report, manuscript):
        record(path)
    canonical = data["release_contract"]
    public = reporting["public_release"]
    check("report_public_release", public["release_id"] == data["release_id"])
    check("report_public_counts", public["counts"] == canonical["counts"])
    check("report_public_count_definitions", public["count_definitions"] == canonical["count_definitions"])
    check("submission_not_automatically_ready", reporting["journal_submission_ready"] is False)
    check("manuscript_release", data["release_id"] in text)
    counts = data["counts"]
    table = {
        "Foods": counts["node_kinds"]["food"],
        "Composition": counts["measurement"],
        "Study curation": counts["science_kinds"]["study"],
        "Exposures": counts["science_kinds"]["exposure"],
        "Contrasts": counts["science_kinds"]["contrast"],
        "Analysis records": counts["science_kinds"]["analysis_run"],
        "Quantitative results": counts["effect"],
        "Clinical summary candidates": counts["science_kinds"]["clinical_endpoint_result"],
        "Study clinical audits": counts["science_kinds"]["clinical_study_audit"],
        "Regulatory predictions": counts["by_tier"]["predicted"],
    }
    for label, expected in table.items():
        check("manuscript_table:" + label, f"| {label} | {expected:,} |" in text, expected)
    for label, required in {
        "fixed_windows_not_validated_dmrs": "Fixed 1 kb region tests remain distinct from discovered or validated DMRs",
        "pending_scientific_retrieval": "Final scientific retrieval accuracy is currently not evaluated",
        "one_reviewer": "One human reviewer",
        "no_personal_prediction": "not prediction of individual response",
        "version_doi_pending": "version DOIs: pending",
    }.items():
        check("manuscript_limit:" + label, required in text)

    for number in range(1, 6):
        folder = figures / f"F{number}"
        copied_metadata = folder / "dataset-metadata.json"
        record(copied_metadata)
        check(f"F{number}_exact_metadata", digest(copied_metadata) == digest(metadata))
        for suffix in ("png", "pdf", "svg"):
            artifact = folder / f"F{number}.{suffix}"
            record(artifact)
        source = folder / "source-data.csv"
        record(source)
        with source.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if number == 2:
            expected = {
                (panel, key): str(value)
                for panel, field in (("A", "node_kinds"), ("B", "science_kinds"), ("C", "effect_types"))
                for key, value in counts[field].items() if key != "curation"
            }
            observed = {(row["panel"], row["type"]): row["records"] for row in rows}
            check("F2_counts_and_grains", observed == expected and len(rows) == len(expected))
            check("F2_administrative_omission_disclosed", "administrative singleton" in (folder / "legend.txt").read_text(encoding="utf-8"))
        elif number == 5:
            expected = {key: str(value) for key, value in counts["by_tier"].items()}
            observed = {row["layer"]: row["relationship_records"] for row in rows}
            check("F5_tier_counts", observed == expected and len(rows) == len(expected))
        elif number == 4:
            performance = folder / "performance.json"
            if performance.is_file():
                record(performance)
                check("F4_latency_denominators_targets_errors_and_order", performance_rows_match(rows, load(performance)))
            else:
                check("F4_pending_not_measured", len(rows) == 1 and rows[0]["case"] == "fresh_benchmark"
                      and rows[0]["measured_requests"] == "0" and rows[0]["p95_seconds"] == "pending")
    result = {
        "schema_version": "nutriomics-artifact-consistency-v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "release_id": data["release_id"],
        "passed": all(row["passed"] for row in checks),
        "checks": checks, "inputs": inputs,
        "limitations": [
            "Packaging and counts only; visual QA and scientific interpretation are separate.",
            "Does not create human review, scientific gold labels, DOI or public-service approval.",
            "Source manuscript bibliography and author statements require human confirmation.",
        ],
    }
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("metadata", "report", "manuscript", "figures", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = audit(**vars(args))
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"])}))
    raise SystemExit(0 if result["passed"] else 1)
