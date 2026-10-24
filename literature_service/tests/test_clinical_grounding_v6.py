from nutri_literature.clinical_grounding_v6 import source_surface, measured_comparison, arms_grounded


def test_percent_and_parentheses_are_preserved_as_source_not_invented():
    text = "ABC 1% (active) versus placebo."
    assert source_surface(text, {"start": 0, "end": 5, "text": "ABC 1"})["text"] == "ABC 1%"
    assert source_surface(text, {"start": 7, "end": 14, "text": "(active"})["text"] == "(active)"
    assert source_surface(text, {"start": 0, "end": 3, "text": "ABC"})["text"] == "ABC"


def test_actual_quantified_had_comparison_is_observed():
    sentence = {
        "text": "Patients receiving treatment had greater weight gain than placebo (2.1 vs. 0.3 kg).",
        "label": "Results",
    }
    assert measured_comparison(sentence)


def test_future_background_and_p_values_are_not_observations():
    for text, label in [
        ("We will test whether patients had greater values than placebo (2.1 vs. 0.3).", "Results"),
        ("Patients had greater values than placebo (2.1 vs. 0.3).", "Background"),
        ("The p values were 0.01 and 0.03.", "Results"),
    ]:
        assert not measured_comparison({"text": text, "label": label})


def test_both_arm_anchors_required_and_same_arm_rejected():
    sentence = {"text": "TreatmentA had greater gains than placebo (2 vs. 1)."}
    assert arms_grounded(sentence, "TreatmentA", "placebo")
    assert not arms_grounded(sentence, "TreatmentB", "placebo")
    assert not arms_grounded(sentence, "placebo", "placebo group")


def test_different_doses_require_both_quantities_in_source():
    assert arms_grounded({"text": "Drug 10 mg was compared with drug 20 mg."}, "drug 10 mg", "drug 20 mg")
    assert not arms_grounded({"text": "Drug 10 mg was compared with placebo."}, "drug 10 mg", "drug 20 mg")
