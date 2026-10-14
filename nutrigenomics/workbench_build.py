"""Immutable Explorer projection of original evidence; no inferred biology.

All accepted rows of each supplied sidecar are indexed, including non-significant
and qualitative results. This does not enlarge the source's selected summaries.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .explorer_build import SCHEMA, ReleaseWriter, canonical, decode, read_only, sha256

WORKBENCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS workbench_evidence(
 id TEXT PRIMARY KEY,subject_id TEXT NOT NULL REFERENCES node(id),
 object_id TEXT NOT NULL REFERENCES node(id),relation TEXT NOT NULL,
 layer TEXT NOT NULL,study_id TEXT,source_id TEXT NOT NULL REFERENCES source(id),
 tissue TEXT,organism TEXT,review_status TEXT NOT NULL,export_allowed INTEGER NOT NULL,
 metadata_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS wb_subject ON workbench_evidence(subject_id,layer);
CREATE INDEX IF NOT EXISTS wb_object ON workbench_evidence(object_id,layer);
CREATE INDEX IF NOT EXISTS wb_layer_tissue ON workbench_evidence(layer,tissue);
CREATE INDEX IF NOT EXISTS wb_study ON workbench_evidence(study_id);
CREATE TABLE IF NOT EXISTS workbench_record_origin(
 key TEXT PRIMARY KEY,evidence_id TEXT NOT NULL REFERENCES workbench_evidence(id));
CREATE TABLE IF NOT EXISTS workbench_review(
 evidence_id TEXT NOT NULL REFERENCES workbench_evidence(id),reviewer TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','pending')),
 reviewed_at TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,note TEXT NOT NULL,
 PRIMARY KEY(evidence_id,reviewer,reviewed_at));
"""


def entity_kind(row):
    namespace, identifier = row["namespace"], row["identifier"]
    if namespace == "ENCODE-cCRE":
        return "regulatory_region"
    if re.fullmatch(r"cg\d{8}", identifier):
        return "cpg"
    if identifier.startswith("ch."):
        return "methylation_probe"
    return row["kind"]


def evidence_layer(row):
    relation = row["relation"].lower()
    if "eqtm" in relation:
        return "eqtm"
    if relation == "experimentally_tested_ccre_gene_pair":
        return "regulatory_tested"
    if relation == "predicted_ccre_gene_link":
        return "regulatory_predicted"
    if row["layer"] == "health_outcome":
        return "clinical"
    return row["layer"]


