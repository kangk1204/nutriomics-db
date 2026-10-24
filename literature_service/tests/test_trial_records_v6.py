from nutri_literature.trial_records_v6 import build_registry, link_results


def span(text, word, start=0):
    lo = text.index(word, start)
    return {"start": lo, "end": lo + len(word), "text": word, "score": .95}


def test_same_bare_drug_at_two_regimens_never_loses_second_arm():
    methods = "Participants received caffeine at 10 mg. Other participants received caffeine at 20 mg. "
    text = methods + "Caffeine reduced blood pressure compared with placebo at week 4."
    roles = {"intervention": [span(text, "caffeine"), span(text, "caffeine", text.index("Other"))], "comparator": []}
    doc = {"pmid": "synthetic", "text": text, "sections": [{"label": "METHODS", "start": 0, "end": len(methods)}, {"label": "RESULTS", "start": len(methods), "end": len(text)}]}
    arms = build_registry(doc, roles, roles["intervention"])
    assert len(arms) == 2
    assert [[q["text"] for q in a["doses"]] for a in arms] == [["10 mg"], ["20 mg"]]
    assert len({a["arm_instance_id"] for a in arms}) == 2
    assert len({a["regimen_signature"] for a in arms}) == 2
    results = link_results(doc, arms)
    assert results[0]["arm_instance_ids"] == []
    assert len(results[0]["arm_candidates"]) == 2


def test_duration_is_not_followup_and_explicit_result_dose_resolves_arm():
    methods = "Participants received caffeine at 10 mg for 4 weeks. Other participants received caffeine at 20 mg for 8 weeks. "
    text = methods + "Caffeine 10 mg reduced pressure compared with placebo at week 6."
    roles = {"intervention": [span(text, "caffeine"), span(text, "caffeine", text.index("Other"))], "comparator": []}
    doc = {"pmid": "synthetic", "text": text, "sections": [{"label": "METHODS", "start": 0, "end": len(methods)}, {"label": "RESULTS", "start": len(methods), "end": len(text)}]}
    arms = build_registry(doc, roles, roles["intervention"])
    assert arms[0]["duration_candidates"][0]["text"] == "for 4 weeks"
    result = link_results(doc, arms)[0]
    assert result["arm_instance_ids"] == [arms[0]["arm_instance_id"]]
    assert result["timepoint"]["text"] == "at week 6"


def test_same_dose_with_different_frequency_cannot_be_merged():
    methods = "Participants received caffeine 10 mg orally daily for 4 weeks. Other participants received caffeine 10 mg orally weekly for 4 weeks. "
    text = methods + "Caffeine 10 mg reduced pressure compared with placebo at week 6."
    roles = {"intervention": [span(text, "caffeine"), span(text, "caffeine", text.index("Other"))], "comparator": []}
    doc = {"pmid": "synthetic", "text": text, "sections": [{"label": "METHODS", "start": 0, "end": len(methods)}, {"label": "RESULTS", "start": len(methods), "end": len(text)}]}
    arms = build_registry(doc, roles, roles["intervention"])
    assert all(a["regimen_complete"] for a in arms)
    assert len({a["regimen_signature"] for a in arms}) == 2
    assert link_results(doc, arms)[0]["arm_instance_ids"] == []
