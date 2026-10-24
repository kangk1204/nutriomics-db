from nutri_literature import trial_composition_v6 as trial
from nutri_literature.trial_linking_v6 import resolve_arms


def test_combined_regimen_cannot_inherit_single_drug_identity(monkeypatch):
    text = "DrugA plus DrugB"
    description = {"start": 0, "end": len(text), "text": text}
    chemical = [{"start": 0, "end": 5, "text": "DrugA"}, {"start": 11, "end": 16, "text": "DrugB"}]
    monkeypatch.setattr(trial, "raw_registry", lambda *args: [{"description": description, "core": ["druga"]}])
    arms = trial.registry({"text": text}, {}, chemical, [])
    assert arms[0]["core"] == ["druga", "drugb"]
    assert resolve_arms({"text": "DrugA reduced pressure compared with placebo."}, arms) == []
    assert resolve_arms({"text": "DrugA plus DrugB reduced pressure."}, arms) == arms