def import_sidecar(target, path):
    counts = Counter()
    seen_symbols = set()
    with read_only(path) as original:
        releases = {r["id"]: dict(r) for r in original.execute("SELECT * FROM ng_release")}
        studies = {r["id"]: dict(r) for r in original.execute("SELECT * FROM ng_study")}
        writer = ReleaseWriter(target)
        for sid, source in releases.items():
            hgnc_cc0 = (
                source["source"] == "HGNC-official-identifiers"
                and source["license"] == "CC0-1.0"
                and source["url"]
                == "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"
            )
            writer.source(
                sid,
                {
                    **source,
                    "export_allowed": hgnc_cc0,
                    "source_kind": "original_ng_release",
                    **(
                        {
                            "terms_url": "https://www.genenames.org/about/license/",
                            "rights_reviewed_at": "2026-10-09",
                        }
                        if hgnc_cc0
                        else {}
                    ),
                },
            )
            # Active presentation replaces an older snapshot of this exact
            # source family. Old immutable releases keep their original rows.
            old_sources = [
                r["id"]
                for r in target.execute("SELECT id,metadata_json FROM source")
                if decode(r["metadata_json"]).get("source") == source["source"]
                and decode(r["metadata_json"]).get("sha256") != source["sha256"]
            ]
            for old in old_sources:
                ids = [
                    r[0]
                    for r in target.execute(
                        "SELECT id FROM workbench_evidence WHERE source_id=?", (old,)
                    )
                ]
                for eid in ids:
                    target.execute("DELETE FROM workbench_review WHERE evidence_id=?", (eid,))
                    target.execute(
                        "DELETE FROM workbench_record_origin WHERE evidence_id=?", (eid,)
                    )
                    target.execute("DELETE FROM edge_domain WHERE edge_id=?", (eid,))
                    target.execute("DELETE FROM edge WHERE id=?", (eid,))
                target.execute("DELETE FROM workbench_evidence WHERE source_id=?", (old,))
                counts["superseded_source_rows"] += len(ids)
        for row in original.execute("SELECT * FROM ng_entity ORDER BY id"):
            item = dict(row)
            metadata = decode(item["metadata_json"], {})
            old = target.execute(
                "SELECT metadata_json FROM node WHERE id=?", (item["id"],)
            ).fetchone()
            if old:
                continue  # A shared original identifier never overwrites a food/version record.
            writer.node(
                item["id"],
                entity_kind(item),
                item["label"],
                metadata={
                    **metadata,
                    "namespace": item["namespace"],
                    "identifier": item["identifier"],
                    "organism": item["organism"],
                    "source_native_identity": True,
                },
            )
        for row in original.execute("SELECT * FROM ng_evidence ORDER BY id"):
            item = dict(row)
            context = decode(item.pop("context_json"), {})
            status = context.get("review_status", "source_recorded")
            if status in {"candidate", "rejected"}:
                counts["withheld_candidate"] += 1
                continue
            item["context"] = context
            item["covariates"] = decode(item.pop("covariates_json"), [])
            item["study"] = studies.get(item["study_id"])
            item["source"] = releases[item["release_id"]]
            item["expert_review_status"] = "pending"
            item["interpretation"] = (
                "Original relation; association, tested pair and prediction remain separate."
            )
            layer = evidence_layer(item)
            import hashlib

            origin = hashlib.sha256(
                canonical(
                    [
                        releases[item["release_id"]]["sha256"],
                        item["relation"],
                        item["subject_id"],
                        item["object_id"],
                        context.get("source_raw_fields", item["source_ref"]),
                    ]
                ).encode()
            ).hexdigest()
            if target.execute(
                "SELECT 1 FROM workbench_record_origin WHERE key=?", (origin,)
            ).fetchone():
                counts["duplicate_source_record"] += 1
                continue
            eid = "WB:" + item["id"]
            exported = context.get("export_allowed") is True
            item["export_allowed"] = exported
            value = canonical(item)
            prior = target.execute(
                "SELECT metadata_json FROM workbench_evidence WHERE id=?", (eid,)
            ).fetchone()
            if prior and prior[0] != value:
                raise ValueError(
                    "changed evidence must use a new source/version identifier: " + eid
                )
            target.execute(
                "INSERT OR IGNORE INTO workbench_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    item["subject_id"],
                    item["object_id"],
                    item["relation"],
                    layer,
                    item["study_id"],
                    item["release_id"],
                    item["tissue"],
                    item["organism"],
                    status,
                    int(exported),
                    value,
                ),
            )
            target.execute("INSERT INTO workbench_record_origin VALUES(?,?)", (origin, eid))
            if not prior:
                counts[layer] += 1
            context = {
                **context,
                "original_evidence_id": item["id"],
                "workbench_evidence_id": eid,
                "evidence_layer": layer,
                "study_id": item["study_id"],
                "tissue": item["tissue"],
                "expert_review_status": "pending",
                "export_allowed": exported,
            }
            writer.edge(
                item["subject_id"],
                item["object_id"],
                item["relation"],
                "predicted" if layer == "regulatory_predicted" else item["tier"],
                context,
                [item["release_id"]],
                review_status=status,
                taxon=item["organism"],
                identifier=eid,
            )
            # Only an original identifier-to-symbol annotation is searchable;
            # different gene namespaces are not merged by that symbol.
            symbol = context.get("source_symbol") or context.get("gene_symbol")
            if (
                symbol
                and (item["object_id"], symbol) not in seen_symbols
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,60}", str(symbol).strip())
            ):
                seen_symbols.add((item["object_id"], symbol))
                node = target.execute(
                    "SELECT aliases_json FROM node WHERE id=? AND kind='gene'", (item["object_id"],)
                ).fetchone()
                if node:
                    aliases = set(decode(node[0], []))
                    aliases.add(str(symbol).strip())
                    target.execute(
                        "UPDATE node SET aliases_json=? WHERE id=?",
                        (canonical(sorted(aliases)), item["object_id"]),
                    )
    return dict(counts)


