from datetime import datetime, timedelta, timezone
import json
import os
import time
import psutil
from .db import connect, digest, dumps, enqueue, enqueue_engine, finish_run, now, start_run, uid
from .collection import ApiClient, fetch_oa


def store_extraction(settings, paper_id, source, result):
    from .registry_v7 import (
        validate_engine,
        validate_source_spans,
        engine_manifest_validation,
        default_engine,
    )

    engine = validate_engine(result["engine_version"])
    text = source["text_content"]
    if digest(text) != source["text_hash"]:
        raise ValueError("Source text integrity failure")
    for item in result["mentions"] + result["assertions"]:
        value = item.get("text", item.get("quote"))
        if not 0 <= item["start"] < item["end"] <= len(text) or text[item["start"] : item["end"]] != value:
            raise ValueError("Candidate offsets do not match source")
    if engine in {"heuristic-v2", "heuristic-v4", "heuristic-v5", "heuristic-v6", "heuristic-v7"}:
        validate_source_spans(result, text)
        reasons = engine_manifest_validation(result, engine, settings if engine in {"heuristic-v5", "heuristic-v6", "heuristic-v7"} else None)
        if reasons:
            raise ValueError("Blocked v2 extraction: " + ", ".join(reasons))
    with connect(settings) as con:
        current = con.execute(
            "SELECT current_source_id,retracted FROM papers WHERE paper_id=?", (paper_id,)
        ).fetchone()
        if current[0] != source["source_id"]:
            raise ValueError("Source changed during extraction")
        existing = con.execute(
            "SELECT extraction_id,result_json FROM extractions WHERE paper_id=? AND source_hash=? "
            "AND engine_version=? AND dictionary_version=? AND active=1",
            (paper_id, source["text_hash"], result["engine_version"], result["dictionary_version"]),
        ).fetchone()
        if (
            existing
            and (
                engine not in {"heuristic-v4", "heuristic-v5", "heuristic-v6", "heuristic-v7"}
                or json.loads(existing["result_json"]).get("manifest", {}).get("artifact_identity")
                == result.get("manifest", {}).get("artifact_identity")
            )
            and json.loads(existing["result_json"]).get("rules_version") == result.get("rules_version")
        ):
            return existing[0]
        prior = {
            r["fingerprint"]: r["status"]
            for r in con.execute(
                "SELECT a.fingerprint,a.status FROM assertions a JOIN extractions e USING(extraction_id) "
                "WHERE a.paper_id=? AND e.engine_version=? AND e.active=1",
                (paper_id, engine),
            )
        }
        con.execute(
            "UPDATE extractions SET active=0 WHERE paper_id=? AND engine_version=?", (paper_id, engine)
        )
        con.execute(
            "UPDATE assertions SET status='needs_review' WHERE paper_id=? AND status IN ('approved','corrected') "
            "AND extraction_id IN (SELECT extraction_id FROM extractions WHERE engine_version=?)",
            (paper_id, engine),
        )
        extraction_id = uid()
        con.execute(
            "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                extraction_id,
                paper_id,
                source["source_id"],
                source["text_hash"],
                result["engine_version"],
                result["dictionary_version"],
                dumps(result),
                now(),
                1,
            ),
        )
        for mention in result["mentions"]:
            con.execute(
                "INSERT INTO mentions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    uid(),
                    extraction_id,
                    paper_id,
                    mention["kind"],
                    mention["text"],
                    mention.get("normalized_id"),
                    mention["start"],
                    mention["end"],
                    mention["status"],
                    dumps(mention.get("payload", {})),
                ),
            )
        for assertion in result["assertions"]:
            fingerprint_inputs = [
                source["text_hash"],
                engine,
                result.get("rules_version"),
                result["dictionary_version"],
                assertion["subject"],
                assertion["predicate"],
                assertion["object"],
                assertion["quote"],
                assertion["start"],
                assertion["end"],
            ]
            if engine in {"heuristic-v2", "heuristic-v4", "heuristic-v5", "heuristic-v6", "heuristic-v7"}:
                fingerprint_inputs += [assertion.get("payload", {}), result.get("manifest", {})]
            fingerprint = digest(dumps(fingerprint_inputs))
            status = "retracted" if current[1] else prior.get(fingerprint, "candidate")
            con.execute(
                "INSERT OR IGNORE INTO assertions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid(),
                    extraction_id,
                    paper_id,
                    fingerprint,
                    assertion["subject"],
                    assertion["predicate"],
                    assertion["object"],
                    assertion["quote"],
                    assertion["start"],
                    assertion["end"],
                    assertion["rule_id"],
                    status,
                    dumps(assertion.get("payload", {})),
                ),
            )
        organism = result.get("context", {}).get("organism", "unknown")
        if not isinstance(organism, str):
            organism = dumps(organism)
        if engine == default_engine(settings):
            con.execute("UPDATE papers SET organism=? WHERE paper_id=?", (organism, paper_id))
        con.execute(
            "INSERT INTO audit VALUES(?,?,?,?,?)",
            (
                uid(),
                "extracted",
                paper_id,
                dumps(
                    {
                        "extraction_id": extraction_id,
                        "engine": engine,
                        "rules_version": result.get("rules_version"),
                        "manifest": result.get("manifest", {}),
                    }
                ),
                now(),
            ),
        )
        return extraction_id


