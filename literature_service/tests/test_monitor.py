from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import requests
from nutri_literature.db import initialize, connect, dumps
from nutri_literature.settings import Settings
from nutri_literature.monitor import check, status


def setup(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(dumps({"topics": []}))
    settings = Settings(tmp_path / "runtime", config)
    initialize(settings)
    return settings


def test_one_check_cannot_claim_seven_days(tmp_path):
    settings = setup(tmp_path)
    clock = datetime(2026, 10, 1, tzinfo=timezone.utc)
    result = check(settings, lambda *a, **k: SimpleNamespace(status_code=200), clock)
    assert result["status"] == "observing" and result["samples"] == 1
    assert status(settings, clock + timedelta(days=7))["status"] == "failed"


def test_recovery_can_pass_a_later_seven_day_window_without_erasing_failure(tmp_path):
    settings = setup(tmp_path)
    clock = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with connect(settings) as con:
        con.executemany(
            "INSERT INTO health_checks VALUES(?,?,?,?)",
            [((clock + timedelta(minutes=5 * i)).isoformat(), int(i != 0), 1, "{}") for i in range(2305)],
        )
    result = status(settings, clock + timedelta(days=8))
    assert result["status"] == "passed"
    assert result["historical_failed_samples"] == 1 and result["failed_samples"] == 0


def test_real_complete_sample_series_only_passes(tmp_path):
    settings = setup(tmp_path)
    clock = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with connect(settings) as con:
        con.executemany(
            "INSERT INTO health_checks VALUES(?,?,?,?)",
            [((clock + timedelta(minutes=5 * i)).isoformat(), 1, 1, "{}") for i in range(2017)],
        )
    assert status(settings, clock + timedelta(days=7))["status"] == "passed"
    with connect(settings) as con:
        con.execute("UPDATE health_checks SET api_up=0 WHERE observed_at=?", (clock.isoformat(),))
    assert status(settings, clock + timedelta(days=7))["status"] == "failed"


def test_timeout_and_http_failure_diagnostics_do_not_change_availability_history(tmp_path):
    settings = setup(tmp_path)
    clock = datetime(2026, 10, 1, 0, 0, 35, tzinfo=timezone.utc)

    def failures(url, timeout):
        assert timeout == 5
        if ":8010/" in url:
            raise requests.Timeout("Synthetic fixture timeout")
        return SimpleNamespace(status_code=500)

    observed = check(settings, failures, clock)
    assert observed["status"] == "failed" and observed["historical_failed_samples"] == 1
    with connect(settings) as con:
        row = dict(con.execute("SELECT * FROM health_checks").fetchone())
    detail = json.loads(row["detail"])
    assert detail["api"] is False and detail["ui"] is False
    assert detail["diagnostics"]["api"] == {"http_status": None, "error_type": "Timeout"}
    assert detail["diagnostics"]["ui"] == {"http_status": 500, "error_type": None}
    assert row["observed_at"] == clock.replace(second=0, microsecond=0).isoformat()
    # INSERT OR IGNORE must preserve the failed sample and original diagnostics.
    check(settings, lambda *a, **k: SimpleNamespace(status_code=200), clock)
    with connect(settings) as con:
        preserved = dict(con.execute("SELECT * FROM health_checks").fetchone())
    assert preserved == row
