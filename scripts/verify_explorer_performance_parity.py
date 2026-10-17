"""Full original-table equality, 50-food calculations and frozen response parity."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import urllib.request
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from scripts.benchmark_explorer_performance import digest
from scripts.performance_release_gate import sha


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def table_digest(db, table):
    info = db.execute("PRAGMA table_info(" + identifier(table) + ")").fetchall()
    columns = [row[1] for row in info]
    row_hashes = []
    # Scan physical rows sequentially. ORDER BY a random text PK makes SQLite
    # visit millions of scattered pages. Sorting compact row digests preserves
    # exact multiset equality (including duplicates) without sorting large rows.
    for row in db.execute("SELECT * FROM " + identifier(table)):
        h = hashlib.sha256()
        # Preserve binary values as bytes; no unit/statistical value transformation.
        for value in row:
            b = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            h.update(type(value).__name__.encode() + b"\0")
            h.update(len(b).to_bytes(8, "big"))
            h.update(b)
        row_hashes.append(h.digest())
    row_hashes.sort()
    h = hashlib.sha256()
    for value in row_hashes:
        h.update(value)
    return {"columns":columns, "rows":len(row_hashes), "sha256":h.hexdigest(),
            "algorithm":"sorted-typed-row-sha256-v2"}


ADDITIVE_FIELDS = {"release_id", "next_cursor", "effects_status", "source_effects_status"}


def scientific_payload(value):
    """Ignore only declared additive protocol fields, never values or provenance."""
    if isinstance(value, dict):
        return {k:scientific_payload(v) for k,v in value.items() if k not in ADDITIVE_FIELDS}
    if isinstance(value, list):
        return [scientific_payload(v) for v in value]
    return value


def verify(parent, candidate, baseline, measured, base, cases_path, manifest_path, output):
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    differences = []
    tables = {}
    def checkpoint(phase, **extra):
        report = {"verified_at": datetime.now(UTC).isoformat(), "status": "in_progress",
                  "phase": phase, "mismatches": differences, "table_comparisons": tables, **extra}
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    with closing(sqlite3.connect(Path(parent).resolve().as_uri()+"?mode=ro",uri=True)) as old, closing(sqlite3.connect(Path(candidate).resolve().as_uri()+"?mode=ro",uri=True)) as new:
        for db in (old, new):
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
        release = json.loads(new.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()[0])
        names = [r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='meta' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'perf_%' ORDER BY name")]
        for name in names:
            before, after = table_digest(old, name), table_digest(new, name)
            tables[name] = {"before":before, "after":after, "equal":before==after}
            if before != after:
                differences.append("table:" + name)
            checkpoint("table_parity", completed_tables=len(tables))
        old.row_factory = sqlite3.Row
        foods = list(old.execute("SELECT * FROM node WHERE kind='food' AND id LIKE 'K-FIND:%' ORDER BY id"))
        expected, inputs, total = defaultdict(float), [], 0
        components = {r["id"]:json.loads(r["metadata_json"]).get("component_key",r["id"]) for r in old.execute("SELECT * FROM node WHERE kind='nutrient'")}
        for i, food in enumerate(foods):
            metadata = json.loads(food["metadata_json"])
            grams = 50 * (i % 5 + 1)
            inputs.append({"food_id":metadata["core_food_id"], "amount_g":grams})
            for m in old.execute("SELECT * FROM measurement WHERE food=?",(food["id"],)):
                total += 1
                if m["basis"] != "source_100g":
                    differences.append("food_basis:" + m["id"])
                elif m["amount"] is not None:
                    expected[(components[m["component"]],m["unit"])] += m["amount"] * grams / 100
        req = urllib.request.Request(base+"/api/v1/meals/analyze", data=json.dumps({"items":inputs}).encode(), headers={"Content-Type":"application/json"})
        checkpoint("meal_calculation", food_count=len(foods), composition_count=total,
                   all_original_tables_equal=all(v["equal"] for v in tables.values()))
        try:
            with urllib.request.urlopen(req,timeout=45) as response:
                meal = json.load(response)
        except Exception as exc:
            checkpoint("meal_calculation", status="failed", error_type=type(exc).__name__,
                       error=str(exc), food_count=len(foods), composition_count=total,
                       all_original_tables_equal=all(v["equal"] for v in tables.values()))
            raise
        actual = {(v["component_key"],v["unit"]):v["amount"] for v in meal["components"]}
        for key, value in expected.items():
            if key not in actual or not math.isclose(actual[key],value,rel_tol=1e-9,abs_tol=1e-8):
                differences.append("food_calculation:" + str(key))
        if len(meal["items"]) != len(foods) or set(actual) != set(expected):
            differences.append("food_calculation_scope")
    responses = []
    for case in cases["cases"]:
        paths = [Path(directory)/(case["id"]+"-response.json") for directory in (baseline, measured)]
        if not all(p.is_file() for p in paths):
            differences.append("missing_response:"+case["id"])
            responses.append({"case":case["id"],"equal":False,"status":"missing_response"})
            continue
        a,b = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        equal = scientific_payload(a) == scientific_payload(b)
        responses.append({"case":case["id"],"equal":equal,"before_sha256":digest(scientific_payload(a)),"after_sha256":digest(scientific_payload(b))})
        if not equal:
            differences.append("response:"+case["id"])
    report = {"verified_at":datetime.now(UTC).isoformat(),"release_id":release,"source_manifest_sha256":sha(manifest_path),"case_sha256":cases["case_sha256"],"food_count":len(foods),"composition_count":total,"mismatches":differences,"all_original_tables_equal":all(v["equal"] for v in tables.values()),"all_frozen_responses_equal":all(v["equal"] for v in responses),"table_comparisons":tables,"responses":responses,"calculated_totals":meal["components"]}
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    return report


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("parent", "candidate", "baseline", "measured", "cases", "manifest", "output"):
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--base",default="http://127.0.0.1:8020")
    a=p.parse_args()
    r=verify(a.parent,a.candidate,a.baseline,a.measured,a.base,a.cases,a.manifest,a.output)
    print(json.dumps({k:r[k] for k in ("release_id","food_count","composition_count","mismatches")},indent=2))
    raise SystemExit(1 if r["mismatches"] else 0)
