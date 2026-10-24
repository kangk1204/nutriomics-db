"""A minutely tick and a durable due ledger; no scheduler daemon is needed."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import connect

SEOUL = ZoneInfo("Asia/Seoul")
MAX_ATTEMPTS = 3
STALE_AFTER = timedelta(hours=6)


def _latest_slots(current):
    def daily(hour):
        slot = current.replace(hour=hour, minute=0, second=0, microsecond=0)
        return slot if slot <= current else slot - timedelta(days=1)

    def weekly(weekday, hour):
        slot = current.replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(
            days=(current.weekday() - weekday) % 7
        )
        return slot if slot <= current else slot - timedelta(days=7)

    month = current.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    if month > current:
        previous = month - timedelta(days=1)
        month = previous.replace(day=1)
    return {
        "collect": daily(14),
        "report": weekly(0, 15),
        "oa-recheck": weekly(6, 13),
        "reconcile": month,
        "backup": daily(16),
    }


def tick(settings, runner=None, clock=None):
    current = (clock or (lambda: datetime.now(UTC)))()
    if current.tzinfo is None:
        raise ValueError("Scheduler clock must be timezone-aware")
    current = current.astimezone(SEOUL)
    timestamp = current.astimezone(UTC).isoformat()
    with connect(settings) as con:
        con.execute("BEGIN IMMEDIATE")
        for row in con.execute("SELECT * FROM jobs WHERE status='running'").fetchall():
            started = datetime.fromisoformat(row["started_at"])
            if current - started >= STALE_AFTER:
                con.execute(
                    "UPDATE jobs SET status='failed',finished_at=?,due_at=?,error=? WHERE job_key=?",
                    (timestamp, timestamp, "stale running job recovered after 6 hours", row["job_key"]),
                )
        for kind, slot in _latest_slots(current).items():
            key = kind + ":" + slot.strftime("%Y%m%dT%H%M%S%z")
            con.execute(
                "UPDATE jobs SET status='superseded',finished_at=?,error=? "
                "WHERE kind=? AND job_key<>? AND status IN ('pending','failed') AND attempts<?",
                (timestamp, "coalesced into latest elapsed slot", kind, key, MAX_ATTEMPTS),
            )
            con.execute(
                "INSERT OR IGNORE INTO jobs(job_key,kind,due_at,status) VALUES(?,?,?,'pending')",
                (key, kind, slot.astimezone(UTC).isoformat()),
            )
    results = []
    while True:
        with connect(settings) as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
                break
            job = con.execute(
                "SELECT * FROM jobs WHERE status IN ('pending','failed') AND due_at<=? "
                "AND attempts<? ORDER BY due_at,job_key LIMIT 1",
                (timestamp, MAX_ATTEMPTS),
            ).fetchone()
            if not job:
                break
            attempt = job["attempts"] + 1
            con.execute(
                "UPDATE jobs SET status='running',started_at=?,finished_at=NULL,attempts=?,error=NULL WHERE job_key=?",
                (timestamp, attempt, job["job_key"]),
            )
        error = None
        try:
            if runner:
                result = runner(job["kind"])
                code = getattr(result, "returncode", result if isinstance(result, int) else 0)
                if code:
                    raise RuntimeError(f"{job['kind']} exited {code}")
            else:
                environment = {
                    **os.environ,
                    "LITERATURE_HOME": str(settings.home),
                    "LITERATURE_CONFIG": str(settings.config),
                }
                subprocess.run(
                    [sys.executable, "-m", "nutri_literature.cli", job["kind"]],
                    check=True,
                    timeout=STALE_AFTER.total_seconds(),
                    env=environment,
                )
        except Exception as exc:  # noqa: BLE001 -- preserve the due ledger on any command failure.
            error = str(exc)[:2000]
        status = "failed" if error else "succeeded"
        completed = (clock or (lambda: datetime.now(UTC)))().astimezone(UTC)
        retry = (completed + timedelta(minutes=5 if attempt == 1 else 15)).isoformat()
        with connect(settings) as con:
            con.execute(
                "UPDATE jobs SET status=?,finished_at=?,due_at=?,error=? WHERE job_key=? AND status='running'",
                (status, completed.isoformat(), retry if error else job["due_at"], error, job["job_key"]),
            )
        results.append(
            {
                "job_key": job["job_key"],
                "kind": job["kind"],
                "status": status,
                "attempts": attempt,
                "error": error,
            }
        )
    return {"timezone": "Asia/Seoul", "at": timestamp, "jobs": results}
