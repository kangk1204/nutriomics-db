"""Read-only parent/value preservation with exact, source-bound clinical additions.

Large tables are physical sequential scans, with compact sorted typed row hashes.
No per-result lookup, source DB write, expert approval or timing sample is created.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import struct
import statistics
import platform
import time
import urllib.request
from collections import Counter, defaultdict
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path

from nutrigenomics.clinical_endpoints import attach_clinical_endpoints, load_clinical_config
from nutrigenomics.composition_policy import convert_unit, normalize_unit
from nutrigenomics.explorer_build import SCHEMA, canonical, read_only, sha256
from nutrigenomics.explorer_performance import fingerprint
from nutrigenomics.kfind import NUTRIENT_CROSSWALK, food_identity, parse_source_value, validate_selection
from nutrigenomics.publication import KEY_FIELDS
from nutrigenomics.source_updates import atomic_json
from nutrigenomics.workbench_build import WORKBENCH_SCHEMA
from scripts.verify_explorer_performance_parity import identifier

ADDITIVE = {"edge", "node", "science_record", "source", "workbench_evidence", "node_domain", "edge_domain"}
REBUILT = {"meta", "node_kind", "perf_adj", "perf_exposure_match", "perf_search", "perf_search_content",
           "perf_search_data", "perf_search_idx", "perf_search_docsize", "perf_search_config"}
ALGORITHM = "sorted-canonical-binary-sqlite-row-sha256-v3"
BINARY_ROW_FORMAT = {"null": "N", "integer": "I + signed int64 big-endian (>q)",
    "real": "F + IEEE754 binary64 big-endian (>d)", "text": "S + uint64 UTF8 byte length big-endian + exact UTF8 bytes",
    "blob": "B + uint64 byte length big-endian + exact bytes",
    "aggregate": "SHA256(concatenated sorted 32-byte row SHA256 values); duplicates retained; columns/schema compared separately"}


def _names(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(db, table):
    return [r[1] for r in db.execute("PRAGMA table_info(" + identifier(table) + ")")]


def _digest(columns, rows):
    """Versioned canonical SQLite scalar bytes, without per-value JSON calls.

    SQL NULL, INTEGER, REAL, TEXT and BLOB retain separate tags. Integers use
    signed big-endian int64, reals use their big-endian binary64 bits, variable
    strings/blobs use uint64 byte lengths. No normalization, pickle memoization,
    numeric conversion, or duplicate removal is permitted. Column order is part
    of the returned comparison record, not embedded into each row hash.
    """
    hashes = []
    for row in rows:
        h = hashlib.sha256()
        for value in row:
            if value is None:
                h.update(b"N")
            elif type(value) is int:
                h.update(b"I" + struct.pack(">q", value))
            elif type(value) is float:
                h.update(b"F" + struct.pack(">d", value))
            elif type(value) is str:
                raw = value.encode("utf-8")
                h.update(b"S" + struct.pack(">Q", len(raw)))
                h.update(raw)
            elif type(value) is bytes:
                h.update(b"B" + struct.pack(">Q", len(value)))
                h.update(value)
            else:
                raise TypeError("Unsupported SQLite scalar in binary row digest: " + type(value).__name__)
        hashes.append(h.digest())
    hashes.sort()
    result = hashlib.sha256()
    for value in hashes:
        result.update(value)
    return {"columns": columns, "rows": len(hashes), "sha256": result.hexdigest(), "algorithm": ALGORITHM}


def _scan_digest(db, table):
    return _digest(_columns(db, table), db.execute("SELECT * FROM " + identifier(table)))


def digest_microbenchmark(rows=10000, repeats=3):
    """Measure old JSON v2 versus binary v3 on one synthetic SQLite table.

    This is serializer overhead evidence, not a production database throughput
    or HTTP latency claim. Alternate call order and report every actual timing.
    """
    from scripts.verify_explorer_performance_parity import table_digest as old_digest
    if rows != 10000 or repeats < 3:
        raise ValueError("The fixed digest microbenchmark uses 10,000 rows and at least three repeats")
    measurements = {"json_v2": [], "binary_v3": []}
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE x(id TEXT,series TEXT,feature_type TEXT,feature_id TEXT,contrast TEXT,analysis_version TEXT,"
                   "effect REAL,se REAL,p REAL,q REAL,n_people INTEGER,n_samples INTEGER,unit TEXT,status TEXT,metadata TEXT,blob BLOB)")
        db.executemany("INSERT INTO x VALUES(" + ",".join("?" for _ in range(16)) + ")", (
            (str(i), "GSE_fixture", "cpg" if i % 2 else "gene", "feature:" + str(i), "paired change", "fixture-v1",
             None if i % 7 == 0 else -.125, .03125, .08, .72, 13, 26, "delta beta", "reported",
             '{"source":"synthetic 반복 β provenance","interpretation":"not clinical validation"}' * 8,
             b"\x00\x01") for i in range(rows)))
        methods = {"json_v2": old_digest, "binary_v3": _scan_digest}
        digests = {name: set() for name in methods}
        for repeat in range(repeats):
            for name in (("json_v2", "binary_v3") if repeat % 2 == 0 else ("binary_v3", "json_v2")):
                started = time.perf_counter()
                receipt = methods[name](db, "x")
                measurements[name].append(time.perf_counter() - started)
                digests[name].add(receipt["sha256"])
    old_time, new_time = (statistics.median(measurements[name]) for name in ("json_v2", "binary_v3"))
    return {"rows": rows, "columns": 16, "repeats": repeats, "seconds": measurements,
        "median_json_v2_s": old_time, "median_binary_v3_s": new_time, "speedup": old_time / new_time,
        "actual_improvement_observed": new_time < old_time,
        "stable_within_each_algorithm": all(len(values) == 1 for values in digests.values()),
        "algorithm": ALGORITHM, "binary_row_format": BINARY_ROW_FORMAT,
        "platform": platform.platform(), "python_version": platform.python_version(),
        "scope": "Synthetic in-memory SQLite serialization comparison, not production or biological validation"}


def _expected_delta(parent, config):
    """Replay only the small allowed layer against original registry/exposure keys.

    This checks exact additive producer/config agreement, not independent science.
    No full graph/effect copy or large in-memory entity registry is needed.
    """
    expected = sqlite3.connect(":memory:")
    expected.row_factory = sqlite3.Row
    expected.executescript(SCHEMA + "CREATE TABLE science_record(id TEXT,kind TEXT,metadata_json TEXT,PRIMARY KEY(kind,id));")
    parent_names = _names(parent)
    if "workbench_evidence" in parent_names:
        expected.executescript(WORKBENCH_SCHEMA)
    science = list(parent.execute("SELECT id,kind,metadata_json FROM science_record"))
    expected.executemany("INSERT INTO science_record VALUES(?,?,?)", science)
    expected.executemany("INSERT INTO source VALUES(?,?)", parent.execute("SELECT * FROM source"))
    exposure_ids = ["Exposure:" + str(r[0]) for r in science if r[1] == "exposure"]
    inherited_nodes = []
    if exposure_ids:
        inherited_nodes = list(parent.execute("SELECT * FROM node WHERE id IN (" + ",".join("?" for _ in exposure_ids) + ")", exposure_ids))
        expected.executemany("INSERT INTO node VALUES(" + ",".join("?" for _ in inherited_nodes[0]) + ")", inherited_nodes) if inherited_nodes else None
    before = {t: {tuple(r) for r in expected.execute("SELECT * FROM " + identifier(t))}
              for t in ("science_record", "source")}
    original_node_ids = {r[0] for r in inherited_nodes}
    summary = attach_clinical_endpoints(expected, config)
    delta = {}
    for table in ADDITIVE:
        if table not in _names(expected):
            continue
        rows = [tuple(r) for r in expected.execute("SELECT * FROM " + identifier(table))]
        if table in before:
            rows = [r for r in rows if r not in before[table]]
        if table == "node":
            rows = [r for r in rows if r[0] not in original_node_ids]
        delta[table] = rows
    overrides = {r[0]: tuple(r) for r in expected.execute("SELECT * FROM node") if r[0] in original_node_ids}
    columns = _columns(parent, "node")
    for row in inherited_nodes:
        altered = overrides[row[0]]
        if any(row[i] != altered[i] for i, name in enumerate(columns) if name not in {"degree", "domains_json"}):
            raise ValueError("Clinical producer changed an inherited node beyond permitted degree/domains")
    expected.close()
    return delta, overrides, summary


def _additive_comparison(old, new, table, allowed_rows, overrides=None, observe=None):
    cols, other = _columns(old, table), _columns(new, table)
    if cols != other:
        return {"equal": False, "reason": "column_schema_changed", "before_columns": cols, "after_columns": other}
    key_indexes = [cols.index(k) for k in (("kind", "id") if table == "science_record" else
                                         ("node_id", "domain") if table == "node_domain" else
                                         ("edge_id", "domain") if table == "edge_domain" else ("id",))]
    key = lambda row: tuple(row[i] for i in key_indexes)
    permitted = {key(r): tuple(r) for r in allowed_rows}
    node = table == "node"
    keep = [i for i, column in enumerate(cols) if not (node and column == "degree")]
    projected_cols = [cols[i] for i in keep]
    def original_rows():
        for native in old.execute("SELECT * FROM " + identifier(table)):
            row = tuple(native)
            k = key(row)
            if k in permitted:
                if table not in {"node_domain", "edge_domain"}:
                    raise ValueError("Allowed new clinical ID already exists in parent: " + str(k))
                permitted.pop(k)
            if node and overrides and row[cols.index("id")] in overrides:
                adjusted = list(row)
                adjusted[cols.index("domains_json")] = overrides[row[cols.index("id")]][cols.index("domains_json")]
                row = tuple(adjusted)
            yield tuple(row[i] for i in keep)
    before = _digest(projected_cols, original_rows())
    added, total = [], 0
    def final_rows():
        nonlocal total
        for native in new.execute("SELECT * FROM " + identifier(table)):
            row = tuple(native)
            total += 1
            if observe:
                observe(cols, row)
            if key(row) in permitted:
                added.append(row)
                continue
            yield tuple(row[i] for i in keep)
    after = _digest(projected_cols, final_rows())
    expected_added = _digest(cols, permitted.values())
    actual_added = _digest(cols, added)
    return {"equal": before == after and expected_added == actual_added, "before": before, "after_inherited": after,
        "expected_additions": expected_added, "actual_additions": actual_added,
        "after_total_rows": total,
        "allowed_inherited_node_changes": "exact clinical domain union and independently recomputed degree" if node else None}


def _nonempty_wal(path):
    wal = Path(str(path) + "-wal")
    return wal.is_symlink() or (wal.exists() and wal.stat().st_size > 0)


def _post_meal(base, items):
    payload = {"items": items}
    request = urllib.request.Request(base.rstrip("/") + "/api/v1/meals/analyze", data=canonical(payload).encode(),
        headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=45) as response:
        value = json.load(response)
        receipt = {"http_status": response.status, "seconds": time.perf_counter() - started,
                   "items": len(items), "payload_sha256": hashlib.sha256(request.data).hexdigest()}
    return value, receipt


def _api_meta(base, release):
    with urllib.request.urlopen(base.rstrip("/") + "/api/v1/explorer/meta", timeout=15) as response:
        value = json.load(response)
    if value.get("release_id") != release:
        raise ValueError("Live Explorer release differs from the validated candidate")


def _food_validation(old, base, release, checkpoint, selection):
    selected = validate_selection(json.loads(Path(selection).read_text(encoding="utf-8")))
    food_ids = [food_identity(str(r["dataset_id"]), r["food_code"], r["food_name"])[0] for r in selected]
    foods = {r[0]: json.loads(r[1]) for r in old.execute("SELECT id,metadata_json FROM node WHERE kind='food' AND id LIKE 'K-FIND:%'")}
    if len(food_ids) != 50 or set(food_ids) != foods.keys():
        raise ValueError("The exact fixed K-FIND panel must contain fifty distinct source-identified foods")
    measurement_columns = _columns(old, "measurement")
    rows = [dict(zip(measurement_columns, row)) for row in old.execute(
        "SELECT * FROM measurement WHERE food IN (" + ",".join("?" for _ in food_ids) + ")", food_ids)]
    if len(rows) != 1200:
        raise ValueError("The fixed K-FIND panel must contain 1,200 composition records")
    components = {r[0]: json.loads(r[1]) for r in old.execute("SELECT id,metadata_json FROM node WHERE kind='nutrient'")}
    by_food = defaultdict(list)
    issues, checked = [], 0
    expected, inputs, expected_sources, expected_unknown = {}, [], {}, {}
    for row in rows:
        by_food[row["food"]].append(row)
    for index, food_id in enumerate(food_ids):
        metadata = foods[food_id]
        core_id = metadata.get("core_food_id")
        if not isinstance(core_id, int) or isinstance(core_id, bool):
            raise ValueError("Fixed food has no verified legacy core food identifier")
        grams = 80 + 3 * index
        inputs.append({"food_id": core_id, "amount_g": grams})
        totals, seen, sources, unknown = defaultdict(float), set(), defaultdict(set), set()
        for row in by_food[food_id]:
            raw = json.loads(row["raw_json"])
            field = raw.get("provider_nutrient_field")
            if field not in NUTRIENT_CROSSWALK or field in seen or "provider_value_raw" not in raw:
                raise ValueError("Missing/duplicate fixed K-FIND source nutrient text")
            seen.add(field)
            definition = NUTRIENT_CROSSWALK[field]
            parsed = parse_source_value(raw["provider_value_raw"])
            amount = float(parsed.amount) if parsed.amount is not None else None
            info = components.get(row["component"], {})
            component_key = info.get("component_key")
            if (row["amount"] != amount or row["status"] != parsed.status or row["basis"] != "source_100g"
                    or normalize_unit(row["unit"]) != normalize_unit(definition["unit"])
                    or component_key != "FDC-NUTRIENT:" + definition["fdc_id"]):
                issues.append({"food": food_id, "measurement": row["id"], "code": "raw_source_value_unit_basis_status_or_identity"})
            checked += 1
            if amount is not None:
                unit = normalize_unit(info.get("canonical_unit") or row["unit"])
                converted = convert_unit(amount, row["unit"], unit)
                if converted is None:
                    raise ValueError("Fixed source quantity has no validated unit conversion")
                totals[(component_key, unit)] += converted * grams / 100
                if not raw.get("source_ref") or not raw.get("release") or not metadata.get("core_source_id"):
                    raise ValueError("Fixed K-FIND source reference/release is missing")
                sources[(component_key, unit)].add((metadata["core_source_id"], raw["source_ref"], raw["release"], row["basis"]))
            else:
                unknown.add((core_id, component_key, parsed.status))
        if len(seen) != len(NUTRIENT_CROSSWALK):
            raise ValueError("Fixed food does not retain all 24 nutrient source states")
        expected[food_id] = totals
        expected_sources[food_id] = sources
        expected_unknown[food_id] = unknown
    state = {"status": "source_values_checked", "food_count": 50, "composition_count": checked,
        "source_values_match": not issues, "differences": issues, "api_checks": [], "completed_foods": 0,
        "selection_sha256": sha256(Path(selection)), "individual_response_validated": False,
        "scope": "Frozen original provider_value_raw text, composition states and arithmetic; preparation/edible-state scientific review remains pending"}
    checkpoint("food_source_values", food_validation=state)
    if issues:
        state["status"] = "failed"
        return state
    def compare(data, totals, wanted_items, sources, unknown):
        actual = {(r["component_key"], normalize_unit(r["unit"])): r["amount"] for r in data.get("components", [])}
        actual_items = [(r.get("food_id"), r.get("amount_g")) for r in data.get("items", [])]
        if (actual_items != [(r["food_id"], r["amount_g"]) for r in wanted_items]
                or len(actual) != len(data.get("components", [])) or set(actual) != set(totals)):
            raise ValueError("API food/component scope differs from fixed source values")
        if data.get("individual_prediction") is not False:
            raise ValueError("API did not preserve the absence of validated individual prediction")
        if data.get("release_id") not in {None, release}:
            raise ValueError("Meal response release changed")
        if any(not math.isclose(actual[key], value, rel_tol=1e-10, abs_tol=1e-8) for key, value in totals.items()):
            raise ValueError("API source-value weighted sum differs")
        for component in data.get("components", []):
            key = (component["component_key"], normalize_unit(component["unit"]))
            observed = {(r.get("source_id"), r.get("source_ref"), r.get("release"), r.get("basis"))
                        for r in component.get("source_records", [])}
            if observed != sources[key]:
                raise ValueError("API source references or release permissions provenance differ")
        observed_unknown = {(r.get("food_id"), r.get("component_key"), r.get("status")) for r in data.get("unquantified", [])}
        if observed_unknown != unknown:
            raise ValueError("API missing/detection states differ from the original source")
    try:
        _api_meta(base, release)
        batch_totals, batch_sources, batch_unknown = defaultdict(float), defaultdict(set), set()
        for food_id, item in zip(food_ids, inputs):
            value, receipt = _post_meal(base, [item])
            compare(value, expected[food_id], [item], expected_sources[food_id], expected_unknown[food_id])
            state["api_checks"].append({"food_id": food_id, **receipt})
            state["completed_foods"] += 1
            for key, amount in expected[food_id].items():
                batch_totals[key] += amount
                batch_sources[key].update(expected_sources[food_id][key])
            batch_unknown.update(expected_unknown[food_id])
            checkpoint("food_single_api", food_validation=state)
        value, receipt = _post_meal(base, inputs)
        compare(value, batch_totals, inputs, batch_sources, batch_unknown)
        state["api_checks"].append({"name": "all50_batch", **receipt})
        state["all50_batch_passed"] = True
        _api_meta(base, release)
        state["status"] = "passed"
        state["release_binding"] = "Explorer metadata before/after plus unchanged frozen core fingerprint; legacy meal API may omit release_id"
    except Exception as exc:
        state.update(status="failed", error_type=type(exc).__name__, error=str(exc))
    checkpoint("food_api_complete", food_validation=state)
    return state


def validate(parent, release, output, *, public=False, base=None, clinical_config=None,
             food_selection=None):
    parent, release, output = (Path(p).resolve() for p in (parent, release, output))
    if output.is_relative_to(parent) or output.is_relative_to(release):
        raise ValueError("Validation evidence must be outside both immutable releases")
    filename = "nutriomics.sqlite3" if public else "explorer.sqlite"
    paths = [parent / filename, release / filename]
    report = {"status": "in_progress", "passed": False, "phase": "initializing", "public": public,
        "table_comparisons": {}, "differences": [], "five_part_effect_key": list(KEY_FIELDS),
        "expert_semantic_accuracy": "not_evaluated", "source_databases_modified": False,
        "memory_policy": "128 MiB SQLite cache per source; one compact sorted row-hash list at a time, no full value matrix or parent graph copy",
        "digest_algorithm": ALGORITHM, "binary_row_format": BINARY_ROW_FORMAT,
        "scope": "Exact scientific value/permission preservation and explicitly allowed clinical additions; not biological validity"}
    def checkpoint(phase, **extra):
        report.update(phase=phase, verified_at=datetime.now(UTC).isoformat(), **extra)
        atomic_json(output, report)
    checkpoint("input_verification")
    try:
        identities = [fingerprint(p) for p in paths]
        manifests = [json.loads((p / "manifest.json").read_text(encoding="utf-8")) for p in (parent, release)]
        for path, manifest in zip(paths, manifests):
            if _nonempty_wal(path) or sha256(path) != manifest["database_sha256"]:
                raise ValueError("Immutable input SHA or nonempty WAL check failed")
        report.update(parent_release_id=manifests[0]["release_id"], release_id=manifests[1]["release_id"],
            parent_database_sha256=manifests[0]["database_sha256"], database_sha256=manifests[1]["database_sha256"])
        if (manifests[1].get("parent_release_id") != manifests[0]["release_id"] or
                manifests[1].get("parent_database_sha256") != manifests[0]["database_sha256"]):
            report["differences"].append({"code": "immediate_parent_manifest_binding_mismatch"})
        config_path = Path(clinical_config) if clinical_config else release / "clinical-endpoints.json"
        config = load_clinical_config(config_path)
        report["clinical_config_sha256"] = sha256(config_path)
        with ExitStack() as stack:
            old, new = [stack.enter_context(closing(read_only(p))) for p in paths]
            for db in (old, new):
                db.execute("PRAGMA cache_size=-131072")
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
            names, after_names = _names(old), _names(new)
            unknown_new = after_names - names - REBUILT
            if unknown_new:
                report["differences"].append({"code": "unexpected_new_tables", "tables": sorted(unknown_new)})
            delta, overrides, summary = _expected_delta(old, config)
            report["allowed_clinical_additions"] = summary
            immutable = sorted(names - ADDITIVE - REBUILT)
            for table in immutable:
                checkpoint("table_scan", current_table=table)
                if table not in after_names:
                    result = {"equal": False, "reason": "inherited_table_missing"}
                else:
                    before, after = _scan_digest(old, table), _scan_digest(new, table)
                    schemas = [list(map(tuple, db.execute("PRAGMA table_info(" + identifier(table) + ")"))) for db in (old, new)]
                    result = {"equal": before == after and schemas[0] == schemas[1], "before": before, "after": after,
                              "schema_equal": schemas[0] == schemas[1]}
                report["table_comparisons"][table] = result
                if not result["equal"]:
                    report["differences"].append({"code": "inherited_table_changed", "table": table})
                checkpoint("table_complete", current_table=table)
            degree, kinds, relation_tiers, science_kinds = Counter(), Counter(), Counter(), Counter()
            degree_errors = []
            def observe_edges(cols, row):
                degree[row[cols.index("subject")]] += 1
                degree[row[cols.index("object")]] += 1
                relation_tiers[row[cols.index("tier")]] += 1
            def observe_nodes(cols, row):
                kinds[row[cols.index("kind")]] += 1
                if row[cols.index("degree")] != degree.get(row[cols.index("id")], 0) and len(degree_errors) < 50:
                    degree_errors.append(row[cols.index("id")])
            def observe_science(cols, row):
                science_kinds[row[cols.index("kind")]] += 1
            for table in ("edge", "node", "science_record", "source", "workbench_evidence", "node_domain", "edge_domain"):
                if table not in names:
                    continue
                checkpoint("additive_table_scan", current_table=table)
                callback = {"edge": observe_edges, "node": observe_nodes, "science_record": observe_science}.get(table)
                result = (_additive_comparison(old, new, table, delta.get(table, []), overrides, callback)
                          if table in after_names else {"equal": False, "reason": "inherited_table_missing"})
                report["table_comparisons"][table] = result
                if not result["equal"]:
                    report["differences"].append({"code": "clinical_delta_or_inherited_rows_changed", "table": table})
                checkpoint("additive_table_complete", current_table=table)
            if degree_errors:
                report["differences"].append({"code": "node_degree_not_recomputed_from_actual_edges", "first_node_ids": degree_errors})
            metas = [{r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value_json FROM meta")} for db in (old, new)]
            current = metas[1]
            counts = current.get("counts", {})
            if any(current.get(k) != report["release_id"] for k in ("release_id", "dataset_release_id", "service_build_id")):
                report["differences"].append({"code": "current_release_alias_mismatch"})
            if (counts.get("by_kind") != dict(kinds) or counts.get("node_kinds") != dict(kinds)
                    or counts.get("science_kinds") != dict(science_kinds) or counts.get("by_tier") != dict(relation_tiers)):
                report["differences"].append({"code": "canonical_counts_differ_from_actual_tables"})
            for key in ("five_part_effect_key", "gene_identifier_bridge", "candidate_layers", "public_reference_policy"):
                if key in metas[0] and metas[0][key] != current.get(key):
                    report["differences"].append({"code": "scientific_metadata_changed", "key": key})
            if "performance" in metas[0] and metas[0]["performance"].get("core_fingerprint") != current.get("performance", {}).get("core_fingerprint"):
                report["differences"].append({"code": "quantitative_source_fingerprint_changed"})
            report["rebuilt_tables"] = sorted(after_names & REBUILT)
            report["all_original_values_and_allowed_additions_match"] = not report["differences"]
            checkpoint("all_database_values_checked")
            if base and not public and not report["differences"]:
                selection = Path(food_selection) if food_selection else Path(__file__).resolve().parents[1] / "config/kfind_food_selection.json"
                food = _food_validation(old, base, report["release_id"], checkpoint, selection)
                if food["status"] != "passed":
                    report["differences"].append({"code": "fixed_food_api_validation_failed"})
            else:
                report["food_validation"] = {"status": "separate_public_source_validation_required" if public else "not_requested_or_database_validation_failed"}
        if any(fingerprint(p) != identity or _nonempty_wal(p) for p, identity in zip(paths, identities)):
            report["differences"].append({"code": "immutable_input_changed_during_validation"})
        report["passed"] = not report["differences"]
        report["full_internal_validation_complete"] = bool(report["passed"] and not public and base
            and report.get("food_validation", {}).get("status") == "passed")
        checkpoint("complete", status="passed" if report["passed"] else "failed")
    except Exception as exc:
        checkpoint(report["phase"], status="failed", passed=False, error_type=type(exc).__name__, error=str(exc))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("parent", "release", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--public", action="store_true")
    parser.add_argument("--base")
    parser.add_argument("--clinical-config", type=Path)
    parser.add_argument("--food-selection", type=Path)
    args = parser.parse_args()
    result = validate(args.parent, args.release, args.output, public=args.public, base=args.base,
                      clinical_config=args.clinical_config, food_selection=args.food_selection)
    print(json.dumps({key: result.get(key) for key in ("status", "passed", "release_id", "differences", "error")}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