def process(settings, stage=None, limit=100, client=None, engine="heuristic-v1"):
    if engine == "heuristic-v5" and stage != "oa":
        from .operations_v5 import process as process_v5

        return process_v5(settings, limit)
    if engine != "heuristic-v1" and stage != "oa":
        return process_engine(settings, engine, limit)
    from .operations_v4 import processing_slot

    with processing_slot(settings):
        return _process(settings, stage, limit, client, engine)


def _process(settings, stage=None, limit=100, client=None, engine="heuristic-v1"):
    if engine != "heuristic-v1" and stage != "oa":
        return process_engine(settings, engine, limit)
    from .dictionaries import load_dictionaries
    from .extraction import extract

    client = client or ApiClient(settings)
    dictionaries = None if stage == "oa" else load_dictionaries(settings)
    run_id = start_run(settings, "process_" + (stage or "all"))
    counts = {"processed": 0, "failed": 0, "stages": {}}
    try:
        for _ in range(limit):
            if (
                psutil.virtual_memory().available < float(__import__("os").environ.get("LITERATURE_MEMORY_RESERVE_GIB", "2")) * 1024**3
                or psutil.Process().memory_info().rss > 8 * 1024**3
            ):
                counts["throttled"] = True
                break
            with connect(settings) as con:
                con.execute("BEGIN IMMEDIATE")
                stale = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
                con.execute("UPDATE queue SET status='pending' WHERE status='running' AND due_at<?", (stale,))
                params = [now()]
                condition = "status='pending' AND due_at<=?"
                if stage:
                    condition += " AND stage=?"
                    params.append(stage)
                queue = con.execute(
                    "SELECT * FROM queue WHERE "
                    + condition
                    + " ORDER BY CASE stage WHEN 'extract' THEN 0 ELSE 1 END,queue_id LIMIT 1",
                    params,
                ).fetchone()
                if not queue:
                    break
                queue = dict(queue)
                con.execute(
                    "UPDATE queue SET status='running',due_at=?,attempts=attempts+1 WHERE queue_id=?",
                    (now(), queue["queue_id"]),
                )
                paper = dict(
                    con.execute("SELECT * FROM papers WHERE paper_id=?", (queue["paper_id"],)).fetchone()
                )
                source = con.execute(
                    "SELECT * FROM sources WHERE source_id=?", (paper["current_source_id"],)
                ).fetchone()
            try:
                if queue["stage"] == "oa":
                    status = fetch_oa(settings, paper, client)
                    with connect(settings) as con:
                        con.execute(
                            "UPDATE papers SET oa_status=? WHERE paper_id=?", (status, paper["paper_id"])
                        )
                else:
                    if source is None:
                        raise ValueError("No source available")
                    metadata = {
                        **paper,
                        "publication_types": json.loads(paper["publication_types"]),
                        "mesh": json.loads(paper["mesh"]),
                    }
                    result = extract(
                        source["text_content"], json.loads(source["sections"]), metadata, dictionaries
                    )
                    store_extraction(settings, paper["paper_id"], dict(source), result)
                with connect(settings) as con:
                    con.execute(
                        "UPDATE queue SET status='completed',error=NULL WHERE queue_id=?",
                        (queue["queue_id"],),
                    )
                counts["processed"] += 1
                counts["stages"][queue["stage"]] = counts["stages"].get(queue["stage"], 0) + 1
            except Exception as error:
                counts["failed"] += 1
                attempts = queue["attempts"] + 1
                due = (datetime.now(timezone.utc) + timedelta(minutes=min(1440, 5 * 2**attempts))).isoformat()
                with connect(settings) as con:
                    con.execute(
                        "UPDATE queue SET status=?,due_at=?,error=? WHERE queue_id=?",
                        ("failed" if attempts >= 5 else "pending", due, str(error), queue["queue_id"]),
                    )
                    if queue["stage"] == "oa":
                        con.execute(
                            "UPDATE papers SET oa_status='transient_failure' WHERE paper_id=?",
                            (paper["paper_id"],),
                        )
        finish_run(settings, run_id, "partial" if counts["failed"] else "completed", counts)
    except BaseException as error:
        finish_run(settings, run_id, "failed", counts, str(error))
        raise
    return {"run_id": run_id, **counts}


