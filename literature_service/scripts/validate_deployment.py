"""Record actual deployment invariants without reporting scientific accuracy."""

import json
from pathlib import Path
import sqlite3
import tempfile

import requests

from nutri_literature.db import connect, digest, now
from nutri_literature.extraction import RULES_VERSION
from nutri_literature.engines import default_engine, engine_manifest_validation, validate_source_spans
from nutri_literature.settings import Settings


def validate(settings):
    with connect(settings) as con:
        con.execute("BEGIN")
        integrity = [r[0] for r in con.execute("PRAGMA integrity_check")]
        foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        sources = [dict(r) for r in con.execute("SELECT * FROM sources")]
        source_by_id = {r["source_id"]: r for r in sources}
        source_errors = sum(digest(r["text_content"]) != r["text_hash"] for r in sources)
        raw_errors = sum(
            not Path(r["raw_path"]).is_file() or digest(Path(r["raw_path"]).read_bytes()) != r["sha256"]
            for r in sources
        )
        offset_errors = 0
        for table, field in [("mentions", "text"), ("assertions", "quote")]:
            for r in con.execute(
                f"SELECT a.*,e.source_id FROM {table} a JOIN extractions e USING(extraction_id)"
            ):
                text = source_by_id[r["source_id"]]["text_content"]
                offset_errors += not (
                    0 <= r["start"] < r["end"] <= len(text) and text[r["start"] : r["end"]] == r[field]
                )
        papers = con.execute("SELECT count(*) FROM papers").fetchone()[0]
        fulltext = con.execute(
            "SELECT count(DISTINCT paper_id) FROM sources WHERE kind='fulltext'"
        ).fetchone()[0]
        active = con.execute(
            "SELECT e.*,p.current_source_id FROM extractions e JOIN papers p USING(paper_id) WHERE e.active=1"
        ).fetchall()
        current_by_engine={}
        nested_span_errors=0
        for row in active:
            result=json.loads(row['result_json'])
            valid=row['source_id']==row['current_source_id'] and not engine_manifest_validation(result,row['engine_version'],settings)
            current_by_engine[row['engine_version']]=current_by_engine.get(row['engine_version'],0)+int(valid)
            if row['engine_version']=='heuristic-v2':
                try:
                    validate_source_spans(result,source_by_id[row['source_id']]['text_content'])
                except ValueError:
                    nested_span_errors+=1
        current = sum(
            r["source_id"] == r["current_source_id"]
            and json.loads(r["result_json"]).get("rules_version") == RULES_VERSION
            for r in active
        )
        current_ids = {
            r["extraction_id"]
            for r in active
            if r["source_id"] == r["current_source_id"]
            and json.loads(r["result_json"]).get("rules_version") == RULES_VERSION
        }
        current_assertions = sum(
            r[0] in current_ids for r in con.execute("SELECT extraction_id FROM assertions")
        )
        last_full = con.execute(
            "SELECT * FROM runs WHERE kind='reconcile' AND status='completed' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        queue = [
            dict(r)
            for r in con.execute("SELECT stage,status,count(*) AS count FROM queue GROUP BY stage,status")
        ]
        backup_paths = sorted(settings.backups.glob("literature-*.sqlite3"))
    restored = None
    if backup_paths:
        with tempfile.TemporaryDirectory(prefix="literature-restore-") as directory:
            copied = Path(directory) / "restored.sqlite3"
            copied.write_bytes(backup_paths[-1].read_bytes())
            with sqlite3.connect(copied) as con:
                restored = {
                    "backup": str(backup_paths[-1]),
                    "integrity": con.execute("PRAGMA integrity_check").fetchone()[0],
                    "foreign_key_errors": len(con.execute("PRAGMA foreign_key_check").fetchall()),
                    "papers": con.execute("SELECT count(*) FROM papers").fetchone()[0],
                }
    health = {
        name: requests.get(url, timeout=10).status_code == 200
        for name, url in [
            ("api", "http://127.0.0.1:8010/healthz"),
            ("ui", "http://127.0.0.1:8510/_stcore/health"),
        ]
    }
    result = {
        "checked_at": now(),
        "rules_version": RULES_VERSION,
        "integrity": integrity,
        "foreign_key_errors": foreign_keys,
        "source_text_hash_errors": source_errors,
        "raw_hash_errors": raw_errors,
        "span_errors": offset_errors,
        'nested_v2_span_errors':nested_span_errors,
        'current_by_engine':current_by_engine,
        'default_engine':default_engine(settings),
        "papers": papers,
        "summarized_current": current,
        "fulltext": fulltext,
        "current_relation_candidates": current_assertions,
        "last_full_collection": {**dict(last_full), "detail": json.loads(last_full["detail"])}
        if last_full
        else None,
        "queue": queue,
        "health": health,
        "backup_restore": restored,
        "scientific_accuracy": "pending independent expert annotation",
        "server_reboot": "pending shared-server maintenance",
        "seven_day_soak": "observing actual five-minute checks",
    }
    result["passed"] = (
        integrity == ["ok"]
        and not any([foreign_keys, source_errors, raw_errors, offset_errors, nested_span_errors])
        and all(health.values())
        and bool(restored and restored["integrity"] == "ok" and restored["foreign_key_errors"] == 0)
    )
    destination = settings.home / "validation" / "deployment.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    receipt = validate(Settings.from_env())
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    if not receipt["passed"]:
        raise SystemExit(1)
