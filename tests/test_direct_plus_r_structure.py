"""Executable synthetic R parity is separate from actual IDAT/run validation."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_actual_source_does_not_collect_all_normalization_chunks():
    source = (ROOT / "scripts/analyze_direct_plus.R").read_text(encoding="utf-8")
    assert "chunks<-lapply" not in source
    assert "dp_stream_qc(chunk_paths,sample_info)" in source
    assert "dp_write_tiles(" in source
    assert "eBayes(dp_join_fits(" in source
    assert '"baseline_plate","followup_plate","baseline_position","followup_position"' in source
    assert 'fitted_m_global_ebayes.rds' in source
    assert '"--environment-lock"' in source
    assert 'scientific_approval=FALSE' in source
    assert 'legacy_source_verified' not in source


def test_synthetic_r_exact_global_fit_and_structural_guards():
    binary = shutil.which("Rscript")
    if binary is None:
        pytest.skip("Rscript unavailable; run tests/check_direct_plus_helpers.R in locked project R")
    available = subprocess.run([binary, "--vanilla", "-e",
        "quit(status=if(requireNamespace('limma',quietly=TRUE)&&requireNamespace('statmod',quietly=TRUE)&&requireNamespace('openssl',quietly=TRUE))0 else 2)"],
        capture_output=True, text=True, timeout=30, check=False)
    if available.returncode:
        pytest.skip("Synthetic R check requires installed limma/statmod/openssl; no package installation performed")
    result = subprocess.run([binary, "--vanilla", "tests/check_direct_plus_helpers.R"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DIRECT PLUS synthetic tests passed" in result.stdout
