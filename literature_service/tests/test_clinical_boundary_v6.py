from nutri_literature.clinical_boundary_v6 import span_features, variants
from nutri_literature.refinement_v6 import tokens


def test_boundary_variants_recover_dose_and_parenthesis_without_crossing_sentence():
    text = "Subjects received drug 60 mg daily (ABC). Next trial."
    sequence = tokens(text)
    spans = variants(2, 3, len(sequence), (0, 7))
    surfaces = {text[sequence[a]["start"] : sequence[b - 1]["end"]] for a, b in spans}
    assert "drug 60 mg daily" in surfaces
    assert "drug 60 mg daily (ABC" in surfaces
    assert not any("Next" in s for s in surfaces)
    assert all(0 <= a < b <= 7 for a, b in spans)


def test_boundary_features_distinguish_dose_and_noun_measurement():
    text = "DSG 1% versus placebo reduced the mean ankle swelling."
    sequence = tokens(text)
    assert span_features(text, sequence, 0, 2, "arms")["has_number"]
    measurement = span_features(text, sequence, 6, 9, "outcome")
    assert measurement["starts_measurement"]
    assert measurement["role"] == "outcome"
    assert "inside=swelling" in measurement
