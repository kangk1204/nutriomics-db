"""Rebuildable immutable read index; original effects and biological records unchanged."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .explorer_build import canonical, read_only, sha256
from .explorer_performance import fingerprint, selector_key
from .explorer_scientific import RULE_VERSION, decoded, exposure_match_identity, science_records

SCHEMA = """
CREATE TABLE perf_effect_selector(selector TEXT PRIMARY KEY,series TEXT,contrast TEXT,version TEXT,total INTEGER);
CREATE TABLE perf_effect_type(selector TEXT,feature_type TEXT,total INTEGER,PRIMARY KEY(selector,feature_type));
CREATE TABLE perf_effect_ref(selector TEXT,feature_type TEXT,feature_id TEXT,effect_id INTEGER,ordinal_all INTEGER,ordinal_type INTEGER);
CREATE UNIQUE INDEX perf_effect_all ON perf_effect_ref(selector,ordinal_all);
CREATE UNIQUE INDEX perf_effect_page ON perf_effect_ref(selector,feature_type,ordinal_type);
CREATE INDEX perf_effect_feature ON perf_effect_ref(selector,feature_type,feature_id);
CREATE TABLE perf_exposure_match(node_id TEXT,exposure_id TEXT,identity_json TEXT,PRIMARY KEY(node_id,exposure_id));
CREATE TABLE perf_adj(edge_id TEXT PRIMARY KEY,subject TEXT,object TEXT,relation TEXT,tier TEXT,rank_default INTEGER,rank_health INTEGER,rank_gene INTEGER,measurement_id TEXT,usable INTEGER);
CREATE INDEX perf_adj_subject ON perf_adj(subject,rank_default,edge_id);
CREATE INDEX perf_adj_object ON perf_adj(object,rank_default,edge_id);
CREATE INDEX perf_adj_health ON perf_adj(subject,rank_health,edge_id);
CREATE INDEX perf_adj_gene ON perf_adj(subject,rank_gene,edge_id);
CREATE INDEX perf_node_label ON node(kind,label,id);
CREATE INDEX perf_node_degree ON node(kind,degree,id);
CREATE TABLE perf_search_content(rowid INTEGER PRIMARY KEY,node_id TEXT UNIQUE,label TEXT,label_en TEXT,aliases TEXT,identifier TEXT);
CREATE VIRTUAL TABLE perf_search USING fts5(label,label_en,aliases,identifier,content='perf_search_content',content_rowid='rowid',tokenize='trigram',detail=full);
"""


def build_performance_release(parent_dir, core_path, output_dir, release_id, ram_workspace=False, memory_budget_gib=32):
    parent_dir, core_path, output_dir = map(Path, (parent_dir, core_path, output_dir))
    if output_dir.exists():
        raise ValueError("Output exists; immutable releases cannot be overwritten")
    if ram_workspace:
        if os.name != "posix" or not Path("/dev/shm").is_dir():
            raise ValueError("RAM workspace requires Linux tmpfs /dev/shm")
        available = int(next(x.split()[1] for x in Path("/proc/meminfo").read_text().splitlines() if x.startswith("MemAvailable:"))) * 1024
        quota = int(memory_budget_gib) * 1024**3
        estimate = (parent_dir / "explorer.sqlite").stat().st_size * 2
        if quota < estimate or available < quota + 4*1024**3 or shutil.disk_usage("/dev/shm").free < quota:
            raise ValueError("Insufficient bounded RAM workspace; preserve live release")
        workspace = Path(tempfile.mkdtemp(prefix="nutriomics-perf-", dir="/dev/shm"))
        try:
            print(canonical({"phase":"ram_build", "budget_gib":memory_budget_gib}), flush=True)
            manifest = build_performance_release(parent_dir, core_path, workspace/"release", release_id)
            if sum(p.stat().st_size for p in workspace.rglob("*") if p.is_file()) > quota:
                raise RuntimeError("RAM build exceeded declared budget")
            print(canonical({"phase":"sequential_permanent_copy"}), flush=True)
            shutil.copytree(workspace/"release", output_dir)
            if sha256(output_dir/"explorer.sqlite") != manifest["database_sha256"]:
                (output_dir/"FAILED").write_text("Permanent snapshot hash mismatch")
                raise RuntimeError("Permanent snapshot hash mismatch")
            return manifest
        finally:
            # This directory was exclusively created above; never remove shared RAM assets.
            shutil.rmtree(workspace)
    output_dir.mkdir(parents=True)
    destination = output_dir / "explorer.sqlite"
    try:
        parent_fingerprint = fingerprint(parent_dir / "explorer.sqlite")
        print(canonical({"phase":"copy_parent"}), flush=True)
        if parent_fingerprint[2] is None:
            shutil.copyfile(parent_dir / "explorer.sqlite", destination)
            if fingerprint(parent_dir / "explorer.sqlite") != parent_fingerprint:
                raise RuntimeError("Parent changed during immutable copy")
        else:
            with closing(read_only(parent_dir / "explorer.sqlite")) as parent, closing(sqlite3.connect(destination)) as db:
                parent.backup(db)
        with closing(sqlite3.connect(destination)) as db, closing(read_only(core_path)) as core:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA cache_size=-262144")
            core.execute("BEGIN")
            # Acquire the read snapshot before fingerprinting. The first WAL
            # reader may initialize empty WAL/SHM files without a data commit.
            core.execute("SELECT id FROM study_effect LIMIT 1").fetchone()
            initial = fingerprint(core_path)
            db.execute("DROP TABLE IF EXISTS perf_search")
            for name, kind in db.execute("SELECT name,type FROM sqlite_master WHERE name LIKE 'perf_%' AND type IN ('table','index')").fetchall():
                escaped = name.replace('"', '""')
                db.execute(f'DROP {kind.upper()} IF EXISTS "{escaped}"')
            db.executescript(SCHEMA)
            print(canonical({"phase":"search_and_sort_indexes"}), flush=True)
            exists = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "workbench_evidence" in exists:
                db.execute("CREATE INDEX perf_wb_order ON workbench_evidence(layer,study_id,tissue,id)")
            db.execute("""INSERT INTO perf_search_content(node_id,label,label_en,aliases,identifier)
                SELECT id,label,coalesce(label_en,''),aliases_json,id FROM node ORDER BY id""")
            db.execute("INSERT INTO perf_search(perf_search) VALUES('rebuild')")
            measurements = {r["id"]: dict(r) for r in db.execute("SELECT id,amount FROM measurement")}
            refs = {k: science_records(db, k) for k in ("contrast", "exposure", "curation")}
            selectors = {selector_key(c): c for c in refs["contrast"]}
            counts = {}
            for selector, c in selectors.items():
                types = Counter()
                batch, total = [], 0
                query = "SELECT id,feature_type,feature_id FROM study_effect WHERE series_accession=? AND analysis_version=? AND contrast=? ORDER BY feature_type,feature_id,id"
                for r in core.execute(query, (c["series_accession"], c["analysis_version"], c["contrast"])):
                    total += 1
                    types[r["feature_type"]] += 1
                    batch.append((selector, r["feature_type"], r["feature_id"], r["id"], total, types[r["feature_type"]]))
                    if len(batch) >= 5000:
                        db.executemany("INSERT INTO perf_effect_ref VALUES(?,?,?,?,?,?)", batch)
                        batch.clear()
                db.executemany("INSERT INTO perf_effect_ref VALUES(?,?,?,?,?,?)", batch)
                db.execute("INSERT INTO perf_effect_selector VALUES(?,?,?,?,?)", (selector, c["series_accession"], c["contrast"], c["analysis_version"], total))
                db.executemany("INSERT INTO perf_effect_type VALUES(?,?,?)", [(selector, t, n) for t, n in types.items()])
                counts[selector] = total
                print(canonical({"phase":"effect_references", "series":c["series_accession"], "selector_done":len(counts), "selector_total":len(selectors), "rows":total}), flush=True)
            curations = refs["curation"][0] if refs["curation"] else {}
            curated = {r["core_exposure_id"]: r for r in curations.get("exposures", [])}
            food_components = {}
            for r in db.execute("SELECT DISTINCT food,component FROM measurement WHERE amount>0 AND status NOT IN ('missing','unverified','below_detection')"):
                food_components.setdefault(r[0], []).append(r[1])
            for r in db.execute("SELECT id,kind,label,label_en,aliases_json,metadata_json FROM node"):
                seed = dict(r)
                seed["metadata"] = decoded(seed.pop("metadata_json"))
                seed["aliases"] = decoded(seed.pop("aliases_json"), [])
                seed["label_en"] = seed.get("label_en") or ""
                if seed["kind"] == "food":
                    seed["metadata"]["available_component_ids"] = food_components.get(seed["id"], [])
                for original in refs["exposure"]:
                    exp = {**original, **curated.get(original["id"], {})}
                    identity = exposure_match_identity(seed, exp, decoded(original.get("component_keys"), []))
                    if identity["status"] != "unknown":
                        db.execute("INSERT INTO perf_exposure_match VALUES(?,?,?)", (seed["id"], original["id"], canonical(identity)))
            print(canonical({"phase":"adjacency_index"}), flush=True)
            batch = []
            correspondence = {"candidate_name_correspondence", "source_identifier_cross_reference"}
            for r in db.execute("SELECT id,subject,object,relation,tier,context_json FROM edge"):
                relation = r["relation"]
                disease = "disease" in relation.lower()
                rank = 0 if disease else 1 if relation in correspondence else 3 if r["tier"] == "observed_composition" else 2
                health = 0 if disease or relation == "randomized_difference" else 1 if relation in correspondence else 4 if relation == "annotated_to_pathway" else 2
                gene = 0 if relation == "chemical_gene_influence" else 1 if disease else 2 if relation in correspondence else 3
                context = decoded(r["context_json"])
                measurement_id = context.get("measurement_id")
                # Existing embedded measurement remains relevant if no referenced record exists.
                m = measurements.get(measurement_id, context.get("measurement"))
                usable = r["tier"] != "restricted" and (not m or m.get("amount") is not None and m["amount"] > 0)
                batch.append((r["id"], r["subject"], r["object"], relation, r["tier"], rank, health, gene, measurement_id, int(usable)))
                if len(batch) >= 5000:
                    db.executemany("INSERT INTO perf_adj VALUES(?,?,?,?,?,?,?,?,?,?)", batch)
                    batch.clear()
            db.executemany("INSERT INTO perf_adj VALUES(?,?,?,?,?,?,?,?,?,?)", batch)
            db.execute("ANALYZE")
            if fingerprint(core_path) != initial:
                raise RuntimeError("Core/WAL changed during build; refuse a mixed snapshot")
            parent_meta = {r[0]: decoded(r[1]) for r in db.execute("SELECT * FROM meta")}
            info = {"schema_version": 1, "indexed_for_release":release_id, "core_fingerprint": json.loads(canonical(initial)), "matching_rule_version": RULE_VERSION,
                    "source_release": parent_meta.get("release_id"), "selector_count": len(selectors), "effect_reference_rows": sum(counts.values()),
                    "effect_values_copied": 0, "builder_sha256": sha256(Path(__file__))}
            created_at = datetime.now(UTC).isoformat()
            db.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", [("release_id", canonical(release_id)), ("performance", canonical(info)), ("created_at", canonical(created_at))])
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Index integrity failed")
        for name in ("manifest.json", "scope.json"):
            if (parent_dir / name).exists():
                shutil.copy2(parent_dir / name, output_dir / name)
        manifest = {**parent_meta, "release_id": release_id, "created_at":created_at, "performance": info, "database_sha256": sha256(destination)}
        (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest
    except BaseException:
        # Keep failed build artifacts for audit; never install this directory.
        (output_dir / "FAILED").write_text("Build incomplete; must not activate", encoding="utf-8")
        raise
