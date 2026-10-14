"""Collect every supported SCREEN link row, without a selected gene panel.

Actual row-method is authoritative. Raw numeric fields remain untyped. The
result is a new, immutable sidecar, not a replacement of production evidence.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import migrate
from nutrigenomics.explorer_build import canonical, sha256
from nutrigenomics.source_updates import atomic_json


def collect(snapshot, output, mode, resume=False):
    output = Path(output)
    if output.exists():
        raise FileExistsError("sidecar is immutable")
    raw = Path(snapshot["object_path"])
    if sha256(raw) != snapshot["sha256"] or mode not in {"crispr", "graphreg"}:
        raise ValueError("invalid source hash or parser mode")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_suffix(output.suffix + ".building")
    was_partial = staging.exists()
    if was_partial and not resume:
        raise FileExistsError("previous incomplete sidecar requires inspection")
    # Random B-tree writes on the HDD were the dominant bottleneck. A bounded
    # in-memory workspace writes one verified sequential snapshot at completion.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    recovered_partial_journal = staging.with_name(staging.name + "-journal").exists()
    if was_partial:
        # Only this generated unfinished sidecar is writable: SQLite must be
        # allowed to roll back an interrupted transaction's hot journal before
        # it can be copied. Immutable raw objects/releases are never opened here.
        with sqlite3.connect(staging) as previous:
            previous.backup(db)
        previous.close()
    db.execute("PRAGMA cache_size=-1048576")
    migrate(db)
    rid = f"SCREEN-complete:{mode}:V4-hg38:{snapshot['sha256'][:16]}"
    old = db.execute("SELECT sha256 FROM ng_release WHERE id=?", (rid,)).fetchone()
    if was_partial and (not old or old[0] != snapshot["sha256"]):
        raise ValueError("partial sidecar belongs to another input hash")
    db.execute(
        "INSERT OR IGNORE INTO ng_release VALUES(?,?,?,?,?,?,?,?,?)",
        (
            rid,
            "SCREEN-" + mode,
            "V4-hg38",
            snapshot["url"],
            snapshot["retrieved_at"],
            snapshot["sha256"],
            str(raw),
            "Public download; derived redistribution not assessed; export disabled",
            "public",
        ),
    )
    counts = Counter()
    known_entities = {r[0] for r in db.execute("SELECT id FROM ng_entity")}
    known_records = set()
    if was_partial:
        for row in db.execute(
            "SELECT id,release_id,context_json,effect,p_value,q_value FROM ng_evidence"
        ):
            native = json.loads(row["context_json"])["source_raw_fields"]
            expected = f"REG:SCREEN:{mode}:{snapshot['sha256'][:16]}:{hashlib.sha256(canonical(native).encode()).hexdigest()}"
            if (
                row["id"] != expected
                or row["release_id"] != rid
                or any(row[k] is not None for k in ("effect", "p_value", "q_value"))
            ):
                raise ValueError("partial record native identity or statistic differs")
            known_records.add(row["id"])
    seen_records = set()
    expanded = 0
    try:
        with gzip.open(raw, "rt", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                expanded += len(line.encode())
                if expanded > 2_000_000_000:
                    raise ValueError("decompressed source budget exceeded")
                if not line.strip():
                    continue
                fields = line.rstrip("\r\n").split("\t")
                counts["source_rows"] += 1
                if mode == "graphreg":
                    if len(fields) != 9:
                        raise ValueError(f"unsupported GraphReg row shape at {number}")
                    ccre, target, symbol, biotype, method, interval, accession, cell, score = fields
                    if method != "GraphRegLR":
                        counts["excluded_other_method"] += 1
                        continue
                    numeric = {"model_score_untyped": score}
                    relation, tier = "predicted_ccre_gene_link", "curated_mechanism"
                else:
                    if len(fields) != 10:
                        raise ValueError(f"unsupported CRISPR row shape at {number}")
                    (
                        ccre,
                        target,
                        symbol,
                        biotype,
                        interval,
                        method,
                        accession,
                        cell,
                        first,
                        second,
                    ) = fields
                    if not method.startswith("CRISPR"):
                        raise ValueError("unexpected CRISPR method")
                    numeric = {"column_9_untyped": first, "column_10_untyped": second}
                    relation, tier = "experimentally_tested_ccre_gene_pair", "in_vitro"
                if not re.fullmatch(r"EH38E\d+", ccre) or not target.strip():
                    raise ValueError("invalid original entity identifier")
                if any(not math.isfinite(float(v)) for v in numeric.values()):
                    raise ValueError("nonfinite native numeric field")
                namespace = (
                    "Ensembl"
                    if re.fullmatch(r"ENSG\d{11}(?:\.\d+)?", target)
                    else "SCREEN-native-gene"
                )
                subject, obj = "ENCODE-cCRE:" + ccre, namespace + ":" + target
                digest = hashlib.sha256(canonical(fields).encode()).hexdigest()
                eid = f"REG:SCREEN:{mode}:{snapshot['sha256'][:16]}:{digest}"
                if eid in seen_records:
                    counts["duplicate"] += 1
                    continue
                seen_records.add(eid)
                if eid in known_records:
                    counts["admitted"] += 1
                    continue
                for identifier, kind, label, ns, native, metadata in [
                    (
                        subject,
                        "molecular_feature",
                        ccre,
                        "ENCODE-cCRE",
                        ccre,
                        {"genome_build": "GRCh38/hg38"},
                    ),
                    (
                        obj,
                        "gene",
                        symbol.strip() or target,
                        namespace,
                        target,
                        {"source_symbol": symbol.strip()},
                    ),
                ]:
                    if identifier not in known_entities:
                        db.execute(
                            "INSERT OR IGNORE INTO ng_entity VALUES(?,?,?,?,?,?,?)",
                            (
                                identifier,
                                kind,
                                label,
                                ns,
                                native,
                                "Homo sapiens",
                                canonical(metadata),
                            ),
                        )
                        known_entities.add(identifier)
                context = {
                    "review_status": "source_checked",
                    "export_allowed": False,
                    "genome_build": "GRCh38/hg38",
                    "source_interval": interval,
                    "source_symbol": symbol.strip(),
                    "source_biotype": biotype,
                    "method": method,
                    "source_accession": accession,
                    "source_numeric_fields": numeric,
                    "source_raw_fields": fields,
                    "line_number": number,
                    "subset": {"all_supported_rows": True, "gene_panel_filter": False},
                    "validation_status": "tested_pair_significance_not_classified"
                    if mode == "crispr"
                    else "computational_prediction_not_experimentally_validated",
                    "limitations": [
                        "No measured dietary effect or clinical benefit inferred.",
                        "cCRE proximity is not a regulatory validation.",
                        "Native numeric columns have not been classified as effect/P/FDR.",
                    ],
                }
                cursor = db.execute(
                    """INSERT OR IGNORE INTO ng_evidence
                    (id,subject_id,object_id,relation,layer,release_id,source_ref,tier,inference,
                    organism,tissue,value_status,context_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        eid,
                        subject,
                        obj,
                        relation,
                        "molecular_target",
                        rid,
                        f"SCREEN native source line {number}",
                        tier,
                        "descriptive" if mode == "crispr" else "association",
                        "Homo sapiens",
                        cell,
                        "qualitative",
                        canonical(context),
                    ),
                )
                if cursor.rowcount:
                    counts["admitted"] += 1
                else:
                    counts["duplicate"] += 1
                if number % 25000 == 0:
                    if (
                        db.execute("PRAGMA page_count").fetchone()[0]
                        * db.execute("PRAGMA page_size").fetchone()[0]
                        > 16_000_000_000
                    ):
                        raise ValueError("in-memory sidecar workspace exceeds 16GB budget")
                    db.commit()
                    print(
                        json.dumps(
                            {"mode": mode, "source_rows": number, "admitted": counts["admitted"]}
                        ),
                        flush=True,
                    )
        if not known_records.issubset(seen_records):
            raise ValueError("partial sidecar contains rows absent from the verified source")
        db.commit()
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("sidecar integrity failure")
        persisted = output.with_suffix(f".snapshot-{os.getpid()}.building")
        if persisted.exists():
            raise FileExistsError("previous snapshot requires inspection")
        target = sqlite3.connect(persisted)
        db.backup(target)
        target.close()
        db.close()
        os.replace(persisted, output)
        receipt = {
            "sidecar_sha256": sha256(output),
            "source_sha256": snapshot["sha256"],
            "mode": mode,
            "scope": "all supported original rows; predictions are not validation",
            "counts": dict(counts),
            "export_allowed": False,
            "expert_review_status": "pending",
            "resumed_partial": was_partial,
            "collector_code_sha256": sha256(Path(__file__)),
            "workspace": "bounded_memory_then_permanent_sequential_snapshot",
            "partial_recovery_journal_present": recovered_partial_journal,
        }
        atomic_json(output.with_suffix(".receipt.json"), receipt)
        return receipt
    except BaseException:
        db.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=["crispr", "graphreg"], required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            collect(json.loads(args.receipt.read_text()), args.output, args.mode, args.resume),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
