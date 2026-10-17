"""Freeze 50 public foods, then compare 1,200 source quantities with the public API."""
import argparse
import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import requests

from nutrigenomics.composition_policy import convert_unit, normalize_unit
from nutrigenomics.explorer_build import canonical, read_only, sha256
from nutrigenomics.source_updates import atomic_json


def validate(release, base, output):
    release, output = Path(release).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    groups = defaultdict(list)
    with read_only(release / "nutriomics.sqlite3") as db:
        for row in db.execute("SELECT n.id,n.label,n.metadata_json,count(*) n FROM node n "
                              "JOIN measurement m ON m.food=n.id JOIN composition_assessment a ON a.measurement_id=m.id "
                              "WHERE a.eligible=1 GROUP BY n.id HAVING count(*)>=24 ORDER BY n.id"):
            info = json.loads(row["metadata_json"])
            category = info.get("core_metadata", {}).get("food_category") or info.get("data_type") or "source_category_unknown"
            if not isinstance(category, str):
                category = canonical(category)
            groups[category].append({"food_id": row["id"], "label": row["label"]})
        cases = []
        while len(cases) < 50 and any(groups.values()):
            for category in sorted(groups):
                if groups[category] and len(cases) < 50:
                    cases.append({**groups[category].pop(0), "source_category": category,
                                  "amount_g": [50, 100, 150, 250][len(cases) % 4]})
        if len(cases) != 50:
            raise ValueError("Fewer than 50 public foods have 24 eligible source records")
        for case in cases:
            case["source_measurements"] = [dict(r) for r in db.execute(
                "SELECT m.id,m.component,m.amount,m.unit,m.source_id FROM measurement m "
                "JOIN composition_assessment a ON a.measurement_id=m.id "
                "WHERE m.food=? AND a.eligible=1 ORDER BY m.id LIMIT 24", (case["food_id"],))]
        frozen = {"release_id": manifest["release_id"], "database_sha256": manifest["database_sha256"],
                  "frozen_at": datetime.now(UTC).isoformat(), "cases": cases,
                  "selection": "round-robin source categories, source-ID order, before API responses"}
        atomic_json(output / "cases.json", frozen)
        mismatches, errors, checks = [], [], 0
        responses = []
        with requests.Session() as client:
            for case in cases:
                try:
                    response = client.post(base.rstrip("/") + "/api/v1/meals/analyze",
                        json={"items": [{"food_id": case["food_id"], "amount_g": case["amount_g"]}]}, timeout=20)
                    response.raise_for_status()
                    data = response.json()
                    if data.get("release_id") != manifest["release_id"]:
                        raise ValueError("response release mismatch")
                    rows = {r["measurement_id"]: r for r in data["provenance"]}
                    for source in case["source_measurements"]:
                        observed = rows.get(source["id"])
                        unit = observed.get("unit") if observed else normalize_unit(source["unit"])
                        converted = convert_unit(source["amount"], source["unit"], unit)
                        expected = converted * case["amount_g"] / 100 if converted is not None else None
                        checks += 1
                        if (not observed or observed.get("source_id") != source["source_id"]
                                or expected is None or not math.isclose(observed["contribution"], expected, rel_tol=1e-10, abs_tol=1e-9)):
                            mismatches.append({"food_id": case["food_id"], "measurement_id": source["id"],
                                               "expected": expected, "observed": observed})
                    responses.append({"food_id": case["food_id"], "status": response.status_code,
                                      "seconds": response.elapsed.total_seconds(), "source_checks": 24})
                except (requests.RequestException, ValueError, KeyError) as exc:
                    errors.append({"food_id": case["food_id"], "error": type(exc).__name__})
                atomic_json(output / "progress.json", {"completed_foods": len(responses) + len(errors),
                    "checked_source_values": checks, "mismatches": mismatches, "errors": errors})
    result = {"release_id": manifest["release_id"], "database_sha256": manifest["database_sha256"],
              "verified_at": datetime.now(UTC).isoformat(), "case_sha256": sha256(output / "cases.json"),
              "food_count": len(responses), "composition_count": checks, "mismatches": mismatches,
              "errors": errors, "responses": responses,
              "passed": len(responses) == 50 and checks == 1200 and not mismatches and not errors,
              "validation_scope": "public source quantity preservation and arithmetic; not expert derivation review"}
    atomic_json(output / "public-composition.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("release", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    result = validate(args.release, args.base, args.output)
    print(json.dumps({k: result[k] for k in ("passed", "food_count", "composition_count", "errors")}))
    raise SystemExit(0 if result["passed"] else 1)
