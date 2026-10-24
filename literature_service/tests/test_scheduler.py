from datetime import datetime, timedelta

import pytest

from nutri_literature.db import connect, initialize
from nutri_literature.scheduler import tick
from nutri_literature.settings import PROJECT, Settings
from scripts.cron_install import BEGIN, END, cron_block, replace_block


@pytest.fixture
def settings(tmp_path):
    settings = Settings(tmp_path / "runtime", PROJECT / "config/topics.json")
    initialize(settings)
    return settings


def test_korean_daily_and_monday_slots_are_idempotent(settings):
    current = datetime.fromisoformat("2026-10-05T13:59:00+09:00")
    tick(settings, runner=lambda _: 0, clock=lambda: current)
    current += timedelta(minutes=1)
    collected = tick(settings, runner=lambda _: 0, clock=lambda: current)
    assert [job["kind"] for job in collected["jobs"]] == ["collect"]
    assert tick(settings, runner=lambda _: 0, clock=lambda: current)["jobs"] == []
    current += timedelta(hours=1)
    report = tick(settings, runner=lambda _: 0, clock=lambda: current)
    assert [job["kind"] for job in report["jobs"]] == ["report"]
    assert report["timezone"] == "Asia/Seoul"
    assert report["jobs"][0]["job_key"] == "report:20261005T150000+0900"


def test_failed_jobs_retry_with_backoff_and_stop_after_three_attempts(settings):
    current = datetime.fromisoformat("2026-10-05T14:00:00+09:00")
    called = []

    def runner(kind):
        called.append(kind)
        return 1 if kind == "collect" else 0

    first = tick(settings, runner=runner, clock=lambda: current)
    assert next(job for job in first["jobs"] if job["kind"] == "collect")["attempts"] == 1
    current += timedelta(minutes=4)
    assert tick(settings, runner=runner, clock=lambda: current)["jobs"] == []
    current += timedelta(minutes=1)
    assert tick(settings, runner=runner, clock=lambda: current)["jobs"][0]["attempts"] == 2
    current += timedelta(minutes=15)
    assert tick(settings, runner=runner, clock=lambda: current)["jobs"][0]["attempts"] == 3
    current += timedelta(minutes=30)
    assert tick(settings, runner=runner, clock=lambda: current)["jobs"] == []
    assert called.count("collect") == 3


def test_missed_slots_coalesce_and_stale_job_recovers(settings):
    current = datetime.fromisoformat("2026-10-05T20:00:00+09:00")
    with connect(settings) as con:
        con.execute(
            "INSERT INTO jobs(job_key,kind,due_at,status,started_at,attempts) "
            "VALUES('collect:20261005T140000+0900','collect',?,'running',?,1)",
            ("2026-10-05T05:00:00+00:00", "2026-10-05T04:00:00+00:00"),
        )
        con.execute(
            "INSERT INTO jobs(job_key,kind,due_at,status) VALUES('older','report',?,'pending')",
            ("2026-09-01T00:00:00+00:00",),
        )
    result = tick(settings, runner=lambda _: 0, clock=lambda: current)
    collect = next(job for job in result["jobs"] if job["kind"] == "collect")
    assert collect["attempts"] == 2 and collect["status"] == "succeeded"
    with connect(settings) as con:
        assert con.execute("SELECT status FROM jobs WHERE job_key='older'").fetchone()[0] == "superseded"
    assert len(result["jobs"]) == 5  # latest single elapsed slot per kind


def test_live_running_job_prevents_parallel_command_launch(settings):
    current = datetime.fromisoformat("2026-10-05T14:00:00+09:00")
    with connect(settings) as con:
        con.execute(
            "INSERT INTO jobs(job_key,kind,due_at,status,started_at,attempts) "
            "VALUES('busy','collect',?,'running',?,1)",
            ("2026-10-05T05:00:00+00:00", "2026-10-05T05:00:00+00:00"),
        )
    assert (
        tick(settings, runner=lambda _: pytest.fail("Should not launch"), clock=lambda: current)["jobs"] == []
    )


def test_cron_block_preserves_other_entries_and_is_replaceable():
    original = "MAILTO=admin\n0 3 * * * /usr/bin/backup\n"
    block = cron_block()
    once = replace_block(original, block)
    assert original.strip() in once
    assert replace_block(once, block) == once
    assert once.count(BEGIN) == 1 and once.count(END) == 1
    assert "@reboot" in once and "flock -n" in once
    assert "/bin/sh -c 'LITERATURE_HOME=" in once
    with pytest.raises(ValueError, match="Unmatched"):
        replace_block(BEGIN + "\ntruncated", block)
