"""Observed five-minute availability; never substitute simulated seven-day uptime."""

from datetime import datetime, timedelta, timezone
import requests
from .db import connect, dumps


def check(settings, request=requests.get, clock=None):
    current = (clock or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    results = {}
    diagnostics = {}
    for name, url in [
        ("api", "http://127.0.0.1:8010/healthz"),
        ("ui", "http://127.0.0.1:8510/_stcore/health"),
    ]:
        try:
            code = request(url, timeout=5).status_code
            results[name] = code == 200
            diagnostics[name] = {"http_status": code, "error_type": None}
        except requests.RequestException as error:
            results[name] = False
            diagnostics[name] = {"http_status": None, "error_type": type(error).__name__}
    results["diagnostics"] = diagnostics
    with connect(settings) as con:
        con.execute(
            "INSERT OR IGNORE INTO health_checks VALUES(?,?,?,?)",
            (current.isoformat(), int(results["api"]), int(results["ui"]), dumps(results)),
        )
    return status(settings, current)


def status(settings, clock=None):
    current = clock or datetime.now(timezone.utc)
    with connect(settings) as con:
        rows = con.execute("SELECT * FROM health_checks ORDER BY observed_at").fetchall()
    if not rows:
        return {"status": "pending", "samples": 0, "required_days": 7}
    historical_first = datetime.fromisoformat(rows[0]["observed_at"])
    historical_failures = sum(not (r["api_up"] and r["ui_up"]) for r in rows)
    cutoff = current - timedelta(days=7)
    anchor = max(
        (i for i, r in enumerate(rows) if datetime.fromisoformat(r["observed_at"]) <= cutoff), default=0
    )
    rows = rows[anchor:]
    if not rows:
        return {
            "status": "failed",
            "samples": 0,
            "required_days": 7,
            "first_observed_at": historical_first.isoformat(),
            "note": "최근 7일 관측 없음",
        }
    first = datetime.fromisoformat(rows[0]["observed_at"])
    elapsed = current - first
    moments = [datetime.fromisoformat(r["observed_at"]) for r in rows]
    gaps = [(b - a).total_seconds() for a, b in zip(moments, moments[1:])]
    gaps.append((current - moments[-1]).total_seconds())
    failures = sum(not (r["api_up"] and r["ui_up"]) for r in rows)
    passed = (
        elapsed >= timedelta(days=7) and failures == 0 and max(gaps, default=0) <= 600 and len(rows) >= 2016
    )
    return {
        "status": "passed" if passed else "failed" if failures or max(gaps, default=0) > 600 else "observing",
        "samples": len(rows),
        "first_observed_at": first.isoformat(),
        "last_observed_at": moments[-1].isoformat(),
        "elapsed_hours": round(elapsed.total_seconds() / 3600, 2),
        "failed_samples": failures,
        "historical_failed_samples": historical_failures,
        "monitoring_started_at": historical_first.isoformat(),
        "maximum_gap_seconds": max(gaps, default=0),
        "required_days": 7,
        "note": "실제 관측값 기반. 전문가 검수·서버 재부팅 검증과 별도입니다.",
    }