def requeue_oa(settings):
    with connect(settings) as con:
        rows = con.execute("SELECT paper_id FROM papers WHERE retracted=0").fetchall()
        for row in rows:
            enqueue(con, row[0], "oa")
    return {"queued": len(rows)}


def reprocess(settings, engine="heuristic-v1"):
    if engine != "heuristic-v1":
        return register_engine(settings, engine, queue_all=True)
    with connect(settings) as con:
        rows = con.execute("SELECT paper_id FROM papers").fetchall()
        for row in rows:
            enqueue(con, row[0], "extract")
    return {"queued": len(rows)}


def worker(settings, stage, engine=None):
    from .extraction import RULES_VERSION

    if stage == "extract" and engine == "heuristic-v2":
        register_engine(settings, engine, queue_all=False)
    with connect(settings) as con:
        # CLI holds this stage's exclusive lock, so previous owners have terminated.
        if stage == "extract":
            con.execute(
                "UPDATE engine_queue SET status='pending',due_at=?,error='recovered after exclusive worker restart' "
                "WHERE status='running' AND engine_version='heuristic-v2'",
                (now(),),
            )
            con.execute(
                "UPDATE runs SET status='interrupted',finished_at=?,error='worker restarted' "
                "WHERE kind='process_extract_heuristic-v2' AND status='running'",
                (now(),),
            )
        con.execute(
            "UPDATE queue SET status='pending',due_at=?,error='recovered after exclusive worker restart' "
            "WHERE stage=? AND status='running'",
            (now(), stage),
        )
        con.execute(
            "UPDATE runs SET status='interrupted',finished_at=?,error='worker restarted' WHERE kind=? AND status='running'",
            (now(), "process_" + stage),
        )
        if stage == "extract":
            for row in con.execute(
                "SELECT paper_id,result_json FROM extractions WHERE active=1 AND engine_version='heuristic-v1'"
            ).fetchall():
                if json.loads(row["result_json"]).get("rules_version") != RULES_VERSION:
                    enqueue(con, row["paper_id"], "extract")
    if hasattr(os, "nice"):
        os.nice(10)
    while True:
        secondary = {"processed": 0, "failed": 0}
        if stage == "extract":
            from .operations_v2 import worker_heartbeat

            worker_heartbeat(settings, "v1_batch")
        result = process(settings, stage, 100)
        if result["processed"] or result["failed"]:
            print(dumps(result), flush=True)
        if stage == "extract":
            with connect(settings) as con:
                enabled = con.execute(
                    "SELECT 1 FROM engine_profiles WHERE engine_version='heuristic-v2' AND enabled=1"
                ).fetchone()
            if enabled:
                secondary = process_engine(settings, "heuristic-v2", 25)
                if secondary["processed"] or secondary["failed"]:
                    print(dumps(secondary), flush=True)
            worker_heartbeat(settings, "idle")
        time.sleep(15 if result["processed"] or secondary["processed"] else 60)


def register_engine(settings, engine, queue_all=False):
    from .dictionaries import load_dictionaries
    from .engines import validate_engine, engine_rules_version

    validate_engine(engine)
    from .operations_v2 import register_artifact

    artifact_version = register_artifact(settings, engine)
    dictionaries = load_dictionaries(settings, engine)
    profile = {
        "engine_version": engine,
        "rules_version": engine_rules_version(engine),
        "dictionary_version": dictionaries.version,
        "artifact_version": artifact_version,
    }
    with connect(settings) as con:
        previous = con.execute("SELECT * FROM engine_profiles WHERE engine_version=?", (engine,)).fetchone()
        changed = not previous or any(previous[k] != profile[k] for k in profile)
        con.execute(
            "INSERT INTO engine_profiles(engine_version,rules_version,dictionary_version,enabled,updated_at,artifact_version) "
            "VALUES(?,?,?,1,?,?) ON CONFLICT(engine_version) DO UPDATE SET "
            "rules_version=excluded.rules_version,dictionary_version=excluded.dictionary_version,"
            "artifact_version=excluded.artifact_version,enabled=1,updated_at=excluded.updated_at",
            (engine, profile["rules_version"], profile["dictionary_version"], now(), artifact_version),
        )
        con.execute(
            "UPDATE engine_queue SET status='superseded',error='engine or dictionary version changed' "
            "WHERE engine_version=? AND status IN ('pending','failed') "
            "AND (rules_version<>? OR dictionary_version<>? OR artifact_version<>?)",
            tuple(profile.values()),
        )
        count = 0
        if queue_all or changed:
            rows = con.execute(
                "SELECT p.paper_id,s.text_hash FROM papers p JOIN sources s "
                "ON s.source_id=p.current_source_id"
            ).fetchall()
            for row in rows:
                enqueue_engine(con, row["paper_id"], row["text_hash"], profile, retry=queue_all)
            count = len(rows)
    return {"engine": engine, "queued": count, "profile": profile}


