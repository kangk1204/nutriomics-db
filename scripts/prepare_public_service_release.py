"""Derive a separate public service release; source candidates remain unchanged."""
import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from nutrigenomics.explorer_build import canonical, read_only, sha256
from nutrigenomics.publication import EFFECT_FIELDS, _csv
from nutrigenomics.source_updates import atomic_json


def prepare(candidate, parent, output, release_id):
    candidate, parent, output = (Path(p).resolve() for p in (candidate, parent, output))
    manifest = json.loads((candidate / "manifest.json").read_text())
    if sha256(candidate / "nutriomics.sqlite3") != manifest["database_sha256"]:
        raise ValueError("candidate hash mismatch")
    staging = output.with_name(output.name + ".building")
    if output.exists() or staging.exists():
        raise FileExistsError("service release already exists")
    staging.mkdir(parents=True)
    for path in candidate.iterdir():
        if path.name in {"manifest.json", "nutriomics.sqlite3"}:
            continue
        if path.is_dir():
            shutil.copytree(path, staging / path.name)
        else:
            shutil.copy2(path, staging / path.name)
    with read_only(candidate / "nutriomics.sqlite3") as origin, closing(sqlite3.connect(staging / "nutriomics.sqlite3")) as db, read_only(parent / "explorer.sqlite") as source:
        origin.backup(db, pages=2048)
        db.executescript("""
            CREATE TABLE node_domain(node_id TEXT,domain TEXT,PRIMARY KEY(node_id,domain));
            CREATE TABLE edge_domain(edge_id TEXT,domain TEXT,PRIMARY KEY(edge_id,domain));
            CREATE TABLE food_alias(legacy_food_id INTEGER UNIQUE,food_id TEXT PRIMARY KEY);
            CREATE INDEX public_kind_label ON node(kind,label,id);
            CREATE INDEX public_domain_node ON node_domain(domain,node_id);
            CREATE INDEX public_domain_edge ON edge_domain(domain,edge_id);
            INSERT OR IGNORE INTO node_domain SELECT n.id,j.value FROM node n,json_each(n.domains_json) j;
            INSERT OR IGNORE INTO edge_domain SELECT e.id,j.value FROM edge e,json_each(e.domains_json) j;
            UPDATE node SET degree=(SELECT count(*) FROM edge WHERE subject=node.id)+(SELECT count(*) FROM edge WHERE object=node.id);
        """)
        for row in source.execute("SELECT id,metadata_json FROM node WHERE kind='food'"):
            value = json.loads(row["metadata_json"])
            core_id = value.get("core_food_id")
            present = db.execute("SELECT metadata_json FROM node WHERE id=?", (row["id"],)).fetchone()
            if present and core_id is not None:
                info = json.loads(present[0])
                info.update(core_food_id=core_id, public_food_id=row["id"])
                db.execute("UPDATE node SET metadata_json=? WHERE id=?", (canonical(info), row["id"]))
                db.execute("INSERT INTO food_alias VALUES(?,?)", (int(core_id), row["id"]))
        columns = [k for k in EFFECT_FIELDS if k != "metadata_json"]
        db.execute("CREATE VIEW study_effect AS SELECT " + ",".join(columns) + ",json_set(metadata_json,'$.export_allowed',json('true'),'$.publication_rights_policy_id',rights_policy_id) AS metadata_json,NULL AS source_file FROM effect")
        metadata = json.loads((staging / "dataset-metadata.json").read_text())
        metadata.update(release_id=release_id, public_service_parent=manifest["release_id"],
                        public_service_parent_database_sha256=manifest["database_sha256"],
                        food_identifier_policy="Stable source food ID is preferred; legacy numeric IDs are lookup aliases only")
        for k, v in metadata.items():
            db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, canonical(v)))
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("service release integrity failure")
        atomic_json(staging / "dataset-metadata.json", metadata)
        for table in ("node", "meta", "food_alias", "node_domain", "edge_domain"):
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            _csv(staging / "tables" / (table + ".csv.gz"), columns, db.execute("SELECT * FROM " + table))
        (staging / "schema.sql").write_text("\n".join(r[0] + ";" for r in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")), encoding="utf-8")
    files = [{"path": str(p.relative_to(staging)).replace('\\', '/'), "bytes": p.stat().st_size,
              "sha256": sha256(p)} for p in sorted(staging.rglob('*')) if p.is_file()]
    atomic_json(staging / "manifest.json", {**manifest, "release_id": release_id,
                "database_sha256": sha256(staging / "nutriomics.sqlite3"), "files": files})
    staging.rename(output)
    return metadata


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "parent", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--release-id", required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.candidate, a.parent, a.output, a.release_id), indent=2))
