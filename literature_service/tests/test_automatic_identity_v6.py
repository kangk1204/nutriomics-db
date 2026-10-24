from nutri_literature.automatic_identity_v6 import choose


def test_identity_recovery_does_not_default_species_or_use_relation_score():
    records = [{"id": "NCBIGene:1", "native_id": "1", "taxon_id": 9606}]
    assert choose(records, [.99], {"eligible": False, "taxon_id": None}, .9, .1) is None
    assert choose(records, [.99], {"eligible": False, "taxon_id": 9606}, .9, .1) is None
    assert choose(records, [.99], {"eligible": True, "taxon_id": 9606}, .9, .1) == records[0]


def test_conflicting_id_candidates_remain_review_hypotheses():
    records = [{"id": "NCBIGene:1", "native_id": "1", "taxon_id": 9606},
               {"id": "NCBIGene:2", "native_id": "2", "taxon_id": 9606}]
    assert choose(records, [.95, .94], {"eligible": True, "taxon_id": 9606}, .9, .1) is None
    assert choose(records, [.95, .6], {"eligible": True, "taxon_id": 9606}, .9, .1) == records[0]
