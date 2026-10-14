"""Read-only scientific semantics and warm API timing against a pinned release."""

import argparse
import json
import math
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path


def verify(base, database, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)

    def get(path, params=None):
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        with urllib.request.urlopen(url, timeout=120) as r:
            return json.load(r)

    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        refs = dict(db.execute("SELECT layer,count(*) FROM workbench_evidence GROUP BY layer"))
        seed = db.execute(
            "SELECT object_id FROM workbench_evidence WHERE layer='regulatory_predicted' LIMIT 1"
        ).fetchone()[0]
        cpg = db.execute(
            "SELECT subject_id FROM workbench_evidence WHERE layer='eqtm' AND subject_id LIKE 'Illumina:cg%' LIMIT 1"
        ).fetchone()[0]
        cross = db.execute(
            "SELECT subject_id,object_id FROM workbench_evidence WHERE relation='verified_gene_identifier_cross_reference' LIMIT 1"
        ).fetchone()
        contrasts = [
            json.loads(r[0])
            for r in db.execute("SELECT metadata_json FROM science_record WHERE kind='contrast'")
        ]
        yogurt = next(
            c
            for c in contrasts
            if c.get("series_accession") == "GSE98645"
            and c.get("contrast") == "yogurt_vs_milk_6h_change"
        )
    meta = get("/api/v1/explorer/meta")
    clinical = get("/api/v1/explorer/evidence", {"layer": "clinical"})
    assert clinical["total"] == refs["clinical"] == 4
    assert all(r["effect_unit"] == "mmHg" and r["effect"] is not None for r in clinical["items"])
    assert any(
        r["p_value_original"] == "<0.001" and r["p_value"] is None for r in clinical["items"]
    )
    tested = get("/api/v1/explorer/evidence", {"layer": "regulatory_tested"})
    assert tested["total"] == refs["regulatory_tested"]
    assert all(
        r["effect"] is None and r["p_value"] is None and r["q_value"] is None
        for r in tested["items"]
    )
    identity = get("/api/v1/explorer/nodes/" + urllib.parse.quote(cross[0], safe=""))
    assert any(r["object_id"] == cross[1] for r in identity["verified_identifier_links"])
    assert all(r["export_allowed"] is True for r in identity["verified_identifier_links"])
    result_params = {
        "series": yogurt["series_accession"],
        "contrast": yogurt["contrast"],
        "analysis_version": yogurt["analysis_version"],
        "feature_type": "gene_expression",
    }
    omics = get("/api/v1/explorer/omics/results", result_params)
    assert (
        omics["status"] == "available"
        and omics["feature_type"] == "gene"
        and omics["total"] == 19287
    )
    assert all(
        r["feature_type"] == "gene" and r["analysis_key"][1] == "gene" for r in omics["items"]
    )
    absent = get("/api/v1/explorer/omics/results", {**result_params, "feature_type": "cpg"})
    assert absent["status"] == "feature_type_unavailable" and absent["total"] is None
    cases = [
        ("/api/v1/explorer/entities", {"kind": "cpg", "q": cpg.split(":")[1]}),
        ("/api/v1/explorer/entities", {"kind": "regulatory_region", "q": "EH38E"}),
        ("/api/v1/explorer/evidence", {"seed_id": seed, "layer": "regulatory_predicted"}),
        ("/api/v1/explorer/loci", {"seed_id": seed}),
        ("/api/v1/explorer/nodes/" + urllib.parse.quote(cross[0], safe=""), {}),
        ("/api/v1/explorer/graph", {"seed_id": cpg}),
    ]
    results = []
    for path, params in cases:
        get(path, params)
        durations = []
        for _ in range(20):
            t = time.perf_counter()
            data = get(path, params)
            durations.append(time.perf_counter() - t)
            assert data["release_id"] == meta["release_id"]
            if path.endswith("/evidence"):
                assert all(r["source"].get("sha256") and r.get("source_ref") for r in data["items"])
        results.append(
            {
                "endpoint": path,
                "params": params,
                "runs": 20,
                "median_s": statistics.median(durations),
                "p95_s": sorted(durations)[math.ceil(0.95 * len(durations)) - 1],
                "seconds": durations,
            }
        )
    report = {
        "release_id": meta["release_id"],
        "measurement": "server loopback warm; 20 runs per predefined case",
        "layers": refs,
        "semantics": "passed",
        "results": results,
        "all_p95_under_1s": all(r["p95_s"] < 1 for r in results),
        "expert_accuracy": "not_evaluated",
        "doi_assigned": False,
    }
    (output / "workbench-live-validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "results"}))
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="http://127.0.0.1:8020")
    p.add_argument("--database", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    verify(a.base, a.database, a.output)
