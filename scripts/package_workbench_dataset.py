"""Create a rights-filtered, portable data package and original source catalog.

This creates local review artifacts only. It neither publishes a repository nor
assigns a blanket license to third-party material. Restricted values stay out.
"""

import argparse
import csv
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.explorer import _source
from nutrigenomics.explorer_build import canonical, decode, read_only, sha256
from nutrigenomics.source_updates import atomic_json, validate_release

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT NOT NULL,label TEXT NOT NULL);
CREATE TABLE source(id TEXT PRIMARY KEY,metadata_json TEXT NOT NULL);
CREATE TABLE edge(id TEXT PRIMARY KEY,subject TEXT NOT NULL REFERENCES node(id),
 object TEXT NOT NULL REFERENCES node(id),relation TEXT NOT NULL,tier TEXT NOT NULL,
 review_status TEXT,taxon TEXT,context_json TEXT,source_ids_json TEXT,pmids_json TEXT);
CREATE TABLE measurement(id TEXT PRIMARY KEY,food TEXT NOT NULL REFERENCES node(id),
 component TEXT NOT NULL REFERENCES node(id),amount REAL,unit TEXT,basis TEXT,
 status TEXT,source_id TEXT NOT NULL REFERENCES source(id));
CREATE INDEX public_food_measurement ON measurement(food);
CREATE INDEX public_component_measurement ON measurement(component);
CREATE INDEX public_subject ON edge(subject);
CREATE INDEX public_object ON edge(object);
CREATE TABLE meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
"""


def csv_file(path, header, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def package(release, output):
    release, output = Path(release), Path(output)
    manifest = validate_release(release)
    output.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(output / "nutriomics-permitted.sqlite3")
    db.executescript(SCHEMA)
    nodes = set()
    counts = {"restricted_edges": 0, "restricted_measurements": 0}
    catalog = []
    with read_only(release / "explorer.sqlite") as origin:
        permitted = set()
        for row in origin.execute("SELECT * FROM source ORDER BY id"):
            data = decode(row["metadata_json"])
            assessed = _source(origin, row["id"])
            allow = assessed["export_allowed"] is True
            if allow:
                permitted.add(row["id"])
            # Catalog facts, URLs and hashes only; raw payloads and local paths
            # from unassessed sources are not included in the public package.
            entry = {"id": row["id"], "export_allowed": allow}
            for key in (
                "source",
                "version",
                "sha256",
                "url",
                "original_url",
                "retrieved_at",
                "terms_url",
                "license",
            ):
                if key in data:
                    entry[key] = data[key]
            for key in ("license", "rights_policy_sha256", "rights_reviewed_at"):
                if assessed.get(key) is not None:
                    entry[key] = assessed[key]
            catalog.append(entry)
            db.execute("INSERT INTO source VALUES(?,?)", (row["id"], canonical(entry)))
        for row in origin.execute("SELECT * FROM measurement"):
            if row["source_id"] not in permitted:
                counts["restricted_measurements"] += 1
                continue
            nodes.update([row["food"], row["component"]])
            db.execute(
                "INSERT OR IGNORE INTO node VALUES(?,?,?)", (row["food"], "pending", "pending")
            )
            db.execute(
                "INSERT OR IGNORE INTO node VALUES(?,?,?)", (row["component"], "pending", "pending")
            )
            db.execute(
                "INSERT INTO measurement VALUES(?,?,?,?,?,?,?,?)",
                tuple(
                    row[k]
                    for k in (
                        "id",
                        "food",
                        "component",
                        "amount",
                        "unit",
                        "basis",
                        "status",
                        "source_id",
                    )
                ),
            )
        for row in origin.execute("SELECT * FROM edge"):
            ids = decode(row["source_ids_json"], [])
            if (
                not ids
                or not all(s in permitted for s in ids)
                or decode(row["context_json"]).get("export_allowed") is False
            ):
                counts["restricted_edges"] += 1
                continue
            nodes.update([row["subject"], row["object"]])
            for node in (row["subject"], row["object"]):
                db.execute("INSERT OR IGNORE INTO node VALUES(?,?,?)", (node, "pending", "pending"))
            db.execute(
                "INSERT INTO edge VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    row[k]
                    for k in (
                        "id",
                        "subject",
                        "object",
                        "relation",
                        "tier",
                        "review_status",
                        "taxon",
                        "context_json",
                        "source_ids_json",
                        "pmids_json",
                    )
                ),
            )
        for row in origin.execute("SELECT id,kind,label FROM node"):
            if row["id"] in nodes:
                db.execute(
                    "UPDATE node SET kind=?,label=? WHERE id=?",
                    (row["kind"], row["label"], row["id"]),
                )
        layers = {}
        if origin.execute("SELECT 1 FROM sqlite_master WHERE name='workbench_evidence'").fetchone():
            # This package has no authority to sublicense unassessed external
            # annotation values, even where the private viewer can show them.
            layers = dict(
                origin.execute("SELECT layer,count(*) FROM workbench_evidence GROUP BY layer")
            )
        private_schema = "\n".join(
            r[0] + ";"
            for r in origin.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
            )
        )
        (output / "explorer-schema.sql").write_text(private_schema, encoding="utf-8")
        columns = []
        for row in origin.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            for c in origin.execute('PRAGMA table_info("' + row[0].replace('"', '""') + '")'):
                columns.append((row[0], c[1], c[2], bool(c[3]), c[4], bool(c[5])))
        csv_file(
            output / "schema-columns.csv",
            ["table", "column", "sqlite_type", "not_null", "default", "primary_key"],
            columns,
        )
    counts.update(
        {
            table: db.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("node", "edge", "measurement", "source")
        }
    )
    public_meta = {
        "release_id": manifest["release_id"],
        "parent_database_sha256": manifest["database_sha256"],
        "scope": "rights-filtered composition and relationships; not complete internal release",
        "counts": counts,
        "expert_review": "pending",
        "upstream_workbench_layers": layers,
        "blanket_third_party_license": False,
        "published": False,
        "doi": None,
    }
    for k, v in public_meta.items():
        db.execute("INSERT INTO meta VALUES(?,?)", (k, canonical(v)))
    db.commit()
    if (
        db.execute("PRAGMA foreign_key_check").fetchall()
        or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
    ):
        raise ValueError("portable package failed integrity checks")
    db.close()
    atomic_json(output / "source-catalog.json", catalog)
    atomic_json(output / "dataset-metadata.json", public_meta)
    for filename in ("manifest.json", "coverage.json", "verification.json"):
        shutil.copyfile(release / filename, output / filename)
    csv_file(
        output / "source-catalog.csv",
        ["id", "source", "version", "url", "sha256", "export_allowed"],
        (
            [r.get(k) for k in ("id", "source", "version", "url", "sha256", "export_allowed")]
            for r in catalog
        ),
    )
    (output / "README.md").write_text(
        "# NutriOmics data review package\n\nThis is a local, unpublished package. Use `dataset-metadata.json` for scope and counts. The permitted SQLite subset is different from the full internal Explorer release. External eQTM and SCREEN values are excluded pending redistribution review. Obtain those values from the original sources with the supplied acquisition code. Source URLs, release hashes, schema, limitations and count definitions remain inspectable. No clinical prediction, expert review completion, dataset DOI or public availability is claimed.\n",
        encoding="utf-8",
    )
    atomic_json(
        output / "SHA256SUMS.json",
        {p.name: sha256(p) for p in sorted(output.iterdir()) if p.is_file()},
    )
    return public_meta


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--release", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(package(a.release, a.output), indent=2))
