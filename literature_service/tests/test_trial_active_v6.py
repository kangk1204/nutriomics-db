from nutri_literature.trial_active_v6 import explicit_active_pair, respective_doses


def arm(text, name, role):
    start = text.index(name)
    return {"core": [name.casefold()], "source_role": role, "description": {"start": start, "end": start + len(name), "text": name},
            "source_sentence": {"start": 0, "end": len(text), "text": text}, "doses": []}


def test_active_comparator_requires_learned_roles_and_explicit_source_direction():
    text = "DrugA lowered pressure compared with DrugB at week 4."
    a, b = arm(text, "DrugA", "intervention"), arm(text, "DrugB", "comparator")
    assert explicit_active_pair({"text": text}, a, b)
    assert not explicit_active_pair({"text": "DrugA and DrugB were studied."}, a, b)
    assert not explicit_active_pair({"text": text}, b, a)
    assert explicit_active_pair({"text": "Compared with DrugB, DrugA lowered pressure."}, a, b)


def test_respectively_dose_units_preserve_original_locations():
    text = "Participants received DrugA and DrugB at 10 and 20 mg, respectively."
    a, b = arm(text, "DrugA", "intervention"), arm(text, "DrugB", "comparator")
    values = respective_doses({"text": text}, [a, b])
    assert [a["doses"][0]["text"] for a in values] == ["10", "20"]
    assert all(a["doses"][0]["unit_evidence"]["text"] == "mg" for a in values)
    for a in values:
        for q in (a["doses"][0], a["doses"][0]["unit_evidence"]):
            assert text[q["start"]:q["end"]] == q["text"]