def process_engine(settings, engine="heuristic-v2", limit=100):
    from .operations_v4 import processing_slot

    if engine == "heuristic-v5":
        from .operations_v5 import process as process_v5

        return process_v5(settings, limit)
    if engine == "heuristic-v4":
        from .operations_v4 import process as process_v4

        return process_v4(settings, limit)
    with processing_slot(settings):
        return _process_engine(settings, engine, limit)


def _process_engine(settings, engine="heuristic-v2", limit=100):
    from .dictionaries import load_dictionaries
    from .engines import extract_engine
    from .operations_v2 import worker_heartbeat

    profile = register_engine(settings, engine)["profile"]
    dictionaries = load_dictionaries(settings, engine)
    run_id = start_run(settings, "process_extract_" + engine)
    counts = {"engine": engine, "processed": 0, "failed": 0, "superseded": 0}
    worker_heartbeat(settings, "v2_batch")
    try:
        for _ in range(limit):
            if (
                psutil.virtual_memory().available < float(__import__("os").environ.get("LITERATURE_MEMORY_RESERVE_GIB", "2")) * 1024**3
                or psutil.Process().memory_info().rss > 4 * 1024**3
            ):
                counts["throttled"] = True
                break
            with connect(settings) as con:
                con.execute("BEGIN IMMEDIATE")
                stale = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
                con.execute(
                    "UPDATE engine_queue SET status='pending' WHERE engine_version=? AND status='running' AND due_at<?",
                    (engine, stale),
                )
                task = con.execute(
                    "SELECT * FROM engine_queue WHERE engine_version=? AND status='pending' AND due_at<=? "
                    "ORDER BY task_id LIMIT 1",
                    (engine, now()),
                ).fetchone()
                if not task:
                    break
                task = dict(task)
                paper = dict(
                    con.execute("SELECT * FROM papers WHERE paper_id=?", (task["paper_id"],)).fetchone()
                )
                source = con.execute(
                    "SELECT * FROM sources WHERE source_id=?", (paper["current_source_id"],)
                ).fetchone()
                if not source or source["text_hash"] != task["source_hash"]:
                    con.execute(
                        "UPDATE engine_queue SET status='superseded',error='current source changed' WHERE task_id=?",
                        (task["task_id"],),
                    )
                    if source:
                        enqueue_engine(con, paper["paper_id"], source["text_hash"], profile)
                    counts["superseded"] += 1
                    continue
                con.execute(
                    "UPDATE engine_queue SET status='running',attempts=attempts+1,due_at=? WHERE task_id=?",
                    (now(), task["task_id"]),
                )
                source = dict(source)
            try:
                worker_heartbeat(settings, "v2_task_" + str(task["task_id"]))
                metadata = {
                    **paper,
                    "publication_types": json.loads(paper["publication_types"]),
                    "mesh": json.loads(paper["mesh"]),
                }
                result = extract_engine(
                    source["text_content"], json.loads(source["sections"]), dictionaries, metadata, engine
                )
                store_extraction(settings, paper["paper_id"], source, result)
                with connect(settings) as con:
                    con.execute(
                        "UPDATE engine_queue SET status='completed',error=NULL WHERE task_id=?",
                        (task["task_id"],),
                    )
                counts["processed"] += 1
            except Exception as error:
                counts["failed"] += 1
                attempts = task["attempts"] + 1
                due = (datetime.now(timezone.utc) + timedelta(minutes=min(1440, 5 * 2**attempts))).isoformat()
                with connect(settings) as con:
                    con.execute(
                        "UPDATE engine_queue SET status=?,due_at=?,error=? WHERE task_id=?",
                        ("failed" if attempts >= 5 else "pending", due, str(error), task["task_id"]),
                    )
        finish_run(
            settings,
            run_id,
            "partial" if counts["failed"] or counts.get("throttled") else "completed",
            counts,
        )
    except BaseException as error:
        finish_run(settings, run_id, "failed", counts, str(error))
        raise
    return {"run_id": run_id, **counts}