def build_workbench_release(base_dir, sidecars, output_dir, release_id):
    base_dir = Path(base_dir).resolve() if base_dir else None
    output_dir = Path(output_dir)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", release_id):
        raise ValueError("invalid release identifier")
    if output_dir.exists():
        raise FileExistsError("release is immutable")
    inputs = [Path(p).resolve() for p in sidecars]
    if len(set(inputs)) != len(inputs):
        raise ValueError("duplicate sidecar")
    hashes = {str(p): sha256(p) for p in inputs}
    parent = decode((base_dir / "manifest.json").read_text(encoding="utf-8")) if base_dir else {}
    if base_dir and sha256(base_dir / "explorer.sqlite") != parent["database_sha256"]:
        raise ValueError("parent hash mismatch")
    staging = output_dir.with_name("." + output_dir.name + ".building")
    staging.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA cache_size=-1048576")
    db.row_factory = sqlite3.Row
    try:
        if base_dir:
            with read_only(base_dir / "explorer.sqlite") as source:
                source.backup(db)
        else:
            db.executescript(SCHEMA)
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(WORKBENCH_SCHEMA)
        added = {str(p): import_sidecar(db, p) for p in inputs}
        # Existing measured clinical graph endpoints remain available even if
        # their source is separate from the dietary omics studies.
        for row in db.execute(
            "SELECT * FROM edge WHERE tier IN ('human_randomized','human_nonrandomized','human_observational')"
        ).fetchall():
            ctx = decode(row["context_json"], {})
            raw = ctx.get("original_assertion", ctx)
            if raw.get("layer") != "health_outcome" and "clinical_endpoint" not in ctx:
                continue
            eid = "clinical:" + row["id"]
            src = decode(row["source_ids_json"], [])
            if not src:
                continue
            value = {
                **raw,
                "context": decode(raw.get("context_json"), {}) if raw.get("context_json") else ctx,
                "source_ids": src,
                "original_edge_id": row["id"],
                "subject_id": row["subject"],
                "object_id": row["object"],
                "expert_review_status": "pending",
            }
            db.execute(
                "INSERT OR IGNORE INTO workbench_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    row["subject"],
                    row["object"],
                    row["relation"],
                    "clinical",
                    raw.get("study_id"),
                    src[0],
                    raw.get("tissue"),
                    raw.get("organism", row["taxon"]),
                    row["review_status"],
                    int(ctx.get("export_allowed") is True),
                    canonical(value),
                ),
            )
        db.execute(
            "UPDATE node SET degree=(SELECT count(*) FROM edge WHERE subject=node.id)+(SELECT count(*) FROM edge WHERE object=node.id)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS edge_tier_ends_idx ON edge(tier,subject,object)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS extracted_only(node_id TEXT PRIMARY KEY REFERENCES node(id))"
        )
        db.execute("DELETE FROM extracted_only")
        db.execute("""INSERT INTO extracted_only
            WITH candidates(id) AS (SELECT subject FROM edge WHERE tier='extracted'
                UNION SELECT object FROM edge WHERE tier='extracted')
            SELECT id FROM candidates c WHERE NOT EXISTS
                (SELECT 1 FROM edge e WHERE (e.subject=c.id OR e.object=c.id) AND tier!='extracted')""")
        count = {
            "node": db.execute("SELECT count(*) FROM node").fetchone()[0],
            "edge": db.execute("SELECT count(*) FROM edge").fetchone()[0],
            "by_kind": dict(db.execute("SELECT kind,count(*) FROM node GROUP BY kind")),
            "by_relation": dict(db.execute("SELECT relation,count(*) FROM edge GROUP BY relation")),
            "by_tier": dict(db.execute("SELECT tier,count(*) FROM edge GROUP BY tier")),
            "source": db.execute("SELECT count(*) FROM source").fetchone()[0],
            "workbench_by_layer": dict(
                db.execute("SELECT layer,count(*) FROM workbench_evidence GROUP BY layer")
            ),
        }
        manifest = {
            **parent,
            "release_id": release_id,
            "created_at": datetime.now(UTC).isoformat(),
            "derived_from": {
                "release_id": parent.get("release_id"),
                "database_sha256": parent.get("database_sha256"),
            },
            "workbench": {
                "schema_version": 1,
                "input_sha256": {**parent.get("workbench", {}).get("input_sha256", {}), **hashes},
                "added": added,
                "source_summary_scope": "All accepted supplied sidecar rows; not all globally tested relations",
                "expert_review_status": "pending",
                "effect_value_rows_copied_from_core": 0,
            },
            "counts": {**parent.get("counts", {}), **count},
            "build_method": "Original SQLite backup plus audited sidecar projection",
            "builder_code_sha256": sha256(Path(__file__)),
            "sqlite_cache_budget_mib": 1024,
            "workspace": "bounded_memory_then_permanent_sequential_snapshot",
        }
        manifest.pop("database_sha256", None)
        for key, value in manifest.items():
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, canonical(value)))
        db.commit()
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("projection integrity check failed")
        if (
            db.execute("PRAGMA page_count").fetchone()[0]
            * db.execute("PRAGMA page_size").fetchone()[0]
            > 24_000_000_000
        ):
            raise ValueError("projection workspace exceeds 24GB budget")
        snapshot = sqlite3.connect(staging / "explorer.sqlite")
        db.backup(snapshot)
        snapshot.close()
        db.close()
        if hashes != {str(p): sha256(p) for p in inputs}:
            raise ValueError("source changed during build")
        manifest["database_sha256"] = sha256(staging / "explorer.sqlite")
        for name, value in [
            ("manifest.json", manifest),
            ("coverage.json", count),
            (
                "verification.json",
                {
                    "release_id": release_id,
                    "integrity": "ok",
                    "input_hashes_verified": True,
                    "expert_review": "pending",
                    "data_scope": "full_parent" if base_dir else "annotation_only",
                },
            ),
        ]:
            (staging / name).write_text(canonical(value) + "\n", encoding="utf-8")
        shutil.copyfile(__file__, staging / "workbench_builder_source.py")
        os.replace(staging, output_dir)
        return manifest
    except BaseException:
        db.close()
        # Keep failed staging evidence for diagnosis. Never touch the parent.
        raise
