from nutri_literature.trial_linking_v6 import bind_arm_conditions, resolve_arms, timepoints


def span(text, surface):
    start = text.index(surface)
    return {"text": surface, "start": start, "end": start + len(surface), "sentence_start": 0,
            "sentence_end": len(text), "score": 1.0}


def test_same_compound_doses_are_not_assigned_to_both_arms():
    text = "Participants received caffeine 10 mg, caffeine 20 mg, or placebo."
    doc = {"pmid": "example", "text": text}
    arms = [span(text, name) for name in ("caffeine 10 mg", "caffeine 20 mg", "placebo")]
    registry = bind_arm_conditions(doc, arms, [{"start": 0, "end": len(text), "text": text}])
    assert [[q["text"] for q in a["doses"]] for a in registry] == [["10 mg"], ["20 mg"], []]
    assert not resolve_arms({"text": "Caffeine lowered pressure compared with placebo."}, registry)[:-1]
    linked = resolve_arms({"text": "Caffeine 10 mg lowered pressure compared with placebo."}, registry)
    assert [a["description"]["text"] for a in linked] == ["caffeine 10 mg", "placebo"]


def test_followup_times_preserve_multiple_candidates_not_one_arbitrary_time():
    text = "Pressure was lower at week 4 and week 8."
    points = timepoints({"text": text}, {"start": 0, "end": len(text), "text": text})
    assert [p["text"] for p in points] == ["at week 4", "week 8"]
    text = "Participants received caffeine for 8 weeks."
    assert timepoints({"text": text}, {"start": 0, "end": len(text), "text": text}) == []


def test_quantity_source_offsets_survive_unicode():
    text = "α participants received vitamin D 10 μg and placebo."
    arms = [span(text, v) for v in ("vitamin D 10 μg", "placebo")]
    registry = bind_arm_conditions({"pmid": "unicode", "text": text}, arms,
                                   [{"start": 0, "end": len(text), "text": text}])
    assert registry[0]["doses"][0]["text"] == "10 μg"
    for arm in registry:
        for q in arm["doses"]:
            assert text[q["start"]:q["end"]] == q["text"]
