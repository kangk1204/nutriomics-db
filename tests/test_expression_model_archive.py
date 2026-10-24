"""Archive-only producer additions must preserve the historical analysis recipe."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_dedicated_producers_save_actual_states_and_weights():
    sources = {name: (ROOT / f"scripts/analyze_{name}.R").read_text(encoding="utf-8")
               for name in ("gse127530", "gse56960", "gse54325")}
    assert "voom_object=voom_fit" in sources["gse127530"]
    assert "correlation=block_correlation$consensus.correlation" in sources["gse127530"]
    assert "correlation=correlation$consensus.correlation" in sources["gse56960"]
    assert 'contrast_application="identity_untransformed"' in sources["gse54325"]
    assert 'channel_direction=list(Cy3="baseline", Cy5="8 weeks")' in sources["gse54325"]
    for source in sources.values():
        assert "model_archive = model_archive" in source
        assert '"reanalysis_inputs.rds"' in source
        assert "helper_sha256 = expression_archive_sha256(archive_helper_path)" in source
        assert "set.seed(20261001L)" in source


def test_real_small_r_weighted_correlated_and_two_colour_archive():
    binary = shutil.which("Rscript")
    if not binary:
        pytest.skip("Run synthetic R check in project R environment")
    available = subprocess.run([binary, "--vanilla", "-e",
        "quit(status=if(requireNamespace('limma',quietly=TRUE)&&requireNamespace('statmod',quietly=TRUE)&&requireNamespace('openssl',quietly=TRUE))0 else 2)"],
        check=False, capture_output=True, text=True, timeout=30)
    if available.returncode:
        pytest.skip("Locked limma/statmod/openssl required; no package installation")
    result = subprocess.run([binary, "--vanilla", "tests/check_expression_model_archive.R"],
                            cwd=ROOT, check=False, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "original fit numbers/RNG preserved" in result.stdout
    cli = subprocess.run([binary, "--vanilla", "tests/check_saved_expression_replay.R"],
                         cwd=ROOT, check=False, capture_output=True, text=True, timeout=60)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert "numeric/hash guards passed" in cli.stdout


@pytest.mark.parametrize("study,expected", [
    ("127530", "7d4af16bb300d73ff49960b67c4b334b1452b3c2f730b0a2a64f61bde0bb60b1"),
    ("56960", "a299c43bf0ced378c3a77272811f32ae6b4f745ce350f699f14a33a13e1128b2"),
    ("54325", "ad5686c00f9f86be833d27367ce8e3e25f3e80081dc7a5d492008478c8c9ae13"),
])
def test_entire_original_analysis_ast_preserved(study, expected):
    import hashlib

    historical = ROOT / f"source_snapshots/review_evidence/{expected}/analyze_gse{study}.R"
    assert hashlib.sha256(historical.read_bytes()).hexdigest() == expected
    binary = shutil.which("Rscript")
    if not binary:
        pytest.skip("Run AST-only check using locked project R")
    result = subprocess.run([binary, "--vanilla", "tests/check_expression_archive_only_ast.R",
        str(historical), f"scripts/analyze_gse{study}.R"], cwd=ROOT, capture_output=True,
        text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
