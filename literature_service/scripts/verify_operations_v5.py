"""Actual online restore and interrupted-task recovery, before worker deployment."""

from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from nutri_literature.db import connect, digest, dumps, now, uid
from nutri_literature import benchmarks as scoring
from nutri_literature.registry_v5 import default_engine, switch_engine
from nutri_literature.settings import Settings

settings = Settings.from_env()
root = settings.home / "experiments" / "v5"
root.mkdir(parents=True, exist_ok=True)
record = {"id": uid(), "at": now(), "kind": "v5-operation-drills"}
with tempfile.TemporaryDirectory(prefix="v5-restore-") as temp:
    backup = Path(temp) / "backup.sqlite3"
    with sqlite3.connect(settings.database) as original, sqlite3.connect(backup) as restored:
        original.backup(restored)
    with sqlite3.connect(backup) as restored:
        integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
        count = restored.execute("SELECT count(*) FROM papers").fetchone()[0]
        invalid = sum(
            digest(text) != sha
            for text, sha in restored.execute("SELECT text_content,text_hash FROM sources")
        )
    record["online_backup_restore"] = {
        "passed": integrity == "ok" and invalid == 0,
        "papers": count,
        "source_integrity_failures": invalid,
    }
args = [sys.executable, "-m", "nutri_literature.cli_v5", "process", "--limit", "1"]
with connect(settings) as con:
    prior_running = {
        r[0]
        for r in con.execute(
            "SELECT task_id FROM engine_queue WHERE engine_version='heuristic-v5' AND status='running'"
        )
    }
with (root / "interruption.log").open("w") as log:
    child = subprocess.Popen(
        args, stdout=log, stderr=subprocess.STDOUT, cwd=Path(__file__).resolve().parents[1]
    )
    interrupted = None
    try:
        deadline = time.monotonic() + 150
        while child.poll() is None and time.monotonic() < deadline:
            with connect(settings) as con:
                task = next(
                    (
                        r
                        for r in con.execute(
                            "SELECT task_id FROM engine_queue WHERE engine_version='heuristic-v5' AND status='running'"
                        )
                        if r[0] not in prior_running
                    ),
                    None,
                )
            if task:
                interrupted = task[0]
                child.kill()
                child.wait(timeout=20)
                break
            time.sleep(0.1)
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
if interrupted:
    with (root / "resume.log").open("w") as log:
        exit_code = subprocess.call(
            args, stdout=log, stderr=subprocess.STDOUT, cwd=Path(__file__).resolve().parents[1]
        )
    with connect(settings) as con:
        task = con.execute(
            "SELECT status,attempts FROM engine_queue WHERE task_id=?", (interrupted,)
        ).fetchone()
    record["interrupted_task_resume"] = {
        "passed": exit_code == 0 and task["status"] == "completed" and task["attempts"] >= 2,
        "task_id": interrupted,
        "status": task["status"],
        "attempts": task["attempts"],
    }
else:
    record["interrupted_task_resume"] = {"passed": False, "reason": "no actual running task was interrupted"}
before = default_engine(settings)
try:
    switch_engine(settings, "heuristic-v5", {"passed": True})
    record["premature_promotion_blocked"] = False
except ValueError:
    record["premature_promotion_blocked"] = default_engine(settings) == before
record["seven_days"] = "pending_actual_elapsed_time"
record["successful_v5_switch_and_rollback"] = "pending_promotion_criteria"
artifact = json.loads((settings.home / "engine-artifacts/heuristic-v5.json").read_bytes())
record.update(artifact_identity=artifact["artifact_identity"], rules_version=artifact["rules_version"])
with (root / ("drills-" + digest(dumps(record)) + ".json")).open("x", encoding="utf-8") as stream:
    stream.write(dumps(record))
scoring._receipt(settings, "v5_operation_drill", record["id"], record)
print(dumps(record), flush=True)
