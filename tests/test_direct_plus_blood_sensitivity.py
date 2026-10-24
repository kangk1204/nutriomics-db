"""Small cell-covariate/rank/memory guards, separate from actual reference run."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_source_exposes_full_cohort_scope_and_explicit_noncausal_failure():
    source = (ROOT / "scripts/analyze_direct_plus_blood_sensitivity.R").read_text(encoding="utf-8")
    assert source.count("counts<-estimateCellCounts(rg,") == 1
    assert "paired$delta_m" not in source and "paired$delta_beta" not in source
    assert "eBayes(dp_join_fits(" in source
    assert "mem.maxVSize(vector_cap*1024)" in source
    assert 'source_arrays=512L' in source
    assert 'primary_result_modified=FALSE' in source
    assert 'scientific_approval=FALSE' in source
    assert "not equivalent to independently normalized chunks" in source


def test_synthetic_cell_adjusted_fit_rank_and_resource_guards():
    binary = shutil.which("Rscript")
    if not binary:
        pytest.skip("Run small R test in the locked project environment")
    available = subprocess.run([binary, "--vanilla", "-e",
        "quit(status=if(requireNamespace('limma',quietly=TRUE)&&requireNamespace('statmod',quietly=TRUE))0 else 2)"],
        capture_output=True, text=True, timeout=30, check=False)
    if available.returncode:
        pytest.skip("Project limma/statmod are required; no package installation performed")
    result = subprocess.run([binary, "--vanilla", "tests/check_direct_plus_blood_sensitivity.R"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic tests passed" in result.stdout


def test_missing_inputs_fail_closed_without_writing_result_metadata(tmp_path):
    binary = shutil.which("Rscript")
    if not binary:
        pytest.skip("Rscript unavailable")
    available = subprocess.run([binary, "--vanilla", "-e",
        "quit(status=if(requireNamespace('jsonlite',quietly=TRUE))0 else 2)"],
        capture_output=True, text=True, timeout=30, check=False)
    if available.returncode:
        pytest.skip("jsonlite unavailable")
    result = subprocess.run([binary, "--vanilla", "scripts/analyze_direct_plus_blood_sensitivity.R",
        "--output-dir", str(tmp_path), "--root", str(tmp_path), "--input-dir", str(tmp_path),
        "--environment-lock", str(tmp_path / "missing.lock")],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2
    receipt = json.loads((tmp_path / "sensitivity_status.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert receipt["primary_result_modified"] is False and receipt["scientific_approval"] is False
    assert not (tmp_path / "analysis_metadata.json").exists()
    assert not (tmp_path / "cpg_effects.tsv.gz").exists()
