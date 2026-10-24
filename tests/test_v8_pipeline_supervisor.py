import importlib.util
import json
from pathlib import Path

import pytest


path = Path(__file__).resolve().parents[1] / "scripts/run_evidence_v8_pipeline.py"
spec = importlib.util.spec_from_file_location("v8_pipeline_supervisor", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_training_and_source_only_prediction_stages():
    for stage in module.STAGES:
        command = module.command(stage, Path("/source"), Path("/home"), Path("/assets"), "python")
        assert "evaluate" not in command
        assert "sealed" not in command
    with pytest.raises(ValueError):
        module.command("evaluate", Path("/source"), Path("/home"), Path("/assets"), "python")


def test_pinned_source_hash_and_escape(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    file = source / "model.py"
    file.write_text("version = 8")
    manifest = source / "source-manifest.json"
    manifest.write_text(json.dumps({"model.py": module.sha(file)}))
    module.verify_source(source.resolve())
    file.write_text("version = 9")
    with pytest.raises(ValueError, match="integrity"):
        module.verify_source(source.resolve())
    outside = tmp_path / "outside"
    outside.write_text("secret")
    manifest.write_text(json.dumps({"../outside": module.sha(outside)}))
    with pytest.raises(ValueError, match="integrity"):
        module.verify_source(source.resolve())


def test_native_stages_use_separate_cli_and_never_gold_evaluation():
    for stage in module.NATIVE_STAGES:
        command = module.command(stage, Path("/source"), Path("/owned"), Path("/assets"), "python", native=True)
        assert command[3].endswith("evidence_v8_native.py")
        assert "evaluate" not in command
