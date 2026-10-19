"""A failed or abbreviated performance series cannot be plotted as a full pass."""
from scripts.audit_submission_artifacts import performance_rows_match


def test_latency_values_do_not_hide_errors_or_change_denominators():
    metrics = {"runs_per_case": 100, "results": [
        {"id": "first-timeout", "metrics": {"n": 1, "p95_s": 10.02, "errors": 1}, "target_s": 1.0},
        {"id": "full-series", "metrics": {"n": 100, "p95_s": 0.42, "errors": 0}, "target_s": 3.0},
    ]}
    rows = [dict(zip(["case", "measured_requests", "p95_seconds", "target_seconds", "errors",
                      "complete_error_free_series"], values, strict=True)) for values in [
        ["first-timeout", "1", "10.02", "1.0", "1", "False"],
        ["full-series", "100", "0.42", "3.0", "0", "True"],
    ]]
    assert performance_rows_match(rows, metrics)
    assert not performance_rows_match(list(reversed(rows)), metrics)
    for field, value in {"measured_requests": "100", "p95_seconds": "0.2", "target_seconds": "15",
                         "errors": "0", "complete_error_free_series": "True"}.items():
        changed = [{**rows[0], field: value}, rows[1]]
        assert not performance_rows_match(changed, metrics)
