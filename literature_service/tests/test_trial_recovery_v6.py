from nutri_literature import trial_recovery_v6 as trial


def location(text, value, score=.95):
    start = text.index(value)
    return {"text": value, "start": start, "end": start + len(value), "score": score}


def setup(monkeypatch, result):
    method = "Participants were randomized to receive vitamin D 10 μg or placebo. "
    text = method + result
    document = {"pmid": "synthetic", "text": text, "sections": [
        {"start": 0, "end": len(method), "label": "METHODS"},
        {"start": len(method), "end": len(text), "label": "RESULTS"}]}
    roles = {"intervention": [location(text, "vitamin D 10 μg")], "comparator": [location(text, "placebo")],
             "outcome": [location(text, "systolic blood pressure")]}
    chemicals = [location(text, "vitamin D"), location(text, "placebo")]
    monkeypatch.setattr(trial, "predict", lambda *args: (roles, chemicals))
    return document


def test_regimen_and_followup_bind_across_methods_and_results_without_gene_id(monkeypatch):
    doc = setup(monkeypatch, "Vitamin D reduced systolic blood pressure compared with placebo at week 4.")
    values, trace = trial.comparisons(doc, {}, {})
    assert values
    condition = trace["observations"][0]["conditions"]
    assert condition["intervention_doses"][0]["text"] == "10 μg"
    assert condition["timepoint"]["text"] == "at week 4"
    assert values[0]["status"] == "needs_review"
    assert values[0]["intervention_description"] == "vitamin D 10 μg"


def test_multiple_followups_remain_ambiguous_and_plans_never_become_results(monkeypatch):
    doc = setup(monkeypatch, "Vitamin D reduced systolic blood pressure compared with placebo at week 4 and week 8.")
    _, trace = trial.comparisons(doc, {}, {})
    assert trace["observations"]
    condition = trace["observations"][0]["conditions"]
    assert condition["timepoint"] is None
    assert condition["hold_reasons"] == ["multiple_timepoints"]
    doc = setup(monkeypatch, "We will compare systolic blood pressure between vitamin D and placebo at week 4.")
    values, trace = trial.comparisons(doc, {}, {})
    assert values == [] and trace["observations"] == []


def test_unique_document_pair_can_resolve_explicit_treatment_group_reference(monkeypatch):
    doc = setup(monkeypatch, "The treatment group had lower systolic blood pressure than the placebo group at week 4 (P = 0.03).")
    values, trace = trial.comparisons(doc, {}, {})
    assert values
    assert trace["observations"][0]["conditions"]["binding_rule"] == "unique_document_two_arm_group_reference"
