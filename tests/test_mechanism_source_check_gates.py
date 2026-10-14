import copy

import pytest

from scripts.verify_mechanism_source_checks import validate_review


@pytest.mark.parametrize('violation', ['human_approval', 'primary_verified', 'release', 'health', 'dose', 'bypass'])
def test_incomplete_assistant_check_cannot_be_promoted(violation):
    # Synthetic unreviewed record; no private capture is needed to test the gate.
    record = copy.deepcopy({"review_actor": "assistant", "human_approved": False,
                            "primary_relation_verified": False,
                            "primary_text": {"redistribution_approved": False},
                            "context": {"health_outcome": None, "nutrient_dose": None},
                            "access": {"gate_bypassed": False}})
    if violation == 'human_approval':
        record['human_approved'] = True
    elif violation == 'primary_verified':
        record['primary_relation_verified'] = True
    elif violation == 'release':
        record['primary_text']['redistribution_approved'] = True
    elif violation == 'health':
        record['context']['health_outcome'] = 'improved health'
    elif violation == 'dose':
        record['context']['nutrient_dose'] = 100
    else:
        record['access']['gate_bypassed'] = True
    with pytest.raises(ValueError):
        validate_review(record)
