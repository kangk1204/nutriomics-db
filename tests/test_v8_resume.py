import importlib.util
from pathlib import Path


path = Path(__file__).resolve().parents[1] / "scripts/resume_evidence_v8.py"
spec = importlib.util.spec_from_file_location("v8_resume", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_interrupted_live_experiments_may_resume():
    for status in ("running", "waiting"):
        assert module.interrupted({"status": status})
        for guard in ("supervisor_alive", "child_alive", "predictions_ready"):
            assert not module.interrupted({"status": status, guard: True})
    for status in ("failed", "cancelled", "resource_budget_exceeded", "waiting_for_input_or_repair", "predictions_ready"):
        assert not module.interrupted({"status": status})
