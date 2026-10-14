import xml.etree.ElementTree as ET

import pytest

from nutrigenomics.primary_tables import mean_sem, rendered, tables


def test_not_detectable_is_not_measured_zero_or_imputed():
    row = mean_sem("Not detectable")
    assert row["value_status"] == "below_detection" and row["mean"] is None and row["sem"] is None
    assert mean_sem("0.0 ± 0.1")["value_status"] == "measured_zero"


def test_superscript_unit_is_preserved():
    assert (
        rendered(ET.fromstring("<td>Leukocytes (10<sup>9</sup>/l)</td>")) == "Leukocytes (10^9/l)"
    )


@pytest.mark.parametrize("value", ["1.0 ± unknown", "1.0 (0.1)", "not reported", "1.0 ± -0.2"])
def test_unrecognized_uncertainty_is_not_reinterpreted(value):
    with pytest.raises(ValueError):
        mean_sem(value)


def test_table_denominators_and_original_units_are_preserved():
    from pathlib import Path

    path = Path("research/2026-09-30/expansion/cohort-primary/GSE32357/PMC3880862-BioC.xml")
    if not path.exists():
        pytest.skip("Optional captured source unavailable")
    rows = tables(path.read_bytes())
    energy = next(row for row in rows if row["label"] == "24h Energy expenditure")
    bp = next(row for row in rows if row["label"] == "Systolic blood pressure")
    crp = next(row for row in rows if row["label"] == "CRP")
    assert energy["n_people"] == 10 and bp["n_people"] == 11
    assert bp["resveratrol"]["mean"] == 124.7 and bp["resveratrol"]["sem"] == 3.1
    assert crp["unit"] == "ng/ml" and crp["p_value"] == 0.11
